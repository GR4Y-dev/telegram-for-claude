"""Short-lived capability URLs. One process owns this private, ephemeral store."""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import quote
import uuid

from starlette.responses import FileResponse, PlainTextResponse

from transfers import TransferError, safe_name, timeout_seconds

SAFE_TYPES = {
    "application/octet-stream", "application/pdf", "application/zip",
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/heic",
    "video/mp4", "video/quicktime", "video/webm", "video/x-matroska",
    "audio/ogg", "audio/mpeg", "audio/mp4", "audio/wav", "audio/flac",
    "text/plain",
}
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'; base-uri 'none'",
}


class NoAccessLogs(logging.Filter):
    def filter(self, record):
        # Also protects the MCP secret, regardless of the hosting log level.
        return False


logging.getLogger("uvicorn.access").addFilter(NoAccessLogs())


def public_origin(domain, secret):
    domain = domain.strip().lower()
    if (not re.fullmatch(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain)
            or "." not in domain or any(not label or len(label) > 63 or
                label.startswith("-") or label.endswith("-") for label in domain.split("."))
            or secret.lower() in domain):
        raise TransferError("Set DOMAIN=telegram.your-domain.com in the connector's .env "
                            "(hostname only, no https:// or path), then recreate the container "
                            "with docker compose up -d. Use your existing tunnel hostname.")
    return "https://" + domain


def export_name(name, secret):
    # Safe both as a Windows filename and inside double quotes in shell commands.
    name = re.sub(r"[^\w .-]", "_", safe_name(name), flags=re.UNICODE)
    name = safe_name(name.replace(secret, "private"))
    return "file_" + name if name.startswith("-") else name


@dataclass
class Export:
    digest: bytes
    path: Path
    name: str
    size: int
    mime: str
    expires: float
    remaining: int = 5
    readers: int = 0
    retired: bool = False


class DownloadResponse(FileResponse):
    def __init__(self, store, entry):
        self.store, self.entry = store, entry
        super().__init__(entry.path, media_type=entry.mime, headers={**HEADERS,
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(entry.name, safe="")})

    async def __call__(self, scope, receive, send):
        # Reserve synchronously, before disk I/O; simultaneous requests cannot
        # exceed the budget. HEAD is free and Range GETs each consume one attempt.
        entry = self.entry
        if entry.retired or time.time() >= entry.expires or entry.remaining <= 0:
            self.store.sweep()
            await PlainTextResponse("Not Found", status_code=404, headers=HEADERS)(scope, receive, send)
            return
        if scope["method"] == "GET":
            entry.remaining -= 1
        entry.readers += 1
        async def secured_send(message):
            if message["type"] == "http.response.start":
                # FileResponse's malformed/unsatisfiable Range replies have their
                # own headers. Keep our privacy policy on those responses too.
                headers = dict(message["headers"])
                headers.update({k.lower().encode(): v.encode() for k, v in HEADERS.items()})
                headers[b"content-disposition"] = self.headers["content-disposition"].encode()
                message = {**message, "headers": list(headers.items())}
            await send(message)
        try:
            # FileResponse handles bounded chunks, HEAD, Range and Content-Length.
            # A slow/disconnected client cannot pin retired disk space indefinitely.
            async with asyncio.timeout(timeout_seconds(entry.size)):
                await super().__call__(scope, receive, secured_send)
        finally:
            entry.readers -= 1
            self.store.sweep()


class DownloadLinks:
    def __init__(self, folder, max_bytes, minutes=30):
        self.folder = Path(folder)
        self.max_bytes = max_bytes
        self.minutes = max(1, min(1440, minutes))
        self.entries = []
        self.generation = 0
        self.sweep_seconds = 30

    def prepare(self):
        if self.folder.is_symlink():
            raise TransferError("Export storage is unsafe; check DATA_DIR permissions.")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)

    def startup_cleanup(self):
        self.prepare()
        # Metadata is memory-only: all previous-process exports are invalid.
        # Never follow symlinks or touch other DATA_DIR contents.
        for path in self.folder.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}\.bin", path.name):
                path.unlink()
            elif path.name.startswith("telegram-transfer-"):
                if path.is_symlink():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)

    def sweep(self):
        for entry in list(self.entries):
            if entry.retired or entry.remaining <= 0 or time.time() >= entry.expires:
                entry.retired = True
                if not entry.readers:
                    entry.path.unlink(missing_ok=True)
                    self.entries.remove(entry)

    def revoke(self):
        self.generation += 1  # Also invalidate any export still downloading.
        count = sum(not e.retired for e in self.entries)
        for entry in self.entries:
            entry.retired = True
        self.sweep()
        return count

    async def _sweeper(self):
        while True:
            await asyncio.sleep(self.sweep_seconds)
            try:
                self.sweep()
            except OSError:
                logging.getLogger(__name__).warning("Export cleanup failed; check storage permissions.")

    @asynccontextmanager
    async def lifespan(self):
        self.startup_cleanup()
        task = asyncio.create_task(self._sweeper())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.revoke()

    def make_room(self, needed):
        removed = 0
        self.sweep()
        while (len(self.entries) >= 5 or sum(e.size for e in self.entries) + needed > self.max_bytes):
            oldest = next((e for e in self.entries if not e.retired), None)
            if oldest is None:
                raise TransferError("Export storage is busy with active downloads. Try again when they finish.")
            oldest.retired = True
            removed += 1
            self.sweep()
        return removed

    async def create(self, client, message, transfers, limit, origin, secret):
        self.prepare()
        name = export_name(message.file.name or "file" + (message.file.ext or ""), secret)
        mime = (message.file.mime_type or "").lower()
        mime = mime if mime in SAFE_TYPES else "application/octet-stream"
        limit = min(limit, self.max_bytes)
        generation = self.generation
        # Same filesystem as the final export: promotion is a rename, not a copy.
        async with transfers.file(limit, message.file.size or None, directory=self.folder) as item:
            removed = self.make_room(item.expected if item.expected is not None else limit)
            await client.download_media(message, file=item)  # No thumb: largest photo/original document.
            item.finish("complete")
            if generation != self.generation:
                raise TransferError("Download links were revoked while this export was downloading. Request a new link.")
            token = secrets.token_urlsafe(32)
            entry = Export(hashlib.sha256(token.encode()).digest(), self.folder / (uuid.uuid4().hex + ".bin"),
                           name, item.size, mime, time.time() + self.minutes * 60)
            item.path.rename(entry.path)
            self.entries.append(entry)
        link = origin + "/files/" + token + "/" + quote(name, safe="")
        return {
            "link": link, "file_name": name, "size_bytes": entry.size, "mime_type": mime,
            "expires_at": datetime.fromtimestamp(entry.expires, timezone.utc).isoformat(),
            "download_command": f'curl -fL -o "{name}" "{link}"',
            "note": "Private link: anyone holding it can download until expiry, for up to 5 GET requests "
                    "(including retries/ranges). HEAD does not count. Use curl.exe in Windows PowerShell. "
                    "Use revoke_download_links to disable links. Downloads already started may finish.",
            "removed_older_links": removed,
            "storage_note": f"Removed {removed} oldest link(s) to make room." if removed else "No older links removed.",
        }

    async def serve(self, request):
        self.sweep()
        token = request.path_params["token"]
        digest = hashlib.sha256(token.encode()).digest()
        # Small bounded list, fixed-size constant-time comparisons; no secret paths.
        entry = next((e for e in self.entries if secrets.compare_digest(e.digest, digest)), None)
        if entry is None or entry.retired:
            return PlainTextResponse("Not Found", status_code=404, headers=HEADERS)
        return DownloadResponse(self, entry)
