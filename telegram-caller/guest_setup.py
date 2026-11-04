"""One-command guest setup: find the native Claude CLI and capture a guest login token.

Standard library only; setup-guests.sh runs it with the system python3 as root.
"""
import getpass
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

VERSION = "2.1.286"
# Native installs only. The owner's `claude` may be a wrapper script; its target is found here.
CANDIDATES = (
    "/var/lib/xwriter-clis/tools/claude/*/claude",
    "/root/.local/share/claude/versions/*",
    "/home/*/.local/share/claude/versions/*",
    "/usr/local/share/claude/versions/*",
)
TOKEN = re.compile(r"sk-ant-oat\d\d-[A-Za-z0-9_-]{20,}")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def cli_version(path: str) -> str:
    out = subprocess.check_output([path, "--version"], text=True, timeout=10,
                                  stderr=subprocess.DEVNULL).split()
    return out[0] if out else ""


def find_native(patterns=CANDIDATES, version=cli_version) -> str | None:
    """Return the first native (ELF) Claude CLI of the reviewed version, newest first."""
    paths = []
    which = shutil.which("claude")
    if which:
        paths.append(which)
    for pattern in patterns:
        paths += sorted(glob.glob(pattern), reverse=True)
    seen = set()
    for path in paths:
        path = os.path.realpath(path)
        if path in seen or not os.path.isfile(path) or not is_elf(path):
            continue
        seen.add(path)
        try:
            if version(path) == VERSION:
                return path
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def extract_token(raw: str) -> str | None:
    found = TOKEN.findall(ANSI.sub("", raw))
    return found[-1] if found else None


def run_setup_token(native: str, workdir: str) -> str | None:
    """Run `claude setup-token` on this terminal and read the token from a private recording."""
    if not shutil.which("script"):
        return None
    private = tempfile.mkdtemp(dir=workdir, prefix=".guest-login-")
    record = os.path.join(private, "session")
    try:
        # A wide terminal keeps the long token on one line in the recording.
        subprocess.run(["script", "-q", "-e", "-c",
                        'stty cols 1000 2>/dev/null; exec "$GUEST_NATIVE" setup-token', record],
                       env={**os.environ, "GUEST_NATIVE": native}, check=False)
        try:
            with open(record, "rb") as f:
                return extract_token(f.read().decode("utf-8", "replace"))
        except OSError:
            return None
    finally:
        if os.path.exists(record):
            with open(record, "r+b") as f:
                f.write(b"\0" * os.path.getsize(record))
            os.remove(record)
        os.rmdir(private)


def write_auth(path: str, token: str):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump({"CLAUDE_CODE_OAUTH_TOKEN": token}, out)


def login(native: str, auth: str):
    data = os.path.dirname(os.path.abspath(auth))
    os.makedirs(data, mode=0o700, exist_ok=True)
    print("\nSign in for guests: open the link below, approve, and paste the code back here.\n")
    token = run_setup_token(native, data)
    # Clear the screen and scrollback so the token isn't left on display.
    print("\x1b[3J\x1b[H\x1b[2J", end="", flush=True)
    if not token:
        print("Couldn't read the token automatically. Paste it below (it stays hidden).")
        token = getpass.getpass("Guest Claude token: ").strip()
    if not TOKEN.fullmatch(token or ""):
        raise SystemExit("No valid token; guest access stays off. Run the setup again to retry.")
    write_auth(auth, token)
    print("Guest login saved.")


if __name__ == "__main__":
    command = sys.argv[1:2]
    if command == ["find"]:
        found = find_native()
        if not found:
            raise SystemExit(1)
        print(found)
    elif command == ["login"] and len(sys.argv) == 4:
        login(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("Usage: guest_setup.py find | login NATIVE AUTH_FILE")
