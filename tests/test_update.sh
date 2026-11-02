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
printf '#!/bin/sh\n[ -f "%s/checks.json" ] && cat "%s/checks.json" || exit 22\n' "$T" "$T" > "$T/bin/curl"
command -v flock >/dev/null || printf '#!/bin/sh\nexit 0\n' > "$T/bin/flock"
if ! python3 -c "" 2>/dev/null; then printf '#!/bin/sh\nexec python "$@"\n' > "$T/bin/python3"; fi
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" OUTBOX_DIR="$T/outbox"
results() { echo "{\"check_runs\": [{\"status\": \"$1\", \"conclusion\": $2}]}" > "$T/checks.json"; }

# "GitHub", a developer's copy, and the server's copy.
git init -q --bare -b main "$T/github.git"
git clone -q "$T/github.git" "$T/dev" 2>/dev/null; git -C "$T/dev" config core.autocrlf false
cp "$REPO/update.sh" "$REPO/.gitignore" "$T/dev/"
git -C "$T/dev" -c user.name=t -c user.email=t@t add -A
git -C "$T/dev" -c user.name=t -c user.email=t@t commit -qm "First version"
git -C "$T/dev" push -q origin main
git clone -q "$T/github.git" "$T/server"
S="$T/server"
mkdir -p "$S/telegram-connector" "$S/telegram-caller/venv/bin"   # an installed setup
touch "$S/telegram-connector/.env" "$S/telegram-caller/.env" "$S/.env"
printf '#!/bin/sh\necho "pip $*" >> "%s/calls.log"\n' "$T" > "$S/telegram-caller/venv/bin/pip"
chmod +x "$S/telegram-caller/venv/bin/pip"
TITLE="$(grep -m1 '^TITLE=' "$REPO/update.sh" | cut -d'"' -f2)"
newest() { cat "$(ls -d "$T"/outbox/*.txt 2>/dev/null | tail -1)" 2>/dev/null || true; }
release() { echo "$1" >> "$T/dev/notes.txt"; git -C "$T/dev" add -A
            git -C "$T/dev" -c user.name=t -c user.email=t@t commit -qm "$1"; git -C "$T/dev" push -q origin main; }

REPORT=1 bash "$S/update.sh" >/dev/null
check '[[ "$(newest)" == *"$TITLE is up to date"* ]]' "/update with nothing new says it's up to date"
rm -f "$T"/outbox/*; bash "$S/update.sh" >/dev/null
check '[ -z "$(ls "$T/outbox")" ]' "the nightly run with nothing new stays quiet"

release "Add reactions"
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

echo; echo "ALL $ok CHECKS PASSED"
