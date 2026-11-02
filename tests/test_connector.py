"""
Offline tests for the Telegram connector: a fake Telegram account, real file reading.

    pip install -r telegram-connector/requirements.txt
    python tests/test_connector.py

Needs internet only for the last check (downloading a public link).
"""
import asyncio, io, os, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

os.environ["MCP_SECRET"] = "a" * 32
os.environ["DATA_DIR"] = tempfile.gettempdir()
os.environ["CALLER"] = "@ai_helper"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-connector"))
import server  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
import pymupdf, docx, openpyxl  # noqa: E402
from telethon.errors import PremiumAccountRequiredError  # noqa: E402
from telethon.tl.functions.messages import (DeleteScheduledMessagesRequest,  # noqa: E402
                                            TranscribeAudioRequest)
from telethon.tl.functions.users import GetFullUserRequest  # noqa: E402
from telethon.tl.types import Birthday, User, UserStatusOffline  # noqa: E402
from telethon.tl.types.messages import TranscribedAudio  # noqa: E402

ok = 0
def check(cond, what):
    global ok
    if not cond:
        raise SystemExit(f"FAIL: {what}")
    ok += 1
    print("ok -", what)

# ---------- files
def png_rgba(w=3000, h=2000):
    b = io.BytesIO(); PILImage.new("RGBA", (w, h), (255, 0, 0, 128)).save(b, "PNG"); return b.getvalue()

def pdf_text(pages=3, words="Hello Telegram connector. " * 20):
    d = pymupdf.open()
    for i in range(pages):
        d.new_page().insert_text((50, 72), f"Page {i+1}: {words}")
    return d.tobytes()

def pdf_scanned(pages=7):
    d = pymupdf.open()
    for _ in range(pages):
        d.new_page().insert_image(pymupdf.Rect(0, 0, 500, 500), stream=png_rgba(400, 400))
    return d.tobytes()

def docx_bytes():
    d = docx.Document(); d.add_paragraph("First paragraph"); t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "A"; t.rows[0].cells[1].text = "B"
    b = io.BytesIO(); d.save(b); return b.getvalue()

def xlsx_bytes():
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Money"; ws.append(["item", "cost"]); ws.append(["tea", 3])
    b = io.BytesIO(); wb.save(b); return b.getvalue()

r = server.read_file(png_rgba(), "x.png", "image/png")
check(r[0].to_image_content().mimeType == "image/jpeg", "PNG with transparency becomes JPEG")
check(max(PILImage.open(io.BytesIO(r[0].data)).size) == 1568, "big picture shrunk to 1568px")
r = server.read_file(pdf_text(), "a.pdf", "application/pdf")
check("PDF with 3 pages" in r[0] and "Hello Telegram" in r[1] and "--- page 3 ---" in r[1], "text PDF read")
big = pdf_text(pages=800)
r = server.read_file(big, "big.pdf", "application/pdf")
hint = [p for p in r if isinstance(p, str) and "start_page=" in p]
check(hint, "long PDF says where to continue")
nxt = int(hint[0].split("start_page=")[1].rstrip("."))
check(f"--- page {nxt} ---" in server.read_file(big, "big.pdf", "application/pdf", nxt)[1], "start_page continues the PDF")
r = server.read_file(pdf_scanned(), "scan.pdf", "application/pdf")
check("scanned" in r[0] and sum(isinstance(p, server.Image) for p in r) == 5, "scanned PDF shown as 5 pictures")
r = server.read_file(docx_bytes(), "w.docx", "application/vnd.openxmlformats")
check("First paragraph" in r[0] and "A | B" in r[0], "Word file read with table")
r = server.read_file(xlsx_bytes(), "s.xlsx", "")
check("sheet: Money" in r[0] and "tea,3" in r[0], "Excel file read")
check("یادداشت" in server.read_file("سلام، این یک یادداشت است".encode(), "n.txt", "text/plain")[0], "UTF-8 Persian text read")
check("سلام" in server.read_file("سلام".encode("cp1256"), "old.txt", "text/plain")[0], "old Windows Persian text read")
check(server.read_file(b"PK\x03\x04\x00\x00junk", "a.zip", "application/zip") is None, "zip refused")
r = server.read_file(b"x" * 100_000, "big.log", "text/plain")
check(len(r[0]) < 61_000 and "cut off" in r[0], "long text cut off")

