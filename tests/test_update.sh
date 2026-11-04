#!/usr/bin/env bash
# Tests update.sh against a pretend GitHub (a local git repo) with stand-in docker, systemctl,
# pip and GitHub test results. Nothing on this machine is changed.
#     bash tests/test_update.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
ok=0
check() { if eval "$1"; then ok=$((ok + 1)); echo "ok - $2"; else echo "FAIL: $2"; exit 1; fi; }

# Stand-ins: they only write down that they were called.
mkdir -p "$T/bin" "$T/outbox"
for c in docker systemctl; do printf '#!/bin/sh\necho "%s $*" >> "%s/calls.log"\n' "$c" "$T" > "$T/bin/$c"; done
printf '[ ! -f "%s/docker-fails" ]\n' "$T" >> "$T/bin/docker"   # touch it to make a build fail
cat >> "$T/bin/docker" <<'EOF'
case "$*" in
  'compose config --format json') printf '{"name":"test","services":{"app":{"build":{"context":"."}}}}\n' ;;
  'image inspect '*) echo 'sha256:test-image' ;;
esac
EOF
# A simulated Docker failure must stop before the case's successful exit status.
sed -i '2i set -e' "$T/bin/docker"
printf '#!/bin/sh\n[ -f "%s/checks.json" ] && cat "%s/checks.json" || exit 22\n' "$T" "$T" > "$T/bin/curl"
command -v flock >/dev/null || printf '#!/bin/sh\nexit 0\n' > "$T/bin/flock"
if ! python3 -c "" 2>/dev/null; then printf '#!/bin/sh\nexec python "$@"\n' > "$T/bin/python3"; fi
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" OUTBOX_DIR="$T/outbox" LOCK_DIR="$T"
results() { echo "{\"check_runs\": [{\"name\": \"tests\", \"app\": {\"slug\": \"github-actions\"}, \"status\": \"$1\", \"conclusion\": $2}]}" > "$T/checks.json"; }

# "GitHub", a developer's copy, and the server's copy.
git init -q --bare -b main "$T/github.git"
git clone -q "$T/github.git" "$T/dev" 2>/dev/null; git -C "$T/dev" config core.autocrlf false
cp "$REPO/update.sh" "$REPO/.gitignore" "$REPO/build_images.py" "$REPO/lock_updates.py" "$T/dev/"
mkdir -p "$T/dev/telegram-caller"
if [ -f "$REPO/telegram-caller/models.py" ]; then cp "$REPO/telegram-caller/models.py" "$T/dev/telegram-caller/"; fi
git -C "$T/dev" -c user.name=t -c user.email=t@t add -A
git -C "$T/dev" -c user.name=t -c user.email=t@t commit -qm "First version"
git -C "$T/dev" push -q origin main
git clone -q "$T/github.git" "$T/server"
S="$T/server"
printf 'telegram-caller/venv/\n' >> "$S/.git/info/exclude"
mkdir -p "$S/telegram-connector" "$S/telegram-caller/venv/bin"   # an installed setup
touch "$S/telegram-connector/.env" "$S/telegram-caller/.env" "$S/.env"
printf '#!/bin/sh\necho "pip $*" >> "%s/calls.log"\n' "$T" > "$S/telegram-caller/venv/bin/pip"
chmod +x "$S/telegram-caller/venv/bin/pip"
printf '#!/bin/sh\nexec python3 "$@"\n' > "$S/telegram-caller/venv/bin/python"
chmod +x "$S/telegram-caller/venv/bin/python"
TITLE="$(grep -m1 '^TITLE=' "$REPO/update.sh" | cut -d'"' -f2)"
newest() { cat "$(ls -d "$T"/outbox/*.txt 2>/dev/null | tail -1)" 2>/dev/null || true; }
release() { echo "$1" >> "$T/dev/notes.txt"; git -C "$T/dev" add -A
            git -C "$T/dev" -c user.name=t -c user.email=t@t commit -qm "$1"; git -C "$T/dev" push -q origin main; }

