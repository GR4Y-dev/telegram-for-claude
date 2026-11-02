"""
Offline tests for the call assistant: real voices, a fake Telegram call.

    pip install -r telegram-caller/requirements.txt
    python tests/test_caller.py

Needs ffmpeg and internet (for Microsoft's voices). If the `claude` command is installed and
logged in, a few checks also ask real Claude; set SKIP_CLAUDE=1 to leave them out.
"""
import asyncio, importlib, os, shutil, sys, time
from pathlib import Path

import numpy as np

os.environ.setdefault("OWNER", "@test")
os.environ["CLAUDE_ALLOWED_TOOLS"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "telegram-caller"))
import caller  # noqa: E402

ok = 0
def check(cond, what):
    global ok
    if not cond:
        raise SystemExit(f"FAIL: {what}")
    ok += 1
    print("ok -", what, flush=True)

def seconds(pcm): return len(pcm) / 2 / caller.RATE

class FakeCalls:
    """Stands in for the Telegram call: counts the audio sent."""
    def __init__(self): self.sent = 0; self.first = None
    async def send_frame(self, chat_id, device, data):
        assert len(data) == caller.FRAME_BYTES
        self.sent += len(data)
        if self.first is None: self.first = time.monotonic()


def with_settings(**env):
    """Reload caller.py with some settings, as if they were in .env."""
    for k, v in env.items():
        os.environ[k] = v
    importlib.reload(caller)
    for k in env:
        del os.environ[k]
    return caller


