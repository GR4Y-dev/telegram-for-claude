"""
Phone calls on Telegram between you and your AI assistant.

Runs on the server as its own (second) Telegram account. It only talks to the
owner's account:
  - The owner calls this account: it answers.
  - The owner (or Claude, through the Telegram connector's call_me tool) sends this
    account "/call <message>": it calls the owner back and says the message.
On a call it takes turns: it listens until you stop talking, writes out what you
said (speech-to-text), asks Claude Code for an answer, and speaks it (Microsoft's
free voices, Persian and English). While it thinks you hear a soft tick.
"""

import asyncio
import io
import json
import logging
import math
import os
import re
import shutil
import time
import wave
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import edge_tts
import httpx
import numpy as np
from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import CallBusy, CallDeclined, CallDiscarded, TimedOutAnswer
from pytgcalls.types import (CallConfig, ChatUpdate, Device, Direction, ExternalMedia,
                             MediaStream, RecordStream, StreamFrames)
from pytgcalls.types.raw import AudioParameters
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ---------------------------------------------------------------- settings
BASE = Path(__file__).resolve().parent
ACCOUNT_FILE = Path(os.environ.get("ACCOUNT_FILE", BASE / "data" / "account.json"))
OWNER = os.environ.get("OWNER", "").strip()                 # the owner's @username
OWNER_NAME = os.environ.get("OWNER_NAME", "").strip()       # what the assistant calls them
TRANSCRIBE_API_KEY = os.environ.get("TRANSCRIBE_API_KEY", "").strip()
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
VOICE_FA = os.environ.get("VOICE_FA", "fa-IR-DilaraNeural")
VOICE_EN = os.environ.get("VOICE_EN", "en-US-AvaMultilingualNeural")
# English can come from Kokoro running on this server instead (it can't speak Persian).
TTS_EN = os.environ.get("TTS_EN", "edge").strip().lower()      # "edge" or "kokoro"
KOKORO_VOICE = os.environ.get("KOKORO_VOICE", "bm_lewis")
KOKORO_DIR = Path(os.environ.get("KOKORO_DIR", BASE / "models"))
PERSONA = os.environ.get("PERSONA", "").strip().lower()       # "jarvis" or empty
JARVIS_ADDRESS = os.environ.get("JARVIS_ADDRESS", "sir").strip()  # how Jarvis addresses you
TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
CLAUDE_MCP_CONFIG = os.environ.get("CLAUDE_MCP_CONFIG", "")   # lets Claude read Telegram on calls
CLAUDE_ALLOWED_TOOLS = os.environ.get("CLAUDE_ALLOWED_TOOLS", "WebSearch,WebFetch")
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "180"))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")            # e.g. "haiku" for faster answers
RING_SECONDS = int(os.environ.get("RING_SECONDS", "45"))
QUIET_HANGUP_SECONDS = int(os.environ.get("QUIET_HANGUP_SECONDS", "90"))
MAX_CALL_MINUTES = int(os.environ.get("MAX_CALL_MINUTES", "30"))
# Most disk space the saved call conversations may use; the oldest go first. 0 = no limit.
STORAGE_LIMIT_MB = int(os.environ.get("STORAGE_LIMIT_MB", "1024"))

RATE = 48000                      # call audio: 16-bit mono PCM at 48 kHz
FRAME_BYTES = RATE * 2 // 100     # 10 ms
# This much quiet after you speak ends your turn. Shorter answers sooner, but may cut you
# off when you pause mid-sentence (people often pause about a second).
END_OF_TURN_MS = int(os.environ.get("END_OF_TURN_MS", "1200"))
MIN_SPEECH_MS = 300               # shorter noises are ignored
MAX_TURN_SECONDS = 90
AUDIO = AudioParameters(bitrate=RATE, channels=1)