REPORT=1 bash "$S/update.sh" >/dev/null
check '[[ "$(newest)" == *"$TITLE is up to date"* ]]' "/update with nothing new says it's up to date"
rm -f "$T"/outbox/*; bash "$S/update.sh" >/dev/null
check '[ -z "$(ls "$T/outbox")" ]' "the nightly run with nothing new stays quiet"

release "Add reactions"
echo '{"check_runs":[]}' > "$T/checks.json"
REPORT=1 bash "$S/update.sh" >/dev/null
check '[ ! -f "$S/notes.txt" ]' "missing test results cannot install an untested version"
results completed '"skipped"'; bash "$S/update.sh" >/dev/null
check '[ ! -f "$S/notes.txt" ]' "skipped tests do not count as passing"
echo '{"check_runs":[{"name":"tests","app":{"slug":"untrusted"},"status":"completed","conclusion":"success"}]}' > "$T/checks.json"
bash "$S/update.sh" >/dev/null
check '[ ! -f "$S/notes.txt" ]' "a different check provider cannot stand in for GitHub tests"
rm -f "$T"/outbox/*
results in_progress null; bash "$S/update.sh" >/dev/null
check '[ ! -f "$S/notes.txt" ] && [ -z "$(ls "$T/outbox")" ]' "a version still being tested isn't installed"
results completed '"failure"'; bash "$S/update.sh" >/dev/null
check '[ ! -f "$S/notes.txt" ] && [[ "$(newest)" == *"tests failed"* ]]' "a version whose tests failed isn't installed, and you're told"
rm -f "$T/checks.json"; REPORT=1 bash "$S/update.sh" >/dev/null
check '[ ! -f "$S/notes.txt" ] && [[ "$(newest)" == *"Couldn"* ]]' "GitHub unreachable: nothing installed"

results completed '"success"'; rm -f "$T"/outbox/* "$T/calls.log"
bash "$S/update.sh" >/dev/null
check '[ -f "$S/notes.txt" ] && [ "$(git -C "$S" rev-parse HEAD)" = "$(git -C "$T/dev" rev-parse HEAD)" ]' "a version whose tests passed is installed"
check 'grep -q docker "$T/calls.log"' "it's rebuilt and restarted: $(tr '\n' ';' < "$T/calls.log")"
check '[[ "$(newest)" == *"$TITLE updated"*"Add reactions"* ]]' "you're told what changed"
check '[ -f "$T/claude-updates.lock" ]' "projects take turns through a shared lock"

release "Add polls"; touch "$T/docker-fails"; rm -f "$T"/outbox/*
bash "$S/update.sh" >/dev/null 2>&1 || true
check '! grep -q "Add polls" "$S/notes.txt" && [[ "$(newest)" == *"failed; the next update will try again"* ]]' \
  "a failed install goes back to the previous version"
rm -f "$T/docker-fails"; bash "$S/update.sh" >/dev/null
check 'grep -q "Add polls" "$S/notes.txt" && [[ "$(newest)" == *"Add polls"* ]]' "the next update installs it after all"

rm -f "$T/calls.log"
bash "$S/update.sh" --now >/dev/null
check '! grep -q "compose build" "$T/calls.log"' "unchanged images are reused, including --now"
check '! grep -q "prune" "$T/calls.log"' "no global cleanup touches other applications"
echo local-edit >> "$S/update.sh"
check '! bash "$S/update.sh" --now >/dev/null 2>&1' "local edits stop manual updates too"
git -C "$S" checkout -q -- update.sh
mv "$T/claude-updates.lock" "$T/safe-lock"
ln -s "$T/safe-lock" "$T/claude-updates.lock"
check '! bash "$S/update.sh" --now >/dev/null 2>&1' "symlink locks fail closed"

echo; echo "ALL $ok CHECKS PASSED"