async def main():
    global caller
    # names, greeting, Jarvis
    c = with_settings(OWNER_NAME="Sara", PERSONA="jarvis", JARVIS_ADDRESS="ma'am")
    g, fa = c.greeting()
    check(g.endswith("ma'am. How may I help?") and not fa, f"Jarvis greeting: {g}")
    check(c.PHONE_PROMPT.startswith("You are Sara's") and "Jarvis" in c.PHONE_PROMPT, "name and Jarvis in Claude's instructions")
    c = with_settings(OWNER_NAME="Sara")
    check(c.greeting() == ("سلام Sara! جانم، بگو.", True) and "Jarvis" not in c.PHONE_PROMPT, "plain Persian greeting")
    caller = c = with_settings()
    check(c.greeting()[0] == "سلام! جانم، بگو." and "the user's" in c.PHONE_PROMPT, "works without a name")

    # speaking
    fa = await caller.speech("سلام! جانم، بگو.")
    en = await caller.speech("Hi, how can I help?")
    check(seconds(fa) > 1 and seconds(en) > 1, f"Persian ({seconds(fa):.1f}s) and English ({seconds(en):.1f}s) speech")
    check(caller.is_persian("ساعت چنده؟") and not caller.is_persian("what time is it"), "language detection")

    # hearing: quiet room, then speech, then a pause
    ear = caller.Listener(); ear.start()
    rng = np.random.default_rng(1)
    noise = lambda s: (rng.normal(0, 60, int(caller.RATE * s))).astype(np.int16).tobytes()
    stream = noise(0.8) + fa + noise(1.6)
    for i in range(0, len(stream), caller.FRAME_BYTES):
        ear.feed(stream[i:i + caller.FRAME_BYTES])
    check(ear.turns.qsize() == 1, "one turn detected after the pause")
    turn = ear.turns.get_nowait()
    check(seconds(fa) <= seconds(turn) <= seconds(fa) + 1.6, f"turn holds the whole sentence ({seconds(turn):.1f}s)")
    click = noise(0.5) + (np.ones(int(caller.RATE * 0.1)) * 8000).astype(np.int16).tobytes() + noise(1.5)
    for i in range(0, len(click), caller.FRAME_BYTES):
        ear.feed(click[i:i + caller.FRAME_BYTES])
    check(ear.turns.qsize() == 0, "a short click is ignored")
    ear.stop(); ear.feed(fa)
    check(ear.turns.qsize() == 0, "nothing is heard while the assistant speaks")
    check(caller.to_wav(fa)[:4] == b"RIFF", "speech packed as WAV for speech-to-text")

    # storage limit: the oldest call conversations go first
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        for i in range(5):
            f = folder / ("sub" if i == 4 else "") / f"call{i}.jsonl"
            f.parent.mkdir(exist_ok=True)
            f.write_bytes(b"x" * 400_000)
            os.utime(f, (1_000_000 + i, 1_000_000 + i))
        check(caller.trim_storage(folder, limit_mb=0) == 0, "limit 0 means no limit")
        removed = caller.trim_storage(folder, limit_mb=1)
        left = sorted(p.name for p in folder.rglob("*.jsonl"))
        check(removed == 3 and left == ["call3.jsonl", "call4.jsonl"], f"over 1 MB: oldest deleted, newest kept ({left})")
        check(caller.trim_storage(folder, limit_mb=1) == 0, "under the limit nothing is deleted")
    check(caller.history_dir().name.endswith("telegram-caller-workdir"), "looks in Claude's folder for this assistant")

    # cutting Claude's answer into sentences
    done, rest = caller.split_sentences("Very good, sir. The skies are clear! Shall I")
    check(done == ["Very good, sir.", "The skies are clear!"] and rest.strip() == "Shall I", "English sentences split")
    done, rest = caller.split_sentences("بله قربان. هوا صافه؟ الان")
    check(done == ["بله قربان.", "هوا صافه؟"] and rest.strip() == "الان", "Persian sentences split")
    check(caller.split_sentences("It costs 3.5 dollars. Ok")[0] == ["It costs 3.5 dollars."], "decimal numbers aren't cut")

    # real Claude, if it's installed
    if shutil.which(caller.CLAUDE_BIN) and not os.environ.get("SKIP_CLAUDE"):
        q = asyncio.Queue(); t0 = time.monotonic()
        reply, sid = await caller.think("Hi! My name is Sara. Tell me in three short sentences what you can do.", None, q)
        check(sid and q.qsize() >= 2, f"Claude streams its answer sentence by sentence ({time.monotonic()-t0:.0f}s)")
        reply2, sid2 = await caller.think("What's my name?", sid)
        check("sara" in reply2.lower(), f"Claude remembers the call: {reply2[:60]}")
        reply3, _ = await caller.think("Ok thanks, bye!", sid2)
        check(reply3.rstrip().endswith("[END]"), f"Claude ends the call on bye: {reply3[:60]}")
    else:
        print("-- skipped the real-Claude checks (no claude command, or SKIP_CLAUDE set)")

    # speaking starts while Claude is still writing
    async def slow_think(text, sid, sentences=None):
        for s in ["Certainly.", "The weather is clear today.", "Anything else? [END]"]:
            await asyncio.sleep(1.5); sentences.put_nowait(s.replace("[END]", "").strip())
        return "Certainly. The weather is clear today. Anything else? [END]", "s2"
    caller.think = slow_think
    calls = FakeCalls(); call = caller.Call(calls, 1); call.persian = False
    async def no_tick(): await asyncio.sleep(3600)
    call.ticking = no_tick
    t0 = time.monotonic(); end = await call.respond("hello")
    check(end is True and call.session_id == "s2", "[END] ends the call")
    check(calls.first - t0 < 4.5, f"first words after {calls.first - t0:.1f}s, before Claude finished (4.5s)")

    # Claude failing gives a spoken apology
    async def broken(text, sid, sentences=None): raise RuntimeError("boom")
    caller.think = broken
    calls = FakeCalls(); call = caller.Call(calls, 1)
    check(await call.respond("hello") is False and calls.sent > caller.RATE, "Claude failing gives a spoken apology")

    # a whole call: greeting, a question, goodbye
    heard = iter(["ساعت چنده؟", "مرسی، خداحافظ"])
    replies = iter([["ساعت ده شبه."], ["خداحافظ!"]])
    async def fake_transcribe(pcm): return next(heard)
    async def fake_think(text, sid, sentences=None):
        said = next(replies)
        for s in said: sentences.put_nowait(s)
        return " ".join(said) + (" [END]" if "خداحافظ" in text else ""), "s1"
    caller.transcribe, caller.think = fake_transcribe, fake_think
    calls = FakeCalls(); call = caller.Call(calls, 42)
    call.ear.turns.put_nowait(b"\x01" * 9600); call.ear.turns.put_nowait(b"\x01" * 9600)
    await asyncio.wait_for(call.converse("سلام! جانم، بگو."), 60)
    check(calls.sent > caller.RATE * 2 * 3 and call.session_id == "s1",
          f"full call: greeting, answer, goodbye ({calls.sent / caller.RATE / 2:.1f}s of audio)")

    # messages and commands from the owner
    from types import SimpleNamespace
    sent, placed = [], []
    class FakeClient:
        async def send_message(self, to, text): sent.append((to, text))
    a = object.__new__(caller.Assistant)
    a.client, a.owner_id, a.call = FakeClient(), 7, None
    async def fake_place(msg): placed.append(msg)
    a.place_call = fake_place
    async def message(sender, text):
        ev = SimpleNamespace(sender_id=sender, raw_text=text, replies=[], deleted=False, is_private=True)
        async def reply(t): ev.replies.append(t)
        async def delete(): ev.deleted = True
        ev.reply, ev.delete = reply, delete
        await a.on_message(ev); await asyncio.sleep(0)
        return ev
    ev = await message(7, "/text Remember the exam at 9")
    check(sent == [(7, "Remember the exam at 9")] and ev.deleted, "/text: the assistant texts you, the command is tidied away")
    ev = await message(7, "/call wake up")
    check(placed == ["wake up"] and ev.deleted, "/call still calls you")
    await message(99, "/text hacked"); await message(99, "/update")
    check(len(sent) == 1, "nobody else can make it text or update")
    with tempfile.TemporaryDirectory() as tmp:
        caller.UPDATES_DIR = Path(tmp) / "updates"
        ev = await message(7, "/update")
        check("aren't set up" in ev.replies[0], "/update explains when updates aren't set up")
        caller.UPDATES_DIR.mkdir()
        (caller.UPDATES_DIR / "telegram-for-claude").write_text("/opt/telegram-for-claude\nTelegram for Claude\n")
        (caller.UPDATES_DIR / "whatsapp-for-claude").write_text("/opt/whatsapp-for-claude\nWhatsApp for Claude\n")
        runs = []
        real_exec = asyncio.create_subprocess_exec
        async def fake_exec(*args, **kw):
            runs.append(args)
            async def communicate(): return b"", b""
            return SimpleNamespace(communicate=communicate, returncode=0)
        asyncio.create_subprocess_exec = fake_exec
        try:
            ev = await message(7, "/update")
        finally:
            asyncio.create_subprocess_exec = real_exec
        check(len(runs) == 2 and runs[0][0] == "systemd-run" and "--setenv=REPORT=1" in runs[0]
              and runs[0][-1] == "/opt/telegram-for-claude/update.sh",
              "/update runs each project's update.sh in its own systemd unit")
        check("Telegram for Claude, WhatsApp for Claude" in ev.replies[0], f"/update says what it's checking: {ev.replies[0]}")
        a.call = object(); ev = await message(7, "/update"); a.call = None
        check("after it" in ev.replies[0], "/update waits while you're on a call")

        # the outbox: files from server scripts become texts
        caller.OUTBOX_DIR = Path(tmp) / "outbox"
        caller.OUTBOX_DIR.mkdir()
        (caller.OUTBOX_DIR / "1-a.txt").write_text("✅ Telegram for Claude updated", encoding="utf-8")
        (caller.OUTBOX_DIR / "2-b.txt").write_text("⚠️ second", encoding="utf-8")
        (caller.OUTBOX_DIR / ".hidden").write_text("not me")
        sent.clear()
        task = asyncio.create_task(a.watch_outbox()); await asyncio.sleep(0.3); task.cancel()
        check(sent == [(7, "✅ Telegram for Claude updated"), (7, "⚠️ second")]
              and sorted(p.name for p in caller.OUTBOX_DIR.iterdir()) == [".hidden"],
              "outbox files are texted in order, then removed")

    # hanging up mid-sentence stops the voice
    call2 = caller.Call(FakeCalls(), 42)
    asyncio.get_running_loop().call_later(0.5, call2.ended.set)
    t0 = time.monotonic()
    await call2.converse("این یک جمله‌ی خیلی طولانیه که نباید تا آخرش پخش بشه چون تماس قطع شده.")
    check(time.monotonic() - t0 < 2.5, "hanging up stops the assistant right away")

asyncio.run(main())
print(f"\nALL {ok} CHECKS PASSED")