PHONE_PROMPT = (
    f"You are {OWNER_NAME or 'the user'}'s personal AI assistant, talking with them on a "
    "Telegram phone call. Everything you write is read out loud by a text-to-speech voice, so "
    "answer in short, natural spoken sentences: no lists, headings, markdown, emoji, links or "
    "code. Reply in the language they speak (Persian or English). Keep answers under about "
    "60 words unless they ask for more. What they say reaches you through speech-to-text, so "
    "guess past small transcription mistakes. You can't send, edit or delete anything on "
    "Telegram during a call; if they ask, tell them to ask Claude in the app. "
    "If they want to end the call (bye, خداحافظ, that's all, ...), say a short goodbye and put "
    "[END] at the very end of your reply."
)
JARVIS_PROMPT = (
    " Play the part of Jarvis, their AI butler: calm, precise and quietly witty, with "
    f"refined British manners. Address them as \"{JARVIS_ADDRESS}\""
    + (" (\"قربان\" in Persian)." if JARVIS_ADDRESS == "sir" else ".")
)
if PERSONA == "jarvis":
    PHONE_PROMPT += JARVIS_PROMPT


def greeting() -> tuple[str, bool]:
    """What it says when a call starts, and whether that's Persian."""
    if PERSONA == "jarvis":
        hour = datetime.now(TZ).hour
        part = "morning" if 5 <= hour < 12 else "afternoon" if 12 <= hour < 18 else "evening"
        return f"Good {part}, {JARVIS_ADDRESS}. How may I help?", False
    return f"سلام{' ' + OWNER_NAME if OWNER_NAME else ''}! جانم، بگو.", True

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("pytgcalls").setLevel(logging.WARNING)
log = logging.getLogger("telegram-caller")

PERSIAN = re.compile(r"[؀-ۿ]")


def is_persian(text: str) -> bool:
    return bool(PERSIAN.search(text or ""))


def tick_sound() -> bytes:
    """A soft short tick played while Claude is thinking."""
    t = np.arange(int(RATE * 0.09)) / RATE
    tone = np.sin(2 * math.pi * 880 * t) * np.hanning(len(t)) * 2500
    return tone.astype(np.int16).tobytes()


TICK = tick_sound()


# ---------------------------------------------------------------- hearing
class Listener:
    """Collects what the caller says and cuts it into turns at the pauses."""

    def __init__(self):
        self.turns: asyncio.Queue[bytes] = asyncio.Queue()
        self.active = False
        self.noise = 300.0
        self.last_heard = time.monotonic()
        self._pre: deque[bytes] = deque(maxlen=25)   # keeps the start of the first word
        self._reset()

    def _reset(self):
        self._buf = bytearray()
        self._speech_ms = 0.0
        self._quiet_ms = 0.0
        self._pre.clear()

    def start(self):
        self._reset()
        self.active = True

    def stop(self):
        self.active = False
        self._reset()

    def feed(self, pcm: bytes):
        if not self.active or not pcm:
            return
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return
        ms = samples.size * 1000 / RATE
        level = float(np.sqrt(np.mean(samples * samples)))
        loud = level > max(3 * self.noise, 400)
        if not self._buf and not loud:
            self.noise = 0.95 * self.noise + 0.05 * max(level, 50)
            self._pre.append(pcm)
            return
        if loud:
            if not self._buf:
                self._buf.extend(b"".join(self._pre))
            self._speech_ms += ms
            self._quiet_ms = 0
            self.last_heard = time.monotonic()
        else:
            self._quiet_ms += ms
        self._buf.extend(pcm)
        too_long = len(self._buf) >= MAX_TURN_SECONDS * RATE * 2
        if self._quiet_ms >= END_OF_TURN_MS or too_long:
            if self._speech_ms >= MIN_SPEECH_MS:
                self.turns.put_nowait(bytes(self._buf))
            self._reset()


def history_dir() -> Path:
    """Where Claude Code keeps the conversations of calls (one file per call)."""
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(BASE / "workdir"))


