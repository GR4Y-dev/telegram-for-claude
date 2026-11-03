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
[ -n "$NATIVE" ] || { echo "Usage: bash setup-guests.sh /absolute/path/to/native/claude"; exit 1; }
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
docker --host unix:///var/run/docker.sock build -t telegram-guest-claude:2.1.286 guest-runtime
python3 - "$NATIVE" <<'PY'
import getpass, json, os, pathlib, sys, tempfile
data = pathlib.Path('data')
data.mkdir(mode=0o700, exist_ok=True)
auth = data / 'guest-auth.json'
if not auth.exists():
    print('Generate a separate long-lived token with your native Claude setup-token command.')
    print('Paste it below; it stays hidden and is not put in the command history.')
    token = getpass.getpass('Guest Claude OAuth token: ').strip()
    if not token or '\n' in token:
        raise SystemExit('No valid token supplied; guest access stays off.')
    fd = os.open(auth, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump({'CLAUDE_CODE_OAUTH_TOKEN':token}, out)
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
echo "Guest isolation configured. Send /allow @username to approve one person."