# ---------- time helpers
t = server._parse_time("2026-11-03 08:00")
check(t.hour == 8 and str(t.tzinfo) == str(server.TZ), "times read as local time")
check(server._parse_time("2026-11-03", end_of_day=True).day == 4, "bare 'until' date includes the whole day")
try:
    server._schedule_time("2020-01-01 10:00"); check(False, "past schedule refused")
except ValueError as e:
    check("not in the future" in str(e), "past schedule refused")

# ---------- fake telegram
NOW = datetime.now(timezone.utc)

def msg(id, data=None, name=None, mime=None, out=True, text="", date=None, **kinds):
    f = SimpleNamespace(name=name, ext=os.path.splitext(name)[1] if name else ".jpg", mime_type=mime,
                        size=len(data) if data else 0) if data is not None else None
    base = dict(photo=None, voice=None, video_note=None, video=None, sticker=None, gif=None,
                audio=None, document=None, geo=None, poll=None, web_preview=None)
    base.update(kinds)
    return SimpleNamespace(id=id, media=object() if f else None, file=f, out=out, sender=None,
                           message=text, date=date or NOW, reply_to=None, _data=data, **base)

class FakeClient:
    def __init__(self, msgs):
        self.msgs = {m.id: m for m in msgs}; self.calls = []; self.transcribe_polls = 0
        self.history = [msg(i, text=f"m{i}", date=NOW - timedelta(hours=300 - i)) for i in range(1, 301)]
        self.scheduled = [msg(900, text="good morning", date=NOW + timedelta(days=1))]
        self.premium = True
    async def __call__(self, req):
        self.calls.append(type(req).__name__)
        if isinstance(req, TranscribeAudioRequest):
            if not self.premium:
                raise PremiumAccountRequiredError(request=req)
            self.transcribe_polls += 1
            return TranscribedAudio(transcription_id=1, text="سلام عزیزم، کجایی؟",
                                    pending=self.transcribe_polls < 2)
        if isinstance(req, DeleteScheduledMessagesRequest):
            self.scheduled = [m for m in self.scheduled if m.id not in req.id]; return None
        if isinstance(req, GetFullUserRequest):
            return SimpleNamespace(full_user=SimpleNamespace(about="Engineer 🌻", common_chats_count=3,
                                                             birthday=Birthday(day=5, month=3)))
        raise AssertionError(req)
    async def get_messages(self, entity, ids):
        return [self.msgs.get(i) for i in ids] if isinstance(ids, list) else self.msgs.get(ids)
    async def iter_messages(self, entity, limit=None, offset_date=None, offset_id=0, scheduled=False):
        if scheduled:
            for m in self.scheduled:
                yield m
            return
        n = 0
        for m in reversed(self.history):
            if offset_id and m.id >= offset_id: continue
            if offset_date and m.date >= offset_date: continue
            if limit is not None and n >= limit: return
            n += 1; yield m
    async def download_media(self, m, file=None, thumb=None):
        return m._data if thumb is None else (png_rgba(200, 200) if (m.video or m.video_note) else None)
    async def download_profile_photo(self, entity, file=None):
        return png_rgba(640, 640)
    async def send_message(self, entity, text, reply_to=None, schedule=None):
        self.calls.append(("send", text, schedule)); return msg(901, text=text, date=schedule or NOW)
    async def send_file(self, entity, file, **kw):
        self.calls.append(("send_file", file, kw)); return msg(999, b"x", "n.txt", "text/plain", document=True,
                                                              date=kw.get("schedule") or NOW)
    async def edit_message(self, entity, id, text): self.calls.append(("edit", id, text))
    async def forward_messages(self, target, ids, from_peer=None, drop_author=None):
        self.calls.append(("fwd", ids, drop_author)); return [msg(500 + i, None) for i in ids]
    async def delete_messages(self, entity, ids, revoke=True):
        self.calls.append(("del", ids, revoke)); return [SimpleNamespace(pts_count=len(ids))]
    async def send_read_acknowledge(self, entity): self.calls.append(("read",))

me = User(id=1, first_name="Alex", username="alex_example", phone="905551112233", contact=True,
          premium=True, status=UserStatusOffline(was_online=NOW - timedelta(minutes=5)))
