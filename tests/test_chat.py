"""Offline chat regression tests: no accounts, API keys or messages sent."""
import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
import caller


class Client:
    def __init__(self):
        self.actions = []
        self.send_message = AsyncMock()

    @asynccontextmanager
    async def action(self, owner, action):
        self.actions.append((owner, action))
        try:
            yield
        finally:
            self.actions.append((owner, "cancel"))


def event(text="", *, sender=7, private=True, voice=False, size=50, duration=2):
    return SimpleNamespace(
        sender_id=sender, is_private=private, raw_text=text, voice=voice,
        message=SimpleNamespace(document=SimpleNamespace(
            size=size, attributes=[SimpleNamespace(duration=duration)])),
        reply=AsyncMock(), delete=AsyncMock(), download_media=AsyncMock(return_value=b"ogg"))


class ChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.a = object.__new__(caller.Assistant)
        self.a.client, self.a.owner_id, self.a.call = Client(), 7, None
        self.a.chat_session_id = None
        self.a.chat_lock, self.a.chat_pending = asyncio.Lock(), 0
        self.think = AsyncMock(return_value=("Hello", "session-1"))
        self.patcher = patch.object(caller, "think", self.think)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_memory_typing_and_reset(self):
        await self.a.on_message(event("My name is Sara"))
        await self.a.on_message(event("What's my name?"))
        self.assertEqual(self.think.await_args_list[1].args[1], "session-1")
        await self.a.on_message(event("/new"))
        await self.a.on_message(event("Hi again"))
        self.assertIsNone(self.think.await_args.args[1])
        self.assertEqual(self.a.client.actions, [(7, "typing"), (7, "cancel")] * 3)
        self.assertEqual(self.a.chat_pending, 0)

    async def test_only_private_owner_messages(self):
        for ev in [event("hi", sender=8), event("hi", private=False),
                   event("/text private", private=False), event("/new", sender=8)]:
            await self.a.on_message(ev)
            ev.reply.assert_not_awaited()
        self.think.assert_not_awaited()
        self.a.client.send_message.assert_not_awaited()

    async def test_voice_transcript_then_reply(self):
        ev = event(voice=True)
        with patch.object(caller, "transcribe_audio", AsyncMock(return_value="ساعت چنده؟")) as stt:
            await self.a.on_message(ev)
        stt.assert_awaited_once_with(b"ogg", "voice.ogg", "audio/ogg")
        self.think.assert_awaited_once_with("ساعت چنده؟", None, chat=True)
        self.assertEqual([c.args[0] for c in ev.reply.await_args_list],
                         ["Transcript:\nساعت چنده؟", "Hello"])

    async def test_voice_limits_and_empty_transcription(self):
        for ev in [event(voice=True, size=caller.MAX_VOICE_BYTES + 1),
                   event(voice=True, duration=301)]:
            await self.a.on_message(ev)
            ev.download_media.assert_not_awaited()
        with patch.object(caller, "transcribe_audio", AsyncMock(return_value="")):
            await self.a.on_message(event(voice=True))
        self.think.assert_not_awaited()

    async def test_failure_and_retry(self):
        self.think.side_effect = RuntimeError("secret provider detail")
        ev = event("hi")
        with self.assertLogs("telegram-caller", level="ERROR"):
            await self.a.on_message(ev)
        self.assertNotIn("secret", ev.reply.await_args.args[0])
        self.assertEqual(self.a.chat_pending, 0)
        self.assertFalse(self.a.chat_lock.locked())
        self.assertEqual(self.a.client.actions[-1], (7, "cancel"))
        self.think.side_effect = None
        await self.a.on_message(event("retry"))
        self.assertEqual(self.a.chat_session_id, "session-1")

    async def test_serialization_and_reset_during_reply(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(text, sid, **kwargs):
            entered.set()
            await release.wait()
            return "answer", "old-session"
        self.think.side_effect = delayed
        first = asyncio.create_task(self.a.on_message(event("first")))
        await entered.wait()
        reset = asyncio.create_task(self.a.on_message(event("/new")))
        second = asyncio.create_task(self.a.on_message(event("second")))
        await asyncio.sleep(0)
        self.assertEqual(self.think.await_count, 1)
        release.set()
        await asyncio.gather(first, reset, second)
        self.assertIsNone(self.think.await_args.args[1])

    async def test_long_unicode_reply_is_lossless_plain_text(self):
        answer = "🙂_*<>" * 1700
        self.think.return_value = (answer, "s")
        ev = event("long answer")
        await self.a.on_message(ev)
        chunks = [c.args[0] for c in ev.reply.await_args_list]
        self.assertEqual("".join(chunks), answer)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks))
        self.assertTrue(all(c.kwargs["parse_mode"] is None for c in ev.reply.await_args_list))

    async def test_commands_and_unsupported_input(self):
        for text in ["/unknown", "/callback", ""]:
            await self.a.on_message(event(text))
        self.think.assert_not_awaited()
        self.a.place_call = AsyncMock()
        self.a.start_updates = AsyncMock()
        await self.a.on_message(event("/call wake up"))
        await asyncio.sleep(0)
        self.a.place_call.assert_awaited_once_with("wake up")
        await self.a.on_message(event("/text reminder"))
        self.a.client.send_message.assert_awaited_once_with(7, "reminder")
        await self.a.on_message(event("/update"))
        self.a.start_updates.assert_awaited_once()

    async def test_backpressure(self):
        self.a.chat_pending = 10
        await self.a.on_message(event("hi"))
        self.think.assert_not_awaited()
        self.assertEqual(self.a.chat_pending, 10)


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_reaps_process(self):
        stdout = asyncio.StreamReader()  # Never produces a result or EOF.
        proc = SimpleNamespace(stdout=stdout,
            stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
            stdin=SimpleNamespace(write=lambda x: None, drain=AsyncMock(), close=lambda: None),
            wait=AsyncMock(return_value=-1), returncode=None)
        killed = []
        proc.kill = lambda: killed.append(True)
        with tempfile.TemporaryDirectory() as tmp, patch.object(caller, "BASE", Path(tmp)), \
             patch.object(caller, "CLAUDE_TIMEOUT", 0.01), \
             patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)):
            with self.assertRaisesRegex(RuntimeError, "took too long"):
                await caller.think("hi", None, chat=True)
        self.assertEqual(killed, [True])
        proc.wait.assert_awaited_once()

    async def test_transcription_preserves_voice_format_and_call_wav(self):
        response = SimpleNamespace(status_code=200, json=lambda: {"text": " hello "})
        http = SimpleNamespace(post=AsyncMock(return_value=response))
        @asynccontextmanager
        async def client(**kwargs):
            yield http
        with patch.object(caller.httpx, "AsyncClient", client), \
             patch.object(caller, "TRANSCRIBE_API_KEY", "test-key"):
            self.assertEqual(await caller.transcribe_audio(b"ogg", "voice.ogg", "audio/ogg"), "hello")
            self.assertEqual(http.post.await_args.kwargs["files"]["file"],
                             ("voice.ogg", b"ogg", "audio/ogg"))
            await caller.transcribe(b"\0\0")
            filename, data, mime = http.post.await_args.kwargs["files"]["file"]
            self.assertEqual((filename, mime), ("speech.wav", "audio/wav"))
            self.assertTrue(data.startswith(b"RIFF"))

    async def test_fresh_time_and_separate_prompts(self):
        def process():
            stdout = asyncio.StreamReader()
            stdout.feed_data((json.dumps({"type": "result", "result": "OK", "session_id": "new"}) + "\n").encode())
            stdout.feed_eof()
            return SimpleNamespace(stdout=stdout, stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
                stdin=SimpleNamespace(write=lambda x: None, drain=AsyncMock(), close=lambda: None),
                wait=AsyncMock(return_value=0), returncode=0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(caller, "BASE", Path(tmp)), \
             patch.object(caller, "time_context", side_effect=["TIME ONE", "TIME TWO"]), \
             patch.object(asyncio, "create_subprocess_exec", AsyncMock(side_effect=[process(), process()])) as run:
            await caller.think("call", None)
            await caller.think("chat", "old", chat=True)
        first, second = [c.args for c in run.await_args_list]
        self.assertIn(caller.PHONE_PROMPT + "\nTIME ONE", first)
        self.assertIn(caller.CHAT_PROMPT + "\nTIME TWO", second)
        self.assertIn("--resume", second)
        self.assertNotIn("--resume", first)

    def test_istanbul_and_utc_clock(self):
        class Clock:
            @staticmethod
            def now(tz):
                return datetime(2026, 11, 2, 22, 30, tzinfo=timezone.utc)
        with patch.object(caller, "datetime", Clock), patch.object(caller, "TZ", ZoneInfo("Europe/Istanbul")):
            context = caller.time_context()
        self.assertIn("2026-11-03T01:30:00+03:00", context)
        self.assertIn("2026-11-02T22:30:00+00:00", context)
        self.assertIn("Europe/Istanbul", context)


if __name__ == "__main__":
    unittest.main()
