"""Offline chat regression tests: no accounts, API keys or messages sent."""
import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
        # Saved conversations and Claude's transcripts live in a temporary folder.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.a.store = caller.Store(self.tmp / "assistant.sqlite3")
        self.a.state_lock = asyncio.Lock()
        self.addCleanup(self.a.store.close)
        for sid in ("session-1", "old-session"):
            (self.tmp / f"{sid}.jsonl").write_text("{}")
        for name, value in (("CHAT_FILE", self.tmp / "chat.json"), ("history_dir", lambda: self.tmp)):
            p = patch.object(caller, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def test_memory_survives_restart_until_new(self):
        await self.a.on_message(event("My name is Sara"))
        self.assertEqual(caller.load_chat_session(), "session-1")  # what a restarted assistant reads
        await self.a.on_message(event("/new"))
        self.assertIsNone(caller.load_chat_session())

    async def test_missing_transcript_starts_fresh(self):
        self.a.chat_session_id = "cleaned-up"  # its file was removed by the storage limit
        await self.a.on_message(event("hello"))
        self.assertIsNone(self.think.await_args.args[1])
        self.assertEqual(self.a.chat_session_id, "session-1")

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

    async def test_reply_markdown_bold_is_removed(self):
        self.think.return_value = ("It is **sunny** today", "session-1")
        ev = event("weather?")
        await self.a.on_message(ev)
        self.assertEqual(ev.reply.await_args.args[0], "It is sunny today")

    async def test_backpressure(self):
        self.a.chat_pending = 10
        await self.a.on_message(event("hi"))
        self.think.assert_not_awaited()
        self.assertEqual(self.a.chat_pending, 10)

    async def test_saved_facts_are_explicit_and_forget_resets_context(self):
        self.a.chat_session_id = "old-session"
        await self.a.on_message(event("Remember that I like **tea**"))
        self.think.assert_not_awaited()
        self.assertEqual(self.a.chat_session_id, "old-session")  # saving a fact keeps the chat
        self.assertEqual(self.a.store.memories(7)[0]["text"], "I like **tea**")
        await self.a.on_message(event("What should I drink?"))
        self.assertEqual(self.think.await_args.args[1], "old-session")
        self.assertIn("I like **tea**", self.think.await_args.kwargs["context"])
        await self.a.on_message(event("/forget 1"))
        self.assertEqual(self.a.store.memories(7), [])
        self.assertIsNone(self.a.chat_session_id)
        await self.a.on_message(event("Another question"))
        self.assertNotIn("context", self.think.await_args.kwargs)

    async def test_new_preserves_facts_and_reminders(self):
        await self.a.on_message(event("/remember My exam is Friday"))
        await self.a.on_message(event("Remind me in 20 minutes to study"))
        await self.a.on_message(event("/new"))
        self.assertEqual(len(self.a.store.memories(7)), 1)
        self.assertEqual(len(self.a.store.reminders(7)), 1)
        self.think.assert_not_awaited()

    async def test_only_private_owner_can_change_saved_state(self):
        for text in ["/remember private fact", "Remind me in 20 minutes to study"]:
            for ev in [event(text, sender=8), event(text, private=False)]:
                await self.a.on_message(ev)
                ev.reply.assert_not_awaited()
        self.assertEqual(self.a.store.memories(7), [])
        self.assertEqual(self.a.store.reminders(7), [])
        self.think.assert_not_awaited()

    async def test_forwarded_commands_never_act(self):
        self.a.place_call = AsyncMock()
        self.a.start_updates = AsyncMock()
        for text in ["/update", "/call", "/new", "/remember a fact",
                     "Remind me in 20 minutes to study"]:
            ev = event(text)
            ev.fwd_from = object()
            await self.a.on_message(ev)
            self.assertIs(self.think.await_args.kwargs["actions"], False)
            self.assertIs(self.think.await_args.kwargs["chat"], True)
        self.a.place_call.assert_not_awaited()
        self.a.start_updates.assert_not_awaited()
        self.assertEqual(self.a.store.memories(7), [])
        self.assertEqual(self.a.store.reminders(7), [])

    async def test_voice_can_create_reminder_but_forwarded_voice_cannot(self):
        with patch.object(caller, "transcribe_audio", AsyncMock(return_value="Remind me in 20 minutes to study")):
            await self.a.on_message(event(voice=True))
            self.think.assert_not_awaited()
            ev = event(voice=True)
            ev.fwd_from = object()
            await self.a.on_message(ev)
        self.assertEqual(len(self.a.store.reminders(7)), 1)
        self.assertIs(self.think.await_args.kwargs["actions"], False)

    def due(self, text="Study", owner=7):
        return self.a.store.add_reminder(owner, text,
            datetime.now(timezone.utc) - timedelta(minutes=1), "UTC")

    async def test_delivery_does_not_need_model_or_chat_lock(self):
        self.due()
        self.due("Another owner's reminder", owner=8)
        async with self.a.chat_lock:
            await asyncio.wait_for(self.a.deliver_reminders(), 1)
        self.think.assert_not_awaited()
        self.a.client.send_message.assert_awaited_once()
        self.assertEqual(self.a.client.send_message.await_args.args[0], 7)
        self.assertIsNone(self.a.store.reminders(7)[0]["next_due"])
        self.assertEqual(len(self.a.store.due_reminders(8, datetime.now(timezone.utc))), 1)
        await self.a.deliver_reminders()
        self.assertEqual(self.a.client.send_message.await_count, 1)

    async def test_failed_delivery_retries_and_snooze_can_resend(self):
        ident = self.due()
        self.a.client.send_message.side_effect = OSError("offline")
        with self.assertLogs("telegram-caller", level="WARNING"):
            await self.a.deliver_reminders()
        self.assertIsNotNone(self.a.store.reminders(7)[0]["next_due"])
        self.a.client.send_message.side_effect = None
        await self.a.deliver_reminders()
        self.assertIsNone(self.a.store.reminders(7)[0]["next_due"])
        await self.a.on_message(event(f"/snooze {ident} 20"))
        self.assertGreater(self.a.store.reminders(7)[0]["next_due"], datetime.now(timezone.utc).timestamp())
        await self.a.on_message(event(f"/done {ident}"))
        self.assertEqual(self.a.store.reminders(7), [])

    async def test_done_waits_for_inflight_delivery_and_is_not_overwritten(self):
        ident = self.due()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
        self.a.client.send_message.side_effect = send
        sending = asyncio.create_task(self.a.deliver_reminders())
        await entered.wait()
        done = asyncio.create_task(self.a.personal_command(f"/done {ident}"))
        await asyncio.sleep(0)
        self.assertFalse(done.done())
        release.set()
        await asyncio.gather(sending, done)
        self.assertEqual(self.a.store.reminders(7), [])

    async def test_reminder_unicode_chunks_are_lossless(self):
        self.due("🙂" * 2000)
        await self.a.deliver_reminders()
        calls = self.a.client.send_message.await_args_list
        self.assertEqual(len(calls), 2)
        chunks = [c.args[1] for c in calls]
        self.assertIn("🙂" * 2000, "".join(chunks))
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 4096 for c in chunks))
        self.assertTrue(all(c.kwargs["parse_mode"] is None for c in calls))

    def test_session_file_owner_and_path_validation(self):
        self.a.save_chat_session("session-1")
        self.assertEqual(caller.load_chat_session(7), "session-1")
        self.assertIsNone(caller.load_chat_session(8))
        caller.CHAT_FILE.write_text(json.dumps({"session_id": "../outside"}))
        self.assertIsNone(caller.load_chat_session(7))
        for invalid in ("[]", "null", "1", "{broken"):
            caller.CHAT_FILE.write_text(invalid)
            self.assertIsNone(caller.load_chat_session(7))

    def test_memory_context_budget_keeps_whole_facts(self):
        for i in range(10):
            self.a.store.remember(7, str(i) + "🙂" * 1900)
        context = self.a.memory_context()
        self.assertLess(len(context.encode("utf-8")), 12500)
        self.assertIn("Some saved facts are omitted", context)
        self.assertIn("0" + "🙂" * 1900, context)


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
        inputs = []
        def process():
            stdout = asyncio.StreamReader()
            stdout.feed_data((json.dumps({"type": "result", "result": "OK", "session_id": "new"}) + "\n").encode())
            stdout.feed_eof()
            return SimpleNamespace(stdout=stdout, stderr=SimpleNamespace(read=AsyncMock(return_value=b"")),
                stdin=SimpleNamespace(write=inputs.append, drain=AsyncMock(), close=lambda: None),
                wait=AsyncMock(return_value=0), returncode=0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(caller, "BASE", Path(tmp)), \
             patch.object(caller, "time_context", side_effect=["TIME ONE", "TIME TWO"]), \
             patch.object(asyncio, "create_subprocess_exec", AsyncMock(side_effect=[process(), process()])) as run:
            await caller.think("call", None)
            await caller.think("chat", "old", chat=True)
        first, second = [c.args for c in run.await_args_list]
        self.assertIn(caller.PHONE_PROMPT, first)
        self.assertIn(caller.CHAT_PROMPT, second)
        self.assertIn(b"TIME ONE", inputs[0])
        self.assertIn(b"TIME TWO", inputs[1])
        self.assertIn("--resume", second)
        self.assertNotIn("--resume", first)
        for args in (first, second):
            self.assertIn("--strict-mcp-config", args)
            self.assertEqual(args[args.index("--tools") + 1], "WebSearch,WebFetch")
            self.assertEqual(args[args.index("--setting-sources") + 1], "")
            self.assertNotIn("--dangerously-skip-permissions", args)
            self.assertIn("mcp__telegram__send_message", args[args.index("--disallowedTools") + 1])
        self.assertIn("mcp__telegram__update_server", first[first.index("--disallowedTools") + 1])
        self.assertNotIn("mcp__telegram__update_server", second[second.index("--disallowedTools") + 1])

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