fake = FakeClient([
    msg(1, pdf_text(), "doc.pdf", "application/pdf", document=True, text="see this"),
    msg(2, png_rgba(800, 600), None, "image/jpeg", photo=True),
    msg(3, b"\x00" * 5000, "clip.mp4", "video/mp4", video=True, document=True),
    msg(4, b"PK\x03\x04junk", "app.apk", "application/vnd.android", document=True),
    msg(5, None, text="plain text"),
    msg(6, None, out=False, text="their message"),
    msg(7, b"\x00" * 10, "big.bin", "application/octet-stream", document=True),
    msg(8, b"OggS" + b"\x01" * 3000, "voice.oga", "audio/ogg", voice=True, document=True),
    msg(9, b"\x00" * 3000, "round.mp4", "video/mp4", video_note=True, document=True),
])
fake.msgs[7].file.size = 999 * 1024 * 1024

async def fake_tg(): return fake
async def fake_resolve(client, chat): return me
server.tg = fake_tg
server.resolve_chat = fake_resolve

async def call(name, **args):
    try:
        res = await server.mcp.call_tool(name, args)
    except Exception as e:
        return "ERROR: " + str(e)
    return res[0] if isinstance(res, tuple) else res

def text_of(c):
    return "\n".join(getattr(x, "text", "") for x in c) if isinstance(c, list) else str(c)

