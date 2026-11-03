"""Guest Claude runs in a limited container, never in the owner's home or wrapper.

Only the exact native CLI and that guest's private directory are mounted. No account
data, MCP config, host socket, owner environment, plugins or settings are inherited.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from claude_stream import Conversation
from processes import stop

IMAGE = "telegram-guest-claude:2.1.286"
UID = 65532
MAX_STORAGE = 32 * 1024 * 1024


class GuestUnavailable(RuntimeError):
    pass


def clean_environment():
    return {"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8"}


async def docker_command(args, env=None):
    process = await asyncio.create_subprocess_exec(
        *args, env=env or clean_environment(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=os.name == "posix")
    try:
        result, _ = await asyncio.wait_for(process.communicate(), 10)
        if process.returncode:
            raise GuestUnavailable("Guest container operation failed")
        return result.decode(errors="replace").strip()
    finally:
        await stop(process)


class GuestRuntime:
    def __init__(self, root, binary, auth_file):
        self.root, self.binary, self.auth_file = Path(root), Path(binary), Path(auth_file)
        self.docker = "/usr/bin/docker"
        self.checked = False
        self.failed = False
        self.instance = hashlib.sha256(str(self.root.resolve()).encode()).hexdigest()[:16]

    def docker_args(self):
        # Never inherit a user's remote Docker context or registry credentials.
        folder = self.root / ".docker-client"
        if folder.is_symlink():
            raise GuestUnavailable("Unexpected Docker client configuration")
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        config = folder / "config.json"
        try:
            fd = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if config.is_symlink() or config.stat().st_size > 4 or config.read_text().strip() != "{}":
                raise GuestUnavailable("Unexpected Docker client configuration")
        else:
            with os.fdopen(fd, "w") as out:
                out.write("{}")
        return [self.docker, "--config", str(folder.resolve()), "--host", "unix:///var/run/docker.sock"]

    def credentials(self):
        info = self.auth_file.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 16384
                or info.st_uid != os.geteuid() or info.st_mode & 0o077):
            raise GuestUnavailable("Guest credential file must be private and owned by the service user")
        values = json.loads(self.auth_file.read_text())
        if (not isinstance(values, dict) or len(values) != 1
                or next(iter(values), None) not in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")):
            raise GuestUnavailable("Guest credential file must contain one supported credential")
        if any(not isinstance(v, str) or not 1 <= len(v) <= 8192 or "\n" in v or "\0" in v for v in values.values()):
            raise GuestUnavailable("Invalid guest credential")
        return values

    async def ready(self):
        if self.failed:
            raise GuestUnavailable("Guest cleanup failed; restart after checking Docker")
        if os.name != "posix" or not Path("/proc").is_dir() or not Path(self.docker).is_file():
            raise GuestUnavailable("Guest isolation requires the Linux container setup")
        self.credentials()
        if not self.binary.is_absolute() or not self.binary.is_file():
            raise GuestUnavailable("Guest native Claude binary is not configured")
        with self.binary.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise GuestUnavailable("Guests require the native binary, never the owner's wrapper")
        if not self.checked:
            version = await docker_command([str(self.binary), "--version"])
            if not re.match(r"^2\.1\.286(?:\s|$)", version):
                raise GuestUnavailable("Guest CLI must be the reviewed version 2.1.286")
            label = await docker_command(self.docker_args() + ["image", "inspect", "--format",
                                          '{{index .Config.Labels "telegram.guest-runtime"}}', IMAGE])
            if label != "1":
                raise GuestUnavailable("Guest runtime image is missing or unrecognized")
            # A service crash can leave a daemon-owned child. Reap only this install's
            # labelled containers, never owner connectors or another application's work.
            await self.reap_orphans()
            self.checked = True

    async def reap_orphans(self):
        old = await docker_command(self.docker_args() + ["ps", "-aq", "--filter",
                                     f"label=telegram.guest-session={self.instance}"])
        ids = old.split()
        if any(not re.fullmatch(r"[a-f0-9]{12,64}", item) for item in ids):
            raise GuestUnavailable("Unexpected guest container identity")
        if ids:
            await docker_command(self.docker_args() + ["rm", "-f", *ids])

    def folder(self, owner, guest, generation):
        if any(type(n) is not int or n <= 0 for n in (owner,guest,generation)):
            raise GuestUnavailable("Invalid guest scope")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = self.root.resolve()
        current = root
        # Refuse symlinks before mkdir/chown; IDs cannot become filesystem paths.
        for part in (str(owner), str(guest), str(generation)):
            current = current / part
            if current.is_symlink():
                raise GuestUnavailable("Invalid guest directory")
            current.mkdir(mode=0o700, exist_ok=True)
        for child in (current, current / "home", current / "work"):
            if child.is_symlink():
                raise GuestUnavailable("Invalid guest directory")
            child.mkdir(mode=0o700, exist_ok=True)
            os.chown(child, UID, UID)
        # A bounded history, separate from the owner's storage and every other guest.
        total = sum(p.lstat().st_size for p in current.parent.rglob("*") if p.is_file() and not p.is_symlink())
        if total > MAX_STORAGE:
            raise GuestUnavailable("Guest history limit reached; ask the owner to clear it")
        return current

    def base_args(self, folder, name, *, auth=True):
        binary, folder = self.binary.resolve(), folder.resolve()
        if any("," in str(path) or "\n" in str(path) for path in (binary,folder)):
            raise GuestUnavailable("Unsupported mount path")
        args = self.docker_args() + ["run", "--rm", "--init", "-i", "--pull=never", "--name", name,
                "--label", f"telegram.guest-session={self.instance}", "--read-only", "--user", f"{UID}:{UID}",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=96",
                "--memory=512m", "--memory-swap=512m", "--cpus=1", "--log-driver=none",
                "--ulimit", "nofile=1024:1024", "--ulimit", "fsize=33554432:33554432",
                "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777",
                "--mount", f"type=bind,src={binary},dst=/usr/local/bin/claude,readonly",
                "--mount", f"type=bind,src={folder},dst=/guest", "--workdir", "/guest/work",
                "--env", "HOME=/guest/home", "--env", "CLAUDE_CONFIG_DIR=/guest/home/.claude",
                "--env", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                "--env", "DISABLE_AUTOUPDATER=1", "--env", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=2048"]
        if auth:
            for key in self.credentials():
                args += ["--env", key]  # values come from the clean subprocess env, never argv
        return args + [IMAGE]

    async def conversation(self, owner, guest, generation, session, prompt, model=""):
        await self.ready()
        folder = self.folder(owner, guest, generation)
        if session and (not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", session)):
            raise GuestUnavailable("Invalid guest session")
        # Never resume an owner's or another guest's session, even if a pointer is corrupted.
        if session and not (folder / "home/.claude/projects/-guest-work" / f"{session}.jsonl").is_file():
            session = None
        name = "telegram-guest-" + uuid.uuid4().hex
        args = self.base_args(folder, name) + ["/usr/local/bin/claude", "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--restricted", "--safe-mode", "--setting-sources", "", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--tools", "WebSearch,WebFetch",
            "--allowedTools", "WebSearch,WebFetch", "--disallowedTools",
            "Read,Write,Edit,Bash,PowerShell,NotebookEdit,Agent,Task,mcp__telegram__*",
            "--permission-mode", "default", "--max-turns", "8", "--settings",
            '{"alwaysThinkingEnabled":false}', "--append-system-prompt", prompt]
        if session:
            args += ["--resume", session]
        if model:
            args += ["--model", model]
        env = clean_environment() | self.credentials()
        return GuestConversation(self, name, args, folder, env)

    async def clear(self, owner, guest):
        """Only after cancelling this guest's job; deletion cannot escape its numeric scope."""
        if any(type(n) is not int or n <= 0 for n in (owner,guest)):
            raise GuestUnavailable("Invalid guest scope")
        root = self.root.resolve()
        folder = root / str(owner) / str(guest)
        if folder.is_symlink() or folder.parent.is_symlink() or not folder.resolve().is_relative_to(root):
            raise GuestUnavailable("Invalid guest directory")
        if folder.exists():
            shutil.rmtree(folder)


class GuestConversation(Conversation):
    def __init__(self, runtime, name, args, folder, env):
        super().__init__(args, folder, 90, env=env)
        self.runtime, self.name = runtime, name

    async def close(self):
        try:
            await super().close()
        finally:
            # Killing docker's client alone does not stop a container owned by its daemon.
            name, self.name = self.name, None
            if name:
                try:
                    try:
                        await docker_command(self.runtime.docker_args() + ["rm", "-f", name])
                    except GuestUnavailable:
                        # --rm may already have removed it. Confirm none remains.
                        remaining = await docker_command(self.runtime.docker_args() + ["ps", "-aq", "--filter", f"name=^{name}$"])
                        if remaining:
                            raise GuestUnavailable("Guest container did not stop")
                except BaseException:
                    self.runtime.failed = True
                    self.name = name  # preserve identity for an explicit cleanup retry
                    raise
