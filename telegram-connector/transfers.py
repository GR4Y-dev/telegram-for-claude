"""Bounded disk transfers. Temporary files never escape the operation that owns them."""
import asyncio
from contextlib import asynccontextmanager
import ipaddress
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import unquote, urljoin, urlparse

import httpx

MB = 1024 * 1024
CHUNK_SIZE = 256 * 1024


class TransferError(ValueError):
    """A fixed public-facing transfer failure, never an underlying provider message."""


def timeout_seconds(size):
    # Budget for downloading AND uploading, at 256 KiB/s overall, plus startup.
    return 120 + size / CHUNK_SIZE


def safe_name(name):
    """Treat remote filenames as labels, never paths (including Windows paths)."""
    name = str(name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]', "_", name).strip(" .")
    if not name or name.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *[f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)]}:
        name = "file"
    # Keep UTF-8 filenames within filesystem component limits, preserving the suffix.
    suffix = Path(name).suffix[:20]
    stem = name[:-len(suffix)] if suffix else name
    stem = stem.encode("utf-8")[:200 - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore")
    return stem + suffix


class Transfer:
    def __init__(self, folder, limit, reserve, expected=None):
        self.folder, self.limit, self.reserve = Path(folder), limit, reserve
        self.size, self.expected = 0, expected
        self.mime = ""
        self.timer = None
        self.started = asyncio.get_running_loop().time()
        self.path, self.stream = self.folder / "payload", None
        self.preflight(expected)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.stream = os.fdopen(fd, "wb", buffering=0)

    def check_disk(self, needed=0):
        if shutil.disk_usage(self.folder).free < self.reserve + needed:
            raise TransferError(f"Not enough free disk space: transfers must leave {self.reserve // MB} MB free.")

    def preflight(self, expected):
        if expected is not None and (expected < 0 or expected > self.limit):
            raise TransferError(f"That file is bigger than {self.limit // MB} MB.")
        self.check_disk(expected or 0)
        self.expected = expected
        if self.timer is not None and expected is not None:
            self.timer.reschedule(self.started + timeout_seconds(expected))

    def write(self, chunk):
        if self.size + len(chunk) > self.limit:
            raise TransferError(f"That file is bigger than {self.limit // MB} MB.")
        if self.expected is not None and self.size + len(chunk) > self.expected:
            raise TransferError("The file size changed during download. Please try again.")
        self.check_disk(len(chunk))
        written = self.stream.write(chunk)
        if written != len(chunk):
            raise OSError("Incomplete disk write")
        self.size += written
        return written

    def finish(self, name):
        self.close()
        if self.expected is not None and self.size != self.expected:
            raise TransferError("The download ended before the complete file arrived.")
        target = self.folder / safe_name(name)
        self.path.rename(target)
        self.path = target
        if self.timer is not None:
            self.timer.reschedule(self.started + timeout_seconds(self.size))
        return self

    def tell(self):
        # Telethon's progress_callback path reports f.tell() after every chunk.
        return self.size

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


class Transfers:
    def __init__(self, reserve):
        self.reserve = reserve
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self, size):
        # No await between checking and acquiring an unlocked asyncio.Lock.
        if self.lock.locked():
            raise TransferError("File transfer busy: another file is being downloaded or uploaded. Try again when it finishes.")
        async with self.lock:
            try:
                async with asyncio.timeout(timeout_seconds(size)) as timer:
                    yield timer
            except TimeoutError:
                raise TransferError("The file transfer took too long and was stopped. Try again with a faster source or a smaller file.") from None

    @asynccontextmanager
    async def file(self, limit, expected=None, *, directory=None):
        async with self.slot(expected if expected is not None else limit) as timer:
            # mkdtemp is private (0700); the file is 0600. Cleanup is synchronous so
            # cancellation cannot interrupt it, including cancellation during upload.
            with tempfile.TemporaryDirectory(prefix="telegram-transfer-", dir=directory) as folder:
                item = Transfer(folder, limit, self.reserve, expected)
                item.timer = timer
                try:
                    yield item
                finally:
                    item.close()


def _check_public(host, infos):
    if not infos:
        raise TransferError("That hostname has no public addresses.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise TransferError("That hostname is a private or local address. Only public web links work.")


async def download_url(url, item):
    """Pin every connection to a checked IP, retaining Host and the TLS name."""
    loop = asyncio.get_running_loop()
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False,
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}) as http:
            for _ in range(6):
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise TransferError("Only http:// and https:// links work.")
                if parsed.username is not None or parsed.password is not None:
                    raise TransferError("Links containing login credentials are not supported.")
                original = httpx.URL(url)
                host = original.raw_host.decode("ascii")
                infos = await loop.getaddrinfo(host, original.port or (443 if original.scheme == "https" else 80))
                _check_public(host, infos)
                pinned = original.copy_with(host=infos[0][4][0])
                async with http.stream("GET", pinned, headers={"Host": original.netloc.decode("ascii")},
                                       extensions={"sni_hostname": host}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise TransferError("The link returned a redirect without a destination.")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise TransferError(f"The link answered with error {response.status_code}.")
                    # Avoid an HTTP decompression bomb before our chunk/size guard.
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise TransferError("The link ignored the uncompressed-download request. Use a direct file link.")
                    length = response.headers.get("content-length")
                    if length is not None and not re.fullmatch(r"[0-9]{1,20}", length):
                        raise TransferError("The link returned an invalid file size.")
                    item.preflight(int(length) if length is not None else None)
                    async for chunk in response.aiter_bytes(chunk_size=CHUNK_SIZE):
                        item.write(chunk)
                    cd = response.headers.get("content-disposition", "")
                    found = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
                    name = unquote(found[1]) if found else unquote(Path(parsed.path).name)
                    item.mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not Path(name).suffix:
                        name = (name or "file") + (mimetypes.guess_extension(item.mime) or "")
                    return item.finish(name)
    except (httpx.HTTPError, httpx.InvalidURL, OSError):
        # Exception strings can contain signed URLs, credentials or local paths.
        raise TransferError("The download failed (network or disk error). Check the link and free space, then try again.") from None
    raise TransferError("That link redirects too many times.")