async def main():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    check(set(tools) >= {"read_attachment", "send_file", "edit_message", "forward_messages", "delete_messages",
                         "mark_read", "send_message", "list_scheduled", "cancel_scheduled", "get_profile"},
          f"all {len(tools)} tools listed")
    check(tools["delete_messages"].annotations.destructiveHint and tools["cancel_scheduled"].annotations.destructiveHint,
          "delete and cancel are marked destructive")

    # attachments
    c = await call("read_attachment", chat="me", message_id=1)
    check("doc.pdf" in c[0].text and "Caption: see this" in c[0].text and "Hello Telegram" in c[2].text, "reads a PDF")
    c = await call("read_attachment", chat="me", message_id=2)
    check([x.type for x in c] == ["text", "image"], "returns a photo as a picture")
    c = await call("read_attachment", chat="me", message_id=3)
    check(c[1].type == "image" and "preview picture" in c[0].text, "video gives its preview picture")
    c = await call("read_attachment", chat="me", message_id=4)
    check("can't open this kind" in c[0].text, "apk explained as unsupported")
    c = await call("read_attachment", chat="me", message_id=5)
    check("no file" in str(c), "message without a file refused")
    c = await call("read_attachment", chat="me", message_id=7)
    check("bigger than 20 MB" in c[0].text, "huge file not downloaded")

    # voice
    c = await call("read_attachment", chat="me", message_id=8)
    check("سلام عزیزم" in c[0].text and "by Telegram" in c[0].text and fake.transcribe_polls == 2,
          "Persian voice message written out by Telegram (waits while pending)")
    fake.transcribe_polls = 0
    c = await call("read_attachment", chat="me", message_id=9)
    check("سلام عزیزم" in c[0].text and c[1].type == "image", "round video: text plus preview picture")
    fake.premium = False
    c = await call("read_attachment", chat="me", message_id=8)
    check("Premium" in c[0].text and "TRANSCRIBE_API_KEY" in c[0].text, "no Premium and no key: explains how to fix")
    seen = {}
    async def fake_stt(data, name, language):
        seen.update(name=name, language=language); return "این یک پیام صوتی است"
    server._speech_to_text, server.TRANSCRIBE_API_KEY = fake_stt, "test-key"
    c = await call("read_attachment", chat="me", message_id=8)
    check("پیام صوتی" in c[0].text and "speech-to-text" in c[0].text and seen["language"] is None,
          "no Premium: falls back to speech-to-text")
    fake.premium, fake.transcribe_polls, before = True, 0, fake.calls.count("TranscribeAudioRequest")
    c = await call("read_attachment", chat="me", message_id=8, language="fa")
    check(seen["language"] == "fa" and fake.calls.count("TranscribeAudioRequest") == before,
          "language='fa' goes straight to speech-to-text with the Persian hint")
    server.TRANSCRIBE_API_KEY = ""

    # history
    c = await call("read_messages", chat="me", limit=20)
    r = c[0].text if isinstance(c, list) else str(c)
    import json
    d = json.loads(r)
    check(d["messages"][-1]["id"] == 300 and d["messages"][0]["id"] == 281 and "before_message_id=281" in d["older"],
          "latest 20 messages, with a pointer to older ones")
    d = json.loads((await call("read_messages", chat="me", limit=20, before_message_id=281))[0].text)
    check(d["messages"][-1]["id"] == 280 and d["messages"][0]["id"] == 261, "before_message_id pages backwards")
    since = server._when(NOW - timedelta(hours=10, minutes=30))
    d = json.loads((await call("read_messages", chat="me", limit=100, since=since))[0].text)
    check([m["id"] for m in d["messages"]] == list(range(290, 301)) and "older" not in d, "since keeps only newer messages")
    until = server._when(NOW - timedelta(hours=100, minutes=30))
    d = json.loads((await call("read_messages", chat="me", limit=5, until=until))[0].text)
    check(d["messages"][-1]["id"] == 199 and len(d["messages"]) == 5, "until starts from that time")

    # profile
    c = await call("get_profile", chat="@alex_example")
    p = json.loads(c[0].text)
    check(p["bio"] == "Engineer 🌻" and p["birthday"] == "05/03" and p["phone"] == "+905551112233"
          and p["last_seen"].startswith("last seen") and p["usernames"] == ["@alex_example"] and c[1].type == "image",
          "profile shows bio, birthday, phone, last seen and picture")

    # scheduled
    c = await call("send_message", chat="me", text="صبح بخیر", schedule_at=server._when(NOW + timedelta(days=1)))
    d = c if isinstance(c, dict) else json.loads(c[0].text)
    check(d["sent"] is False and "scheduled_for" in d and fake.calls[-1][2] is not None, "message scheduled for tomorrow")
    c = await call("send_message", chat="me", text="x", schedule_at="2020-01-01 08:00")
    check("not in the future" in str(c), "scheduling in the past refused")
    c = await call("send_file", chat="me", text_content="list", schedule_at=server._when(NOW + timedelta(hours=2)))
    check(fake.calls[-1][2]["schedule"] is not None, "file scheduled")
    d = json.loads((await call("list_scheduled", chat="me"))[0].text)
    check(d["scheduled"][0]["id"] == 900 and d["scheduled"][0]["text"] == "good morning", "lists scheduled messages")
    d = json.loads((await call("cancel_scheduled", chat="me", message_ids=[900, 12345]))[0].text)
    check(d["cancelled"] == [900] and d["not_found"] == [12345] and not fake.scheduled, "cancels a scheduled message")

    # writing tools
    c = await call("send_file", chat="me")
    check("exactly one source" in str(c), "send_file needs one source")
    await call("send_file", chat="me", text_content="سلام list", file_name="list.txt", caption="hi")
    kind, f, kw = fake.calls[-1]
    check(f.name == "list.txt" and f.getvalue().decode() == "سلام list" and kw["caption"] == "hi", "send_file sends a written file")
    await call("send_file", chat="me", from_chat="me", from_message_id=1)
    check(fake.calls[-1][1] is fake.msgs[1].media, "send_file re-sends another message's file")
    c = await call("send_file", chat="me", url="http://127.0.0.1:8000/x")
    check("private or local" in str(c), "send_file blocks local addresses")
    c = await call("edit_message", chat="me", message_id=6, new_text="hack")
    check("someone else" in str(c), "can't edit other people's messages")
    await call("edit_message", chat="me", message_id=5, new_text="fixed")
    check(fake.calls[-1] == ("edit", 5, "fixed"), "edits own message")
    await call("forward_messages", from_chat="me", message_ids=[1, 2], to_chat="me", hide_sender=True)
    check(fake.calls[-1] == ("fwd", [1, 2], True), "forwards with hidden sender")
    await call("delete_messages", chat="me", message_ids=[5, 404], for_everyone=False)
    check(fake.calls[-1] == ("del", [5], False), "deletes only existing messages, for me only")
    server._sent_times.clear()
    server._sent_times.extend([__import__("time").time()] * server.MAX_SENDS_PER_HOUR)
    check("Safety limit" in str(await call("send_file", chat="me", text_content="x")), "hourly limit also covers files")
    server._sent_times.clear()

    # call_me asks the call assistant's account to ring the user
    await call("call_me", message="Time for your meds")
    check(fake.calls[-1] == ("send", "/call Time for your meds", None), "call_me asks the assistant to call now")
    await call("call_me", message="بیدار شو", schedule_at=server._when(NOW + timedelta(hours=8)))
    check(fake.calls[-1][1] == "/call بیدار شو" and fake.calls[-1][2] is not None, "call_me can be scheduled")

    data, name = await server._fetch_url("https://www.google.com/robots.txt")
    check(name == "robots.txt" and b"User-agent" in data, "downloads a public link")

asyncio.run(main())
print(f"\nALL {ok} CHECKS PASSED")
