#!/usr/bin/env bash
# Optional guest isolation. Does not install or upgrade Claude or change its owner wrapper.
set -Eeuo pipefail
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo "Run this optional setup as the service administrator (root)."; exit 1; }
if [ "${CONNECTOR_UPDATE_LOCKED:-}" != 1 ]; then
  exec python3 ../lock_updates.py /run/lock/claude-updates.lock "$PWD/setup-guests.sh" "$@"
fi
[ -f .env ] || { echo "Set up the owner assistant first."; exit 1; }
NATIVE="${1:-}"
if [ -z "$NATIVE" ]; then
  NATIVE="$(python3 guest_setup.py find)" || {
    echo "Couldn't find the native Claude $(python3 -c 'import guest_setup; print(guest_setup.VERSION)') program."
    echo "Run: bash setup-guests.sh /absolute/path/to/native/claude"; exit 1; }
  echo "Using Claude at $NATIVE"
fi
python3 - "$NATIVE" <<'PY'
import pathlib, subprocess, sys
path = pathlib.Path(sys.argv[1])
if any(c in str(path) for c in ('\n', '"', ',')):
    raise SystemExit('Choose a native binary path without quotes, commas or newlines.')
if not path.is_absolute() or not path.is_file() or path.open('rb').read(4) != b'\x7fELF':
    raise SystemExit('Choose the existing native Linux binary, not the xWriter/owner wrapper.')
version = subprocess.check_output([str(path), '--version'], text=True, timeout=10)
if version.split()[0] != '2.1.286':
    raise SystemExit('Expected the reviewed CLI 2.1.286; no upgrade has been made.')
PY
# Sign in first, so the long build below needs nobody watching.
[ -f data/guest-auth.json ] || python3 guest_setup.py login "$NATIVE" data/guest-auth.json
echo "Building the guest container (a few minutes the first time)..."
docker --host unix:///var/run/docker.sock build -t telegram-guest-claude:2.1.286 guest-runtime
python3 - "$NATIVE" <<'PY'
import os, pathlib, sys, tempfile
data = pathlib.Path('data')
data.mkdir(mode=0o700, exist_ok=True)
auth = data / 'guest-auth.json'
values = {'GUEST_ENABLED':'1', 'GUEST_CLAUDE_BIN':str(pathlib.Path(sys.argv[1]).resolve()),
          'GUEST_TOKEN_FILE':str(auth.resolve())}
lines = [line for line in pathlib.Path('.env').read_text().splitlines()
         if line.partition('=')[0].strip() not in values]
fd, name = tempfile.mkstemp(dir='.', prefix='.guest-settings-')
with os.fdopen(fd, 'w') as out:
    out.write('\n'.join(lines + [f'{key}="{value}"' for key,value in values.items()]) + '\n')
os.replace(name, '.env')
PY
systemctl restart telegram-caller
echo "Guest access is on. In Telegram, send /allow @username to let someone in."