def trim_storage(folder: Path | None = None, limit_mb: int | None = None) -> int:
    """Keep the saved call conversations under the storage limit by deleting the oldest.
    Returns how many files were deleted."""
    folder = folder or history_dir()
    limit = (STORAGE_LIMIT_MB if limit_mb is None else limit_mb) * 1024 * 1024
    if limit <= 0 or not folder.exists():
        return 0
    files = sorted((p for p in folder.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    removed = 0
    for p in files:
        if total <= limit:
            break
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        removed += 1
    for d in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    if removed:
        log.info("storage limit: deleted %s old call files, %.0f MB left", removed, total / 1024 / 1024)
    return removed


def to_wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue()


async def transcribe(pcm: bytes) -> str:
    if not TRANSCRIBE_API_KEY:
        raise RuntimeError("TRANSCRIBE_API_KEY is missing in .env")
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.post(TRANSCRIBE_URL, data={"model": TRANSCRIBE_MODEL},
                            headers={"Authorization": f"Bearer {TRANSCRIBE_API_KEY}"},
                            files={"file": ("speech.wav", to_wav(pcm), "audio/wav")})
    if r.status_code >= 400:
        raise RuntimeError(f"speech-to-text error {r.status_code}: {r.text[:200]}")
    return (r.json().get("text") or "").strip()


# ---------------------------------------------------------------- thinking
SENTENCE_END = re.compile(r"[.!?؟…]+[\"')\]»]*\s|\n")


def split_sentences(buf: str) -> tuple[list[str], str]:
    """Cut the finished sentences off the front of buf; return them and the rest."""
    done = []
    while (m := SENTENCE_END.search(buf)):
        piece, buf = buf[:m.end()].strip(), buf[m.end():]
        if piece:
            done.append(piece)
    return done, buf


async def think(text: str, session_id: str | None,
                sentences: asyncio.Queue | None = None) -> tuple[str, str | None]:
    """Ask Claude Code for the answer. Each sentence goes into `sentences` as soon as
    Claude has written it, so speaking can start early. Returns (whole reply, session id
    for the next turn)."""
    args = [CLAUDE_BIN, "-p", "--output-format", "stream-json", "--verbose",
            "--include-partial-messages", "--settings", '{"alwaysThinkingEnabled": false}',
            "--append-system-prompt", PHONE_PROMPT]
    if session_id:
        args += ["--resume", session_id]
    if CLAUDE_MODEL:
        args += ["--model", CLAUDE_MODEL]
    if CLAUDE_MCP_CONFIG and Path(CLAUDE_MCP_CONFIG).exists():
        args += ["--mcp-config", CLAUDE_MCP_CONFIG]
    if CLAUDE_ALLOWED_TOOLS:
        args += ["--allowedTools", CLAUDE_ALLOWED_TOOLS]
    workdir = BASE / "workdir"
    workdir.mkdir(exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=workdir, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    proc.stdin.write(text.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    buf, full, result = "", "", None

    def emit(pieces):
        for p in pieces:
            p = p.replace("[END]", "").strip()
            if p and sentences is not None:
                sentences.put_nowait(p)

    async def read():
        nonlocal buf, full, result, session_id
        async for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = event.get("session_id") or session_id
            if event.get("type") == "result":
                result = event
            if event.get("type") != "stream_event":
                continue
            inner = event.get("event") or {}
            delta = inner.get("delta") or {}
            if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                full += delta.get("text", "")
                buf += delta.get("text", "")
                ready, buf = split_sentences(buf)
                emit(ready)
            elif inner.get("type") == "content_block_stop" and buf.strip():
                emit([buf])  # e.g. "Let me check, sir." before a web search
                buf = ""

    try:
        await asyncio.wait_for(read(), CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("Claude took too long")
    await proc.wait()
    emit([buf])
    if result is None or result.get("is_error"):
        err = (await proc.stderr.read()).decode()[:300]
        raise RuntimeError(f"Claude failed: {(result or {}).get('result') or err}")
    return (result.get("result") or full).strip(), session_id


# ---------------------------------------------------------------- speaking
_kokoro = None


def load_kokoro():
    """Load Kokoro once (it takes a moment and stays in memory)."""
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        _kokoro = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
    return _kokoro


async def _to_call_audio(data: bytes, *input_format: str) -> bytes:
    """Any audio to the call's 48 kHz mono PCM, with ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", *input_format, "-i", "pipe:0", "-f", "s16le", "-ac", "1",
        "-ar", str(RATE), "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    pcm, _ = await proc.communicate(data)
    return pcm


async def speech(text: str) -> bytes:
    """Text to 48 kHz mono PCM: Kokoro on this server for English (when TTS_EN=kokoro),
    otherwise Microsoft's free voices."""
    persian = is_persian(text)
    if not persian and TTS_EN == "kokoro":
        lang = "en-gb" if KOKORO_VOICE.startswith("b") else "en-us"
        samples, rate = await asyncio.to_thread(load_kokoro().create, text,
                                                voice=KOKORO_VOICE, lang=lang)
        pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
        return await _to_call_audio(pcm, "-f", "s16le", "-ar", str(rate), "-ac", "1")
    mp3 = bytearray()
    async for chunk in edge_tts.Communicate(text, VOICE_FA if persian else VOICE_EN).stream():
        if chunk["type"] == "audio":
            mp3.extend(chunk["data"])
    return await _to_call_audio(bytes(mp3))


class Call:
    def __init__(self, calls: PyTgCalls, chat_id: int):
        self.calls = calls
        self.chat_id = chat_id
        self.ear = Listener()
        self.ended = asyncio.Event()
        self.session_id: str | None = None
        self.persian = True

    async def play_pcm(self, pcm: bytes):
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        for i in range(0, len(pcm), FRAME_BYTES):
            if self.ended.is_set():
                return
            chunk = pcm[i:i + FRAME_BYTES]
            if len(chunk) < FRAME_BYTES:
                chunk += b"\x00" * (FRAME_BYTES - len(chunk))
            await self.calls.send_frame(self.chat_id, Device.MICROPHONE, chunk)
            next_at += 0.01
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)

    async def say(self, text: str):
        self.ear.stop()
        try:
            await self.play_pcm(await speech(text))
            await asyncio.sleep(0.3)
        finally:
            self.ear.start()

    async def ticking(self):
        while True:
            await self.play_pcm(TICK)
            await asyncio.sleep(1.8)

    async def next_turn(self, timeout: float) -> bytes | None:
        get = asyncio.ensure_future(self.ear.turns.get())
        end = asyncio.ensure_future(self.ended.wait())
        done, _ = await asyncio.wait({get, end}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        for t in (get, end):
            if t not in done:
                t.cancel()
        return get.result() if get in done else None

    def words(self, fa: str, en: str) -> str:
        return fa if self.persian else en

    async def respond(self, heard: str) -> bool:
        """Think and speak at the same time: Claude writes, each finished sentence is
        turned into speech while the one before it plays. Returns True to end the call."""
        sentences: asyncio.Queue = asyncio.Queue()
        audio: asyncio.Queue = asyncio.Queue()
        made = 0

        async def thinking() -> bool:
            try:
                reply, self.session_id = await think(heard, self.session_id, sentences)
                log.info("reply: %s", reply)
                return "[END]" in reply
            except Exception as e:
                log.warning("thinking failed: %s", e)
                if not made and sentences.empty():
                    sentences.put_nowait(self.words(
                        "ببخشید، الان نتونستم جواب بدم. دوباره امتحان کن.",
                        "Sorry, I couldn't come up with an answer. Try again?"))
                return False
            finally:
                sentences.put_nowait(None)

        async def voicing():
            nonlocal made
            while (text := await sentences.get()) is not None:
                try:
                    audio.put_nowait(await speech(text))
                    made += 1
                except Exception as e:
                    log.warning("speech failed: %s", e)
            audio.put_nowait(None)

        thinker = asyncio.create_task(thinking())
        voicer = asyncio.create_task(voicing())
        tick = asyncio.create_task(self.ticking())
        try:
            while (pcm := await audio.get()) is not None:
                if not tick.done():
                    tick.cancel()
                    await asyncio.gather(tick, return_exceptions=True)
                await self.play_pcm(pcm)
                if self.ended.is_set():
                    break
        finally:
            tick.cancel()
            if self.ended.is_set():
                thinker.cancel()
                voicer.cancel()
        if self.ended.is_set():
            return True
        return await thinker

    async def converse(self, opening: str):
        started = time.monotonic()
        await self.say(opening)
        while not self.ended.is_set():
            pcm = await self.next_turn(QUIET_HANGUP_SECONDS)
            if self.ended.is_set():
                break
            if pcm is None:
                await self.say(self.words("صدایی نمیاد، قطع می‌کنم. خداحافظ!",
                                          "I can't hear you, so I'll hang up. Bye!"))
                break
            self.ear.stop()
            try:
                heard = await transcribe(pcm)
            except Exception as e:
                log.warning("speech-to-text failed: %s", e)
                await self.say(self.words("ببخشید، نشنیدم. دوباره بگو.",
                                          "Sorry, I didn't catch that. Say it again?"))
                continue
            if not heard:
                self.ear.start()
                continue
            self.persian = is_persian(heard)
            log.info("heard: %s", heard)
            done = await self.respond(heard)
            await asyncio.sleep(0.3)
            self.ear.start()
            if done:
                break
            if time.monotonic() - started > MAX_CALL_MINUTES * 60:
                await self.say(self.words("وقت تماس تموم شد. خداحافظ!",
                                          "We've hit the call time limit. Bye!"))
                break


# ---------------------------------------------------------------- the account
class Assistant:
    def __init__(self):
        acc = json.loads(ACCOUNT_FILE.read_text())
        self.client = TelegramClient(StringSession(acc["session"]), int(acc["api_id"]),
                                     acc["api_hash"], device_model="AI assistant",
                                     system_version="Linux server", app_version="1.0")
        self.calls = PyTgCalls(self.client)
        self.owner_id: int | None = None
        self.call: Call | None = None

    async def run(self):
        if not OWNER:
            raise SystemExit("OWNER is missing in .env (your main account's @username).")
        await self.client.start()
        owner = await self.client.get_entity(OWNER)
        self.owner_id = owner.id
        me = await self.client.get_me()
        log.info("logged in as %s, owner is %s (%s)", me.first_name, OWNER, self.owner_id)
        if TTS_EN == "kokoro":
            await asyncio.to_thread(load_kokoro)  # loaded now so the first answer isn't slow
            log.info("Kokoro voice %s ready", KOKORO_VOICE)

        self.client.add_event_handler(self.on_message, events.NewMessage(incoming=True))
        self.calls.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))(self.on_ring)
        self.calls.on_update(filters.chat_update(
            ChatUpdate.Status.DISCARDED_CALL | ChatUpdate.Status.BUSY_CALL))(self.on_hangup)
        self.calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))(self.on_audio)
        await self.calls.start()
        trim_storage()
        log.info("ready for calls")
        await self.client.run_until_disconnected()

    async def on_message(self, event):
        if event.sender_id != self.owner_id:
            return
        text = (event.raw_text or "").strip()
        m = re.match(r"^(/call|call me|زنگ بزن)\b\s*(.*)$", text, re.I | re.S)
        if not m:
            return
        if self.call:
            await event.reply("I'm already on a call with you.")
            return
        asyncio.create_task(self.place_call(m.group(2).strip()))

    async def place_call(self, message: str):
        chat_id = self.owner_id
        try:
            await self.calls.play(chat_id, MediaStream(ExternalMedia.AUDIO, AUDIO),
                                  CallConfig(timeout=RING_SECONDS))
        except (TimedOutAnswer, CallDeclined, CallBusy, CallDiscarded) as e:
            log.info("call not answered: %s", type(e).__name__)
            if message:
                await self.client.send_message(chat_id, f"📞 I tried to call you:\n{message}")
            return
        opening, persian = (message, is_persian(message)) if message else greeting()
        await self.talk(chat_id, opening, persian)

    async def on_ring(self, _, update: ChatUpdate):
        if update.chat_id != self.owner_id or self.call:
            log.info("ignoring a call from %s", update.chat_id)
            return
        # The library waits for handlers to finish, so the call runs on its own.
        asyncio.create_task(self.answer(update.chat_id))

    async def answer(self, chat_id: int):
        try:
            await self.calls.play(chat_id, MediaStream(ExternalMedia.AUDIO, AUDIO))
        except Exception:
            log.exception("couldn't answer the call")
            return
        await self.talk(chat_id, *greeting())

    async def talk(self, chat_id: int, opening: str, persian: bool):
        call = self.call = Call(self.calls, chat_id)
        call.persian = persian
        try:
            await self.calls.record(chat_id, RecordStream(True, AUDIO))
            await call.converse(opening)
        except Exception:
            log.exception("call failed")
        finally:
            self.call = None
            if not call.ended.is_set():
                try:
                    await self.calls.leave_call(chat_id)
                except Exception:
                    pass
            log.info("call ended")
            try:
                trim_storage()
            except OSError as e:
                log.warning("couldn't trim the call history: %s", e)

    async def on_hangup(self, _, update: ChatUpdate):
        if self.call and update.chat_id == self.call.chat_id:
            self.call.ended.set()

    async def on_audio(self, _, update: StreamFrames):
        if self.call and update.chat_id == self.call.chat_id:
            for frame in update.frames:
                self.call.ear.feed(frame.frame)


if __name__ == "__main__":
    # The call library grabs the current event loop when it's created, so the loop
    # has to exist first.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(Assistant().run())
