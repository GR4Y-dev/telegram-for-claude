"""
A private Telegram connector for Claude.

It logs in as YOUR Telegram account (not a bot) and gives Claude tools to
read your chats and attachments, search, send messages and files, edit,
forward and delete messages. Claude is told to always show you exactly what
will happen, and wait for your OK, before it sends, edits, forwards or deletes.

The address only works with the long secret in it:
    https://<your-domain>/<MCP_SECRET>/mcp
Every other address returns "Not Found".
"""

import asyncio
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import stat
import zipfile
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from PIL import Image as PILImage, ImageOps
from telethon import TelegramClient, utils
from telethon.errors import (FloodWaitError, MessageAuthorRequiredError,
                             MessageEditTimeExpiredError, MessageNotModifiedError,
                             RPCError)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.functions.messages import (DeleteScheduledMessagesRequest, GetFullChatRequest,
                                            TranscribeAudioRequest)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (Channel, Chat, User, UserStatusLastMonth, UserStatusLastWeek,
                               UserStatusOffline, UserStatusOnline, UserStatusRecently)

# ---------------------------------------------------------------- settings
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
ACCOUNT_FILE = DATA_DIR / "account.json"
SECRET = os.environ.get("MCP_SECRET", "").strip()
PORT = int(os.environ.get("PORT", "8000"))
TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
MAX_SENDS_PER_HOUR = int(os.environ.get("MAX_SENDS_PER_HOUR", "30"))
MAX_ATTACHMENT_MB = int(os.environ.get("MAX_ATTACHMENT_MB", "20"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
WHATSAPP_STORE = Path("/whatsapp-store")
# WhatsApp files are streamed from disk, so they can go up to Telegram's own 2 GB limit.
MAX_WHATSAPP_MB = int(os.environ.get("MAX_WHATSAPP_MB", "2000"))
MAX_TEXT_CHARS = 60_000   # how much text one read_attachment call returns
IMAGE_SIDE = 1568         # pictures are shrunk to this many pixels on the long side
SCANNED_PAGES = 5         # pages shown as pictures when a PDF has no text in it
# Optional speech-to-text for voice messages when Telegram Premium can't do it.
# Any OpenAI-compatible service works (OpenAI, Groq, ...).
TRANSCRIBE_API_KEY = os.environ.get("TRANSCRIBE_API_KEY", "").strip()
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
# The AI assistant's own Telegram account that can call the user (see telegram-caller).
CALLER = os.environ.get("CALLER", "").strip()

if len(SECRET) < 24 or not SECRET.isalnum():
    raise SystemExit(
        "MCP_SECRET is missing or too short. Put a long random letters-and-numbers "
        "string (24+ characters) in the .env file. See README.md step 3."
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("telegram-connector")

# ---------------------------------------------------------------- telegram
_client: TelegramClient | None = None
_client_lock = asyncio.Lock()
_sent_times: deque[float] = deque()


async def tg() -> TelegramClient:
    """Return a connected, logged-in Telegram client (made on first use)."""
    global _client
    async with _client_lock:
        if _client is not None and _client.is_connected():
            return _client
        if not ACCOUNT_FILE.exists():
            raise RuntimeError(
                "Not logged in to Telegram yet. On the server, run the login step "
                "from README.md (docker compose run --rm telegram python login.py)."
            )
        acc = json.loads(ACCOUNT_FILE.read_text())
        client = TelegramClient(
            StringSession(acc["session"]),
            int(acc["api_id"]),
            acc["api_hash"],
            device_model="Claude connector",
            system_version="Linux server",
            app_version="1.0",
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                "Telegram logged this connector out (maybe the session was ended in "
                "Settings > Devices). Run the login step from README.md again."
            )
        _client = client
        return _client


def _kind(entity) -> str:
    if isinstance(entity, User):
        return "bot" if entity.bot else "person"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        return "group" if entity.megagroup else "channel"
    return "chat"


def _when(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if dt else ""


def _parse_time(value: str, end_of_day: bool = False) -> datetime:
    """'2026-11-03 08:00' or '2026-11-03' in the user's time zone. A bare date used as
    an end point means the end of that day."""
    raw = (value or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("T", " "))
    except ValueError:
        raise ValueError(f"'{value}' isn't a time I understand. Use 2026-11-03 08:00 "
                         "or 2026-11-03 (the user's local time).")
    if end_of_day and len(raw) == 10:
        dt += timedelta(days=1)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _schedule_time(value: str) -> datetime:
    dt = _parse_time(value)
    now = datetime.now(TZ)
    if dt < now + timedelta(seconds=30):
        raise ValueError(f"{_when(dt)} is not in the future (it's {_when(now)} now).")
    if dt > now + timedelta(days=365):
        raise ValueError("Telegram can schedule at most a year ahead.")
    return dt


def _last_seen(status) -> str:
    if isinstance(status, UserStatusOnline):
        return "online now"
    if isinstance(status, UserStatusOffline):
        return f"last seen {_when(status.was_online)}"
    if isinstance(status, UserStatusRecently):
        return "last seen recently"
    if isinstance(status, UserStatusLastWeek):
        return "last seen within a week"
    if isinstance(status, UserStatusLastMonth):
        return "last seen within a month"
    return "hidden or a long time ago"


def _size(n: int | None) -> str:
    if not n:
        return "size unknown"
    if n < 1024 * 1024:
        return f"{max(1, n // 1024)} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def _media_label(m) -> str:
    if m.photo:
        return "[photo]"
    if m.voice:
        return "[voice message]"
    if m.video_note:
        return "[round video]"
    if m.video:
        return "[video]"
    if m.sticker:
        return "[sticker]"
    if m.gif:
        return "[GIF]"
    if m.audio:
        return "[audio]"
    if m.document:
        name = m.file.name if m.file and m.file.name else "file"
        return f"[file: {name}]"
    if m.geo:
        return "[location]"
    if m.poll:
        return "[poll]"
    if m.web_preview:
        return "[link preview]"
    return "[media]" if m.media else ""


def _has_file(m) -> bool:
    return bool(m.media) and not m.web_preview and m.file is not None


def _fmt(m) -> dict:
    who = "You" if m.out else (utils.get_display_name(m.sender) if m.sender else "Unknown")
    text = (m.message or "").strip()
    media = _media_label(m)
    item = {"id": m.id, "time": _when(m.date), "from": who, "text": f"{media} {text}".strip()}
    if _has_file(m):
        item["attachment"] = True
    if m.reply_to and getattr(m.reply_to, "reply_to_msg_id", None):
        item["reply_to"] = m.reply_to.reply_to_msg_id
    return item


async def resolve_chat(client: TelegramClient, chat: str):
    """Turn what Claude passes ("me", @username, +phone, id, or a chat name) into a chat."""
    raw = (chat or "").strip()
    if not raw:
        raise ValueError("Say which chat: a name, @username, phone number, chat id, or 'me'.")
    low = raw.lower()
    if low in ("me", "saved", "saved messages", "myself"):
        return await client.get_me()

    # @username, t.me link, +phone
    if raw.startswith("@") or "t.me/" in low or raw.startswith("+"):
        return await client.get_entity(raw)

    # numeric chat id
    if raw.lstrip("-").isdigit():
        wanted = int(raw)
        try:
            return await client.get_entity(wanted)
        except (ValueError, RPCError):
            async for d in client.iter_dialogs(limit=1000):
                if d.id == wanted:
                    return d.entity
            raise ValueError(f"No chat with id {raw}.")

    # a name: look through your chat list
    exact, partial = [], []
    async for d in client.iter_dialogs(limit=1000):
        name = (d.name or "").strip()
        if name.lower() == low:
            exact.append(d)
        elif low in name.lower():
            partial.append(d)
    found = exact or partial
    if len(found) == 1:
        return found[0].entity
    if not found:
        raise ValueError(
            f"No chat called '{raw}'. Try find_chats to search, or use their @username."
        )
    options = "; ".join(f"{d.name} (id {d.id}, {_kind(d.entity)})" for d in found[:10])
    raise ValueError(f"More than one chat matches '{raw}': {options}. Use the id to pick one.")


async def _get_message(client: TelegramClient, entity, message_id: int):
    m = await client.get_messages(entity, ids=int(message_id))
    if m is None:
        raise ValueError(f"No message {message_id} in that chat. Use read_messages to get ids.")
    return m


def _check_send_limit(count: int = 1):
    now = time.time()
    while _sent_times and now - _sent_times[0] > 3600:
        _sent_times.popleft()
    if len(_sent_times) + count > MAX_SENDS_PER_HOUR:
        raise RuntimeError(
            f"Safety limit reached: {MAX_SENDS_PER_HOUR} messages in the last hour. "
            "This protects the account from looking like spam. Try again later."
        )


def _record_sends(count: int = 1):
    _sent_times.extend([time.time()] * count)


def _slow_down(e: FloodWaitError):
    return RuntimeError(f"Telegram says to slow down. Wait {e.seconds} seconds and try again.")


# ---------------------------------------------------------------- reading files
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}
TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".css", ".js", ".ts",
    ".py", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".php", ".rb", ".sh", ".bat",
    ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".log",
    ".srt", ".vtt", ".tex", ".asc", ".pem", ".svg",
}


def _cut(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + "\n\n[...cut off here: the file is longer than this]"


def _to_image(data: bytes) -> Image:
    """Shrink a picture so Claude can look at it, and send it as a JPEG."""
    with PILImage.open(io.BytesIO(data)) as im:
        if im.width * im.height > 25_000_000:
            raise ValueError("That image is too large to decode safely (25 megapixels max).")
        im.seek(0)  # first frame of GIFs and animated pictures
        im = ImageOps.exif_transpose(im)
        im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = PILImage.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


def _read_pdf(data: bytes, start_page: int) -> list:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = doc.page_count
    start = max(1, int(start_page))
    if start > pages:
        raise ValueError(f"This PDF has only {pages} pages.")
    parts, total, last, letters = [], 0, start - 1, 0
    for i in range(start - 1, min(pages, start + 49)):
        text = doc[i].get_text().strip()
        chunk = f"--- page {i + 1} ---\n{text}"
        if parts and total + len(chunk) > MAX_TEXT_CHARS:
            break
        parts.append(_cut(chunk))
        total += len(chunk)
        letters += len(text)
        last = i + 1
    out = [f"PDF with {pages} pages. Showing pages {start}-{last}."]
    if letters < 50 * (last - start + 1):
        # No real text inside: probably a scan or photos. Show the pages as pictures.
        last = min(pages, start + SCANNED_PAGES - 1)
        out[0] = (f"PDF with {pages} pages. It has almost no text inside (probably scanned), "
                  f"so here are pages {start}-{last} as pictures.")
        for i in range(start - 1, last):
            page = doc[i]
            scale = min(110 / 72, IMAGE_SIDE / max(1, page.rect.width, page.rect.height))
            out.append(_to_image(page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")))
    else:
        out.append("\n\n".join(parts))
    if last < pages:
        out.append(f"More pages left: call read_attachment again with start_page={last + 1}.")
    doc.close()
    return out


def _check_office_size(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        items = archive.infolist()
        if (len(items) > 10000 or sum(x.file_size for x in items) > 64 * 1024 * 1024
                or any(x.file_size > 32 * 1024 * 1024 for x in items)):
            raise ValueError("That document expands beyond the safe reading limit.")


def _read_docx(data: bytes) -> list:
    import docx

    _check_office_size(data)
    d = docx.Document(io.BytesIO(data))
    lines = [p.text for p in d.paragraphs if p.text.strip()]
    for n, table in enumerate(d.tables, 1):
        lines.append(f"\n--- table {n} ---")
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return [_cut("\n".join(lines)) or "The document is empty."]


def _read_xlsx(data: bytes) -> list:
    import openpyxl

    _check_office_size(data)
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets[:20]:
        lines.append(f"=== sheet: {ws.title} ===")
        total = sum(map(len, lines))
        for row in ws.iter_rows(max_row=min(ws.max_row or 10000, 10000),
                                max_col=min(ws.max_column or 100, 100), values_only=True):
            if any(v is not None for v in row):
                line = ",".join("" if v is None else str(v) for v in row)
                lines.append(line)
                total += len(line)
                if total > MAX_TEXT_CHARS:
                    break
        if sum(map(len, lines)) > MAX_TEXT_CHARS:
            break
    wb.close()
    return [_cut("\n".join(lines)) + "\n[Limited to 20 sheets, 10,000 rows and 100 columns per sheet.]"]


def _as_text(data: bytes) -> str | None:
    if b"\x00" in data[:4096]:
        return None
    for enc in ("utf-8-sig", "cp1256"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return None


def read_file(data: bytes, name: str, mime: str, start_page: int = 1) -> list | None:
    """Turn a file into things Claude can read: text and/or pictures.
    Returns None for kinds of files it can't open (zip, apk, ...)."""
    ext = Path(name).suffix.lower()
    mime = (mime or "").lower()
    if (mime.startswith("image/") and mime != "image/svg+xml") or ext in IMAGE_EXTS:
        try:
            return [_to_image(data)]
        except Exception:
            return None
    if ext == ".pdf" or mime == "application/pdf":
        return _read_pdf(data, start_page)
    if ext == ".docx":
        return _read_docx(data)
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(data)
    if mime.startswith("text/") or ext in TEXT_EXTS or mime in ("application/json", "application/xml"):
        text = _as_text(data)
        return [_cut(text) if text.strip() else "The file is empty."] if text is not None else None
    # Unknown kind: still show it if it turns out to be plain text.
    text = _as_text(data) if len(data) < 2_000_000 else None
    if text and text.strip():
        sample = text[:5000]
        readable = sum(c.isprintable() or c in "\n\r\t" for c in sample)
        if readable >= 0.97 * len(sample):
            return [_cut(text)]
    return None


# ---------------------------------------------------------------- voice messages
async def _thumb(client: TelegramClient, m) -> bytes | None:
    try:
        return await client.download_media(m, file=bytes, thumb=-1)
    except (RPCError, ValueError, TypeError):
        return None


async def _speech_to_text(data: bytes, name: str, language: str | None) -> str:
    if name.lower().endswith(".oga"):  # Telegram's voice files; the services want .ogg
        name = name[:-4] + ".ogg"
    form = {"model": TRANSCRIBE_MODEL}
    if language:
        form["language"] = language
    async with httpx.AsyncClient(timeout=180) as http:
        r = await http.post(TRANSCRIBE_URL, data=form,
                            headers={"Authorization": f"Bearer {TRANSCRIBE_API_KEY}"},
                            files={"file": (name, data)})
    if r.status_code >= 400:
        raise RuntimeError(f"error {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


async def _transcribe(client: TelegramClient, entity, m, name: str,
                      language: str | None = None) -> tuple[str | None, str]:
    """Write out what's said in a voice message, round video or audio file.
    Telegram (Premium) goes first, unless a language is asked for and a speech-to-text
    service is set up: then that service gets the language as a hint.
    Returns (text, who wrote it out) or (None, why it couldn't)."""
    why = ""
    if (m.voice or m.video_note) and not (language and TRANSCRIBE_API_KEY):
        try:
            r = await client(TranscribeAudioRequest(peer=entity, msg_id=m.id))
            for _ in range(20):
                if not r.pending:
                    break
                await asyncio.sleep(1.5)
                r = await client(TranscribeAudioRequest(peer=entity, msg_id=m.id))
            if not r.pending and (r.text or "").strip():
                return r.text.strip(), "Telegram"
            why = "Telegram took too long to write it out."
        except RPCError as e:
            why = ("Telegram only writes out voice messages for Premium accounts."
                   if "PREMIUM" in type(e).__name__.upper() or "PREMIUM" in str(e).upper()
                   else f"Telegram couldn't write it out ({type(e).__name__}).")
    if not TRANSCRIBE_API_KEY:
        return None, (f"{why} I can't listen to audio myself. To fix this, put a "
                      "TRANSCRIBE_API_KEY in the connector's .env file (see README).").strip()
    if m.file.size and m.file.size > 25 * 1024 * 1024:
        return None, f"{why} It's too long for the speech-to-text service (25 MB max).".strip()
    data = await client.download_media(m, file=bytes)
    try:
        return await _speech_to_text(data, name, language), "speech-to-text"
    except (httpx.HTTPError, RuntimeError) as e:
        return None, f"{why} The speech-to-text service failed: {e}".strip()


# ---------------------------------------------------------------- getting files to send
def _check_public(host: str, infos) -> None:
    if not infos:
        raise ValueError("That hostname has no public addresses.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"{host} is a private or local address. Only public web links work.")


async def _fetch_url(url: str) -> tuple[bytes, str]:
    """Download a public web link (max MAX_UPLOAD_MB). Returns the bytes and a file name."""
    limit = MAX_UPLOAD_MB * 1024 * 1024
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False,
                                 headers={"User-Agent": "Mozilla/5.0"}) as http:
        for _ in range(6):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ValueError("Only http:// and https:// links work.")
            if p.username or p.password:
                raise ValueError("Links containing login credentials are not supported.")
            infos = await loop.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
            _check_public(p.hostname, infos)
            # Connect to the address we checked, not a second DNS lookup which can change.
            original = httpx.URL(url)
            pinned = original.copy_with(host=infos[0][4][0])
            async with http.stream("GET", pinned, headers={"Host": original.netloc.decode("ascii")},
                                   extensions={"sni_hostname": original.raw_host.decode("ascii")}) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers["location"])
                    continue
                if r.status_code >= 400:
                    raise ValueError(f"The link answered with error {r.status_code}.")
                if int(r.headers.get("content-length") or 0) > limit:
                    raise ValueError(f"That file is bigger than {MAX_UPLOAD_MB} MB.")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > limit:
                        raise ValueError(f"That file is bigger than {MAX_UPLOAD_MB} MB.")
                cd = r.headers.get("content-disposition", "")
                found = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
                name = unquote(found.group(1)) if found else unquote(Path(p.path).name)
                if not Path(name).suffix:
                    ctype = r.headers.get("content-type", "").split(";")[0].strip()
                    name = (name or "file") + (mimetypes.guess_extension(ctype) or "")
                return bytes(buf), name or "file"
    raise ValueError("That link redirects too many times.")


# ---------------------------------------------------------------- MCP server
mcp = FastMCP(
    name="Telegram",
    log_level="WARNING",  # Request URLs contain the connector's bearer secret.
    instructions=(
        "These tools act as the user's OWN personal Telegram account, not a bot. "
        "Anything sent appears as written by the user. Before calling send_message, "
        "send_file, edit_message, forward_messages or delete_messages, always show the "
        "user exactly what will happen (the chat, the exact text or file, which messages) "
        "and only go ahead after they clearly say yes. Never send to people the user did "
        "not name. Only open attachments the user asked about. Reading chats with these "
        "tools does not mark them as read; mark_read does, and only when the user asks. "
        "Times (schedule_at, since, until) are the user's local time; whoami tells the "
        "current local time. call_me and text_me only ever reach the user themself, so "
        "when they ask to be called or texted by their assistant, just do it."
    ),
    host="0.0.0.0",
    port=PORT,
    streamable_http_path=f"/{SECRET}/mcp",
    stateless_http=True,
    json_response=True,
    # Host checks are done by the HTTPS proxy in front; the secret path is the lock.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                              idempotentHint=False, openWorldHint=True)


@mcp.tool(annotations=READ)
async def whoami() -> dict:
    """Show which Telegram account this connector is logged in as, and the user's
    current local time (use it to work out times for schedule_at)."""
    me = await (await tg()).get_me()
    return {
        "name": utils.get_display_name(me),
        "username": f"@{me.username}" if me.username else None,
        "id": me.id,
        "premium": bool(me.premium),
        "now": _when(datetime.now(TZ)),
        "timezone": str(TZ),
    }


@mcp.tool(annotations=READ)
async def list_chats(limit: int = 30, unread_only: bool = False) -> list[dict]:
    """List the user's most recent Telegram chats (people, groups, channels),
    newest first, with unread counts and a preview of the last message.
    limit: how many chats (1-100). unread_only: only chats with unread messages."""
    client = await tg()
    limit = max(1, min(int(limit), 100))
    out = []
    async for d in client.iter_dialogs(limit=None if unread_only else limit):
        if unread_only and d.unread_count == 0:
            continue
        last = d.message
        preview = ""
        if last is not None:
            preview = f"{_media_label(last)} {(last.message or '')}".strip()[:80]
        out.append({
            "name": d.name,
            "id": d.id,
            "type": _kind(d.entity),
            "unread": d.unread_count,
            "last_message": preview,
            "last_time": _when(d.date),
        })
        if len(out) >= limit:
            break
    return out


@mcp.tool(annotations=READ)
async def find_chats(query: str, limit: int = 15) -> list[dict]:
    """Find chats or contacts by name or @username. Also finds public
    usernames you have never chatted with if you pass the full @username."""
    client = await tg()
    q = query.strip().lstrip("@").lower()
    if not q:
        raise ValueError("Give a name or @username to look for.")
    out = []
    async for d in client.iter_dialogs(limit=1000):
        ent = d.entity
        username = (getattr(ent, "username", None) or "").lower()
        if q in (d.name or "").lower() or (username and q in username):
            out.append({
                "name": d.name,
                "id": d.id,
                "username": f"@{ent.username}" if getattr(ent, "username", None) else None,
                "type": _kind(ent),
            })
            if len(out) >= limit:
                return out
    if not out and query.strip().startswith("@"):
        try:
            ent = await client.get_entity(query.strip())
            out.append({
                "name": utils.get_display_name(ent),
                "id": utils.get_peer_id(ent),
                "username": query.strip(),
                "type": _kind(ent),
            })
        except (ValueError, RPCError):
            pass
    return out


@mcp.tool(annotations=READ)
async def read_messages(chat: str, limit: int = 20, since: str | None = None,
                        until: str | None = None, before_message_id: int | None = None) -> dict:
    """Read messages in one chat, oldest first. Without since/until you get the latest ones.
    chat: a chat name, @username, +phone number, chat id, or 'me' for Saved Messages.
    limit: how many messages (1-100). Does NOT mark them as read.
    since / until: only messages in this time range, in the user's local time, like
    "2026-03-01" or "2026-03-01 18:00". A bare date for until includes that whole day.
    before_message_id: go further back, starting just before this message.
    Messages with "attachment": true have a file you can open with read_attachment."""
    client = await tg()
    entity = await resolve_chat(client, chat)
    limit = max(1, min(int(limit), 100))
    start = _parse_time(since) if since else None
    kw = {}
    if until:
        kw["offset_date"] = _parse_time(until, end_of_day=True)
    if before_message_id:
        kw["offset_id"] = int(before_message_id)
    msgs, more = [], False
    async for m in client.iter_messages(entity, limit=limit + 1, **kw):
        if start and m.date < start:
            break
        if len(msgs) == limit:
            more = True
            break
        msgs.append(m)
    msgs.reverse()
    out = {"chat": utils.get_display_name(entity), "messages": [_fmt(m) for m in msgs]}
    if more:
        out["older"] = (f"There are older messages{' in this range' if start else ''}: "
                        f"call again with before_message_id={msgs[0].id}.")
    return out


@mcp.tool(annotations=READ)
async def search_messages(query: str, chat: str | None = None, limit: int = 20) -> list[dict]:
    """Search message text. If chat is given, search only that chat;
    otherwise search across all chats. limit: 1-50 results."""
    client = await tg()
    limit = max(1, min(int(limit), 50))
    entity = await resolve_chat(client, chat) if chat else None
    out = []
    async for m in client.iter_messages(entity, search=query, limit=limit):
        item = _fmt(m)
        if entity is None:
            chat_ent = await m.get_chat()
            item["chat"] = utils.get_display_name(chat_ent) if chat_ent else ""
            item["chat_id"] = m.chat_id
        out.append(item)
    return out


@mcp.tool(annotations=READ, structured_output=False)
async def get_profile(chat: str, include_photo: bool = True) -> list:
    """Show someone's profile: name, @username, phone (if they share it), bio, birthday,
    when they were last online, whether they're a contact, and their profile picture.
    For groups and channels: description and member count."""
    client = await tg()
    entity = await resolve_chat(client, chat)
    info = {"name": utils.get_display_name(entity), "id": utils.get_peer_id(entity),
            "type": _kind(entity)}
    names = [f"@{u.username}" for u in (getattr(entity, "usernames", None) or []) if u.active]
    if getattr(entity, "username", None):
        names.insert(0, f"@{entity.username}")
    if names:
        info["usernames"] = list(dict.fromkeys(names))
    if isinstance(entity, User):
        full = (await client(GetFullUserRequest(entity))).full_user
        info.update({
            "phone": f"+{entity.phone}" if entity.phone else "hidden",
            "bio": full.about or "",
            "last_seen": _last_seen(entity.status),
            "in_your_contacts": bool(entity.contact),
            "mutual_contact": bool(entity.mutual_contact),
            "groups_in_common": full.common_chats_count,
            "premium": bool(entity.premium),
        })
        if full.birthday:
            b = full.birthday
            info["birthday"] = f"{b.day:02d}/{b.month:02d}" + (f"/{b.year}" if b.year else "")
        for flag in ("verified", "scam", "fake", "bot"):
            if getattr(entity, flag, False):
                info[flag] = True
    elif isinstance(entity, Channel):
        full = (await client(GetFullChannelRequest(entity))).full_chat
        info.update({"about": full.about or "", "members": full.participants_count})
    elif isinstance(entity, Chat):
        full = (await client(GetFullChatRequest(entity.id))).full_chat
        info.update({"about": full.about or "", "members": entity.participants_count})
    out = [json.dumps(info, ensure_ascii=False, indent=1)]
    if include_photo:
        try:
            photo = await client.download_profile_photo(entity, file=bytes)
        except (RPCError, ValueError, TypeError):
            photo = None
        out.append(_to_image(photo) if photo else "No profile picture (or it's hidden).")
    return out


@mcp.tool(annotations=READ, structured_output=False)
async def read_attachment(chat: str, message_id: int, start_page: int = 1,
                          language: str | None = None) -> list:
    """Open the file, photo or other attachment of one message and show what is in it.
    Photos, screenshots and stickers come back as pictures. PDFs, Word (.docx),
    Excel (.xlsx), text, CSV and code files come back as text (scanned PDFs as pictures).
    Voice messages, round videos and audio files come back written out as text
    (Persian, English, Turkish and most other languages work).
    Videos and GIFs can't be played: you get their preview picture instead.
    Only open attachments the user asked about.
    chat: like read_messages. message_id: the message's id from read_messages.
    start_page: for long PDFs, the page to start from.
    language: for voice/audio, a language code like "fa", "en" or "tr". Leave it out
    (the language is detected); pass it to try again when the text came out wrong."""
    client = await tg()
    entity = await resolve_chat(client, chat)
    m = await _get_message(client, entity, message_id)
    if not _has_file(m):
        raise ValueError("That message has no file or photo attached.")
    f = m.file
    name = f.name or f"{_media_label(m).strip('[]').split(':')[0]}{f.ext or ''}"
    head = (f"{_media_label(m)} from {_fmt(m)['from']} at {_when(m.date)}: {name} "
            f"({f.mime_type or 'unknown type'}, {_size(f.size)}).")
    if m.message:
        head += f"\nCaption: {m.message}"

    if m.voice or m.audio or m.video_note:
        heard, how = await _transcribe(client, entity, m, name, (language or "").strip() or None)
        out = [head + (f"\nWhat's said (written out by {how}):\n{heard}" if heard else f"\n{how}")]
        thumb = await _thumb(client, m) if m.video_note else None
        if thumb:
            out.append(_to_image(thumb))
        return out
    if m.video or m.gif or (m.sticker and (f.mime_type or "") != "image/webp"):
        thumb = await _thumb(client, m)
        if thumb:
            return [head + "\nI can't play this, but here is its preview picture.", _to_image(thumb)]
        return [head + "\nI can't play video, and it has no preview picture."]

    if f.size and f.size > MAX_ATTACHMENT_MB * 1024 * 1024:
        return [head + f"\nIt's bigger than {MAX_ATTACHMENT_MB} MB, so I didn't open it."]
    data = await client.download_media(m, file=bytes)
    parts = read_file(data, name, f.mime_type or "", start_page)
    if parts is None:
        return [head + "\nI can't open this kind of file. I can read pictures, PDFs, "
                       "Word, Excel, text and code files."]
    return [head, *parts]


@mcp.tool(annotations=WRITE)
async def send_message(chat: str, text: str, reply_to_message_id: int | None = None,
                       schedule_at: str | None = None) -> dict:
    """Send a text message FROM THE USER'S OWN Telegram account.
    Only call this after showing the user the exact recipient and text (and the time, if
    scheduled) and getting a clear yes.
    chat: a chat name, @username, +phone number, chat id, or 'me' for Saved Messages.
    reply_to_message_id: optional id of a message to reply to (from read_messages).
    schedule_at: optional time to send it later, in the user's local time, like
    "2026-11-03 08:00". Telegram itself sends it then, even if nothing else is running."""
    text = (text or "").strip()
    if not text:
        raise ValueError("The message is empty.")
    if len(text) > 4096:
        raise ValueError("Telegram messages can be at most 4096 characters. Split it up.")
    when = _schedule_time(schedule_at) if schedule_at else None
    _check_send_limit()
    client = await tg()
    entity = await resolve_chat(client, chat)
    try:
        sent = await client.send_message(entity, text, reply_to=reply_to_message_id, schedule=when)
    except FloodWaitError as e:
        raise _slow_down(e)
    _record_sends()
    log.debug("message %s", "scheduled" if when else "sent")
    out = {
        "sent": not when,
        "to": utils.get_display_name(entity),
        "message_id": sent.id,
    }
    out["scheduled_for" if when else "time"] = _when(sent.date)
    return out


def _read_whatsapp_file(source: str) -> str:
    """Read a downloaded WhatsApp file, confined to the optional store mount."""
    prefix = "/home/app/store/"
    if not source.startswith(prefix):
        raise ValueError("whatsapp_file must be the /home/app/store/ path returned by WhatsApp download_media.")
    if not WHATSAPP_STORE.is_dir():
        raise ValueError(
            "The WhatsApp store isn't connected. In telegram-connector/.env, set "
            "COMPOSE_FILE=docker-compose.yml:docker-compose.whatsapp.yml, then run docker compose up -d "
            "from telegram-connector. See README.md for WhatsApp files."
        )
    root = WHATSAPP_STORE.resolve()
    path = (root / source[len(prefix):]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("That WhatsApp path is outside the store.")
    limit = MAX_WHATSAPP_MB * 1024 * 1024
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("That WhatsApp path is not a regular file.")
        if info.st_size > limit:
            raise ValueError(f"That file is bigger than {MAX_WHATSAPP_MB} MB.")
        with path.open("rb") as downloaded:
            downloaded.read(1)
    except FileNotFoundError:
        raise ValueError("That WhatsApp file wasn't found. Download it again with WhatsApp download_media.") from None
    except PermissionError:
        raise ValueError(
            "The Telegram container cannot read that WhatsApp file. Grant its user read access "
            "to the file and search access to the store directories (WhatsApp uses uid 10001). "
            "See README.md for WhatsApp files."
        ) from None
    # Telethon streams the file from this path and keeps its name.
    return str(path)


@mcp.tool(annotations=WRITE)
async def send_file(
    chat: str,
    caption: str | None = None,
    url: str | None = None,
    text_content: str | None = None,
    file_name: str | None = None,
    from_chat: str | None = None,
    from_message_id: int | None = None,
    as_document: bool = False,
    reply_to_message_id: int | None = None,
    schedule_at: str | None = None,
    whatsapp_file: str | None = None,
) -> dict:
    """Send a file or photo FROM THE USER'S OWN Telegram account.
    Only call this after showing the user the recipient, the file and the caption, and
    getting a clear yes. Give exactly ONE source:
      url: a public web link to download and send (max 50 MB), or
      text_content + file_name: a file you write yourself (notes.txt, list.csv, page.html...), or
      from_chat + from_message_id: re-send the attachment of another message, without
        the "forwarded from" label, or
      whatsapp_file: the exact /home/app/store/ path from WhatsApp download_media
        (up to 2 GB; requires the optional WhatsApp store mount). Keeps the filename.
    caption: optional text under the file (max 1024 characters).
    as_document: send pictures as files (full quality) instead of as photos.
    schedule_at: optional time to send it later, like send_message."""
    sources = [url is not None, text_content is not None,
               from_chat is not None or from_message_id is not None, whatsapp_file is not None]
    if sum(sources) != 1:
        raise ValueError("Give exactly one source: url, text_content, from_chat + from_message_id, or whatsapp_file.")
    caption = (caption or "").strip()
    if len(caption) > 1024:
        raise ValueError("A caption can be at most 1024 characters.")
    when = _schedule_time(schedule_at) if schedule_at else None
    _check_send_limit()
    client = await tg()
    entity = await resolve_chat(client, chat)

    if url is not None:
        data, name = await _fetch_url(url.strip())
        file = io.BytesIO(data)
        file.name = (file_name or name).strip()
    elif text_content is not None:
        if not text_content.strip():
            raise ValueError("The file would be empty.")
        file = io.BytesIO(text_content.encode("utf-8"))
        file.name = (file_name or "note.txt").strip()
    elif whatsapp_file is not None:
        file = _read_whatsapp_file(whatsapp_file)
    else:
        if not (from_chat and from_message_id):
            raise ValueError("Give both from_chat and from_message_id.")
        src = await _get_message(client, await resolve_chat(client, from_chat), from_message_id)
        if not _has_file(src):
            raise ValueError("That message has no file or photo to send.")
        file = src.media

    try:
        sent = await client.send_file(entity, file, caption=caption or None, schedule=when,
                                      force_document=as_document, reply_to=reply_to_message_id)
    except FloodWaitError as e:
        raise _slow_down(e)
    _record_sends()
    log.debug("file %s", "scheduled" if when else "sent")
    out = {
        "sent": not when,
        "to": utils.get_display_name(entity),
        "message_id": sent.id,
        "what": _media_label(sent),
    }
    out["scheduled_for" if when else "time"] = _when(sent.date)
    return out


@mcp.tool(annotations=READ)
async def list_scheduled(chat: str) -> dict:
    """List the messages waiting to be sent later in one chat, with when each one goes out."""
    client = await tg()
    entity = await resolve_chat(client, chat)
    msgs = [m async for m in client.iter_messages(entity, scheduled=True)]
    msgs.sort(key=lambda m: m.date)
    return {"chat": utils.get_display_name(entity),
            "scheduled": [{"id": m.id, "sends_at": _when(m.date),
                           "text": f"{_media_label(m)} {m.message or ''}".strip()} for m in msgs]}


@mcp.tool(annotations=DESTRUCTIVE)
async def cancel_scheduled(chat: str, message_ids: list[int]) -> dict:
    """Cancel scheduled messages so they are never sent (ids from list_scheduled).
    Only call this after showing the user which ones and getting a clear yes."""
    ids = [int(i) for i in message_ids][:100]
    if not ids:
        raise ValueError("Say which scheduled messages to cancel (ids from list_scheduled).")
    client = await tg()
    entity = await resolve_chat(client, chat)
    waiting = {m.id async for m in client.iter_messages(entity, scheduled=True)}
    found = [i for i in ids if i in waiting]
    if not found:
        raise ValueError("None of those are scheduled in that chat. Use list_scheduled.")
    await client(DeleteScheduledMessagesRequest(peer=entity, id=found))
    log.debug("cancelled %s scheduled messages", len(found))
    return {"cancelled": found, "not_found": [i for i in ids if i not in waiting],
            "chat": utils.get_display_name(entity)}


@mcp.tool(annotations=WRITE)
async def edit_message(chat: str, message_id: int, new_text: str) -> dict:
    """Change the text of a message the user sent (or the caption of a file they sent).
    Only call this after showing the user the old and the new text and getting a clear yes.
    Only the user's own messages can be edited."""
    new_text = (new_text or "").strip()
    if not new_text:
        raise ValueError("The new text is empty. To remove a message, use delete_messages.")
    client = await tg()
    entity = await resolve_chat(client, chat)
    m = await _get_message(client, entity, message_id)
    if not m.out:
        raise ValueError("That message was written by someone else. You can only edit your own.")
    limit = 1024 if _has_file(m) else 4096
    if len(new_text) > limit:
        raise ValueError(f"That's too long: at most {limit} characters here.")
    _check_send_limit()
    try:
        await client.edit_message(entity, m.id, new_text)
    except MessageNotModifiedError:
        raise ValueError("The new text is the same as the old text.")
    except (MessageEditTimeExpiredError, MessageAuthorRequiredError):
        raise ValueError("Telegram doesn't allow editing this message anymore.")
    except FloodWaitError as e:
        raise _slow_down(e)
    _record_sends()
    log.debug("message edited")
    return {"edited": True, "chat": utils.get_display_name(entity), "message_id": m.id,
            "old_text": m.message or "", "new_text": new_text}


@mcp.tool(annotations=WRITE)
async def forward_messages(
    from_chat: str, message_ids: list[int], to_chat: str, hide_sender: bool = False
) -> dict:
    """Forward messages (with their files) from one chat to another, at most 20 at once.
    Only call this after showing the user which messages go where and getting a clear yes.
    hide_sender: send them as copies, without the "forwarded from" label."""
    ids = [int(i) for i in message_ids][:20]
    if not ids:
        raise ValueError("Say which messages to forward (their ids from read_messages).")
    _check_send_limit(len(ids))
    client = await tg()
    source = await resolve_chat(client, from_chat)
    target = await resolve_chat(client, to_chat)
    try:
        sent = await client.forward_messages(target, ids, from_peer=source,
                                             drop_author=hide_sender or None)
    except FloodWaitError as e:
        raise _slow_down(e)
    sent = [s for s in (sent if isinstance(sent, list) else [sent]) if s]
    _record_sends(len(sent))
    log.debug("forwarded %s messages", len(sent))
    return {"forwarded": len(sent), "from": utils.get_display_name(source),
            "to": utils.get_display_name(target), "new_message_ids": [s.id for s in sent]}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_messages(chat: str, message_ids: list[int], for_everyone: bool = True) -> dict:
    """Delete messages, at most 50 at once. This can't be undone.
    Only call this after showing the user exactly which messages will be deleted
    and getting a clear yes.
    for_everyone: also delete them for the other person (in private chats and in groups
    where the user may). False deletes only on the user's side."""
    ids = [int(i) for i in message_ids][:50]
    if not ids:
        raise ValueError("Say which messages to delete (their ids from read_messages).")
    client = await tg()
    entity = await resolve_chat(client, chat)
    found = [m for m in await client.get_messages(entity, ids=ids) if m]
    if not found:
        raise ValueError("None of those messages exist in that chat.")
    try:
        done = await client.delete_messages(entity, [m.id for m in found], revoke=for_everyone)
    except FloodWaitError as e:
        raise _slow_down(e)
    count = sum(getattr(d, "pts_count", 0) for d in done)
    log.debug("deleted %s messages", count)
    return {"deleted": count, "chat": utils.get_display_name(entity),
            "for_everyone": for_everyone,
            "messages": [_fmt(m)["text"][:80] for m in found]}


@mcp.tool(annotations=WRITE)
async def call_me(message: str | None = None, schedule_at: str | None = None) -> dict:
    """Ring the user's phone: their AI assistant (a second Telegram account on their server)
    calls them on Telegram, says `message` out loud, then keeps talking with them by voice.
    Use it when the user asks to be called, now or later (schedule_at, like send_message,
    e.g. a wake-up call or a reminder). It only ever calls the user themself.
    If they don't pick up, the message reaches them as text instead."""
    out = {"calling": not schedule_at, "message": (message or "").strip()}
    return out | await _tell_assistant("/call", message, schedule_at)


@mcp.tool(annotations=WRITE)
async def text_me(message: str, schedule_at: str | None = None) -> dict:
    """Send the user a Telegram message FROM THEIR AI ASSISTANT's account (the second account
    that also calls them), now or later (schedule_at, like send_message). Use it for reminders,
    notes and alerts the user asked to get from their assistant. It only ever texts the user
    themself, so when they ask for it, just do it."""
    if not (message or "").strip():
        raise ValueError("The message is empty.")
    out = {"texting": not schedule_at, "message": message.strip()}
    return out | await _tell_assistant("/text", message, schedule_at)


@mcp.tool(annotations=WRITE)
async def update_server() -> dict:
    """Update the user's server now: the assistant account runs the update of every project that
    has automatic updates turned on (only versions whose tests passed on GitHub), and texts the
    user the result. Use it when the user asks to update the server or the connectors."""
    return {"updating": True} | await _tell_assistant("/update", None, None)


async def _tell_assistant(command: str, message: str | None, schedule_at: str | None) -> dict:
    """Send a command to the call assistant's account from the user's account."""
    if not CALLER:
        raise ValueError("The call assistant isn't set up yet: CALLER is missing in the "
                         "connector's .env (telegram-caller's setup.sh adds it).")
    when = _schedule_time(schedule_at) if schedule_at else None
    _check_send_limit()
    client = await tg()
    target = await resolve_chat(client, CALLER)
    text = f"{command} {(message or '').strip()}".strip()
    if len(text) > 4096:
        raise ValueError("That's too long for one message: at most about 4000 characters.")
    try:
        sent = await client.send_message(target, text, schedule=when)
    except FloodWaitError as e:
        raise _slow_down(e)
    _record_sends()
    log.info("%s %s", "scheduled" if when else "sent", command)
    return {"scheduled_for": _when(sent.date)} if when else {}


@mcp.tool(annotations=WRITE)
async def mark_read(chat: str) -> dict:
    """Mark all messages in a chat as read (the other person sees the blue ticks).
    Only when the user asks for it."""
    client = await tg()
    entity = await resolve_chat(client, chat)
    await client.send_read_acknowledge(entity)
    return {"marked_read": True, "chat": utils.get_display_name(entity)}


if __name__ == "__main__":
    log.info("Telegram connector listening on port %s", PORT)
    mcp.run(transport="streamable-http")
