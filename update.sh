#!/usr/bin/env bash
# Updates this project from GitHub, but only to a version whose tests passed there.
#
#   bash update.sh              update now, if there is a new tested version
#   bash update.sh --now        restart everything on the version that's here (after a manual git pull)
#   bash update.sh --install    update automatically every night
#   bash update.sh --uninstall  stop the nightly updates
#
# Results go to you as a Telegram text from the call assistant's account, when it's set up.
set -Eeuo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
NAME="$(basename "$ROOT")"
TITLE="Telegram for Claude"
OUTBOX="${OUTBOX_DIR:-/var/spool/telegram-assistant}"   # the call assistant texts what lands here
REPORT="${REPORT:-0}"                                    # 1: also say when there's nothing new

notify() {
  echo "$1"
  if [ -d "$OUTBOX" ]; then printf '%s\n' "$1" > "$OUTBOX/$(date +%s%N)-$NAME.txt"; fi
}
ROLLBACK=""   # the version to go back to if installing a new one fails
failed() {
  # Back to the previous version, so the next update tries the new one again.
  if [ -n "$ROLLBACK" ]; then git reset -q --hard "$ROLLBACK" || true; fi
  notify "❌ Updating $TITLE failed; the next update will try again. Details: journalctl -u '$NAME-update*' -n 80"
}
trap failed ERR

# What an update does for this project.
apply() {
  if [ -f telegram-connector/.env ]; then
    # The WhatsApp connector runs on this server too: let send_file read its downloaded files.
    if [ -f telegram-connector/docker-compose.whatsapp.yml ] \
       && ! grep -q '^COMPOSE_FILE=' telegram-connector/.env \
       && docker volume inspect "${WHATSAPP_STORE_VOLUME:-whatsapp-connector_whatsapp-store}" >/dev/null 2>&1; then
      printf '\nCOMPOSE_FILE=docker-compose.yml:docker-compose.whatsapp.yml\n' >> telegram-connector/.env
    fi
    # Keep Compose in this folder so .env's COMPOSE_FILE opt-ins apply to every call.
    (cd telegram-connector && python3 ../build_images.py)
  fi
  if [ -f telegram-caller/.env ] && [ -x telegram-caller/venv/bin/pip ]; then
    telegram-caller/venv/bin/pip install -q -r telegram-caller/requirements.txt
    telegram-caller/venv/bin/python telegram-caller/models.py --env telegram-caller/.env
    systemctl restart telegram-caller
  fi
}

case "${1:-}" in
  --install)
    cat > "/etc/systemd/system/$NAME-update.service" <<EOF
[Unit]
Description=Update $TITLE from GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $ROOT/update.sh
EOF
    cat > "/etc/systemd/system/$NAME-update.timer" <<EOF
[Unit]
Description=Update $TITLE every night

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    mkdir -p /etc/claude-updates.d
    printf '%s\n%s\n' "$ROOT" "$TITLE" > "/etc/claude-updates.d/$NAME"   # lets /update find it
    systemctl daemon-reload
    systemctl enable --now "$NAME-update.timer" >/dev/null 2>&1
    echo "Automatic updates are on: every night around 04:00 (server time)."
    exit 0 ;;
  --uninstall)
    systemctl disable --now "$NAME-update.timer" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$NAME-update.service" "/etc/systemd/system/$NAME-update.timer" \
          "/etc/claude-updates.d/$NAME"
    systemctl daemon-reload
    echo "Automatic updates are off."
    exit 0 ;;
  --now|"") ;;
  *) echo "Unknown update option."; exit 2 ;;
esac

# One project at a time: their builds share the server's disk and memory.
LOCKS="${LOCK_DIR:-/run/lock}"
[ -d "$LOCKS" ] && [ -w "$LOCKS" ] || { echo "Shared update lock unavailable."; exit 1; }
if [ "${CONNECTOR_UPDATE_LOCKED:-}" != 1 ]; then
  exec python3 lock_updates.py "$LOCKS/claude-updates.lock" "$ROOT/update.sh" "$@"
fi
exec 9>"$(git rev-parse --git-path update.lock)"
flock -n 9 || { echo "An update is already running."; exit 0; }
# Never roll back over somebody's local edits.
git diff --quiet && git diff --cached --quiet || { echo "Local edits found; update stopped."; exit 1; }
[ -z "$(git ls-files --others --exclude-standard)" ] || { echo "Untracked local files found; update stopped."; exit 1; }
if [ "${1:-}" = --now ]; then
  apply
  notify "✅ $TITLE restarted on version $(git log -1 --format='%h (%cs)')."
  exit 0
fi

git fetch -q origin main
HERE="$(git rev-parse HEAD)"
NEW="$(git rev-parse origin/main)"
if [ "$HERE" = "$NEW" ]; then
  [ "$REPORT" = 1 ] && notify "✅ $TITLE is up to date."
  echo "Already up to date."
  exit 0
fi

# Only take a version whose tests passed on GitHub.
SLUG="$(git remote get-url origin | sed -E 's#(\.git)?/*$##; s#^.*github\.com[:/]##')"
TESTS="$(curl -fsS -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/$SLUG/commits/$NEW/check-runs?per_page=100" 2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)
runs = data.get("check_runs", [])
tests = [r for r in runs if r.get("name") == "tests" and r.get("app", {}).get("slug") == "github-actions"]
if data.get("total_count", len(runs)) > len(runs): print("unknown")
elif not tests: print("none")
elif any(r["status"] != "completed" for r in runs): print("running")
elif all(r.get("conclusion") == "success" for r in tests) and all(r.get("conclusion") in ("success", "neutral", "skipped") for r in runs): print("passed")
else: print("failed")' 2>/dev/null || echo unknown)"
case "$TESTS" in
  passed) ;;
  none)
    [ "$REPORT" = 1 ] && notify "⏳ Not updating $TITLE: waiting for the GitHub tests to pass."
    echo "No trusted test result yet."; exit 0 ;;
  running)
    [ "$REPORT" = 1 ] && notify "⏳ A new version of $TITLE is still being tested. Try again in a few minutes."
    echo "The new version's tests are still running."; exit 0 ;;
  failed)
    notify "⚠️ Not updating $TITLE: the new version's tests failed on GitHub."; exit 0 ;;
  *)
    [ "$REPORT" = 1 ] && notify "⚠️ Couldn't check $TITLE's new version on GitHub. Try again later."
    echo "Couldn't reach GitHub."; exit 0 ;;
esac

CHANGES="$(git log --format='• %s' "$HERE..$NEW" | head -5)"
ROLLBACK="$HERE"
git merge -q --ff-only "$NEW"
apply
ROLLBACK=""
notify "✅ $TITLE updated:
$CHANGES"
