"""Cancellation, process reuse and bounded audio/protocol regression tests; no providers."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
import caller
from claude_stream import Conversation, MAX_RESPONSE, StreamFailure, StreamUnavailable, read_turn
from voice_worker import VoiceWorker
import models


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_sentence_boundaries_preserve_english_persian_and_decimals(self):
        for text, expected in (
                ("Very good, sir. The skies are clear! Shall I", ["Very good, sir.", "The skies are clear!", "Shall I"]),
                ("بله قربان. هوا صافه؟ الان", ["بله قربان.", "هوا صافه؟", "الان"]),
                ("It costs 3.5 dollars. Ok", ["It costs 3.5 dollars.", "Ok"])):
            reader, queue = asyncio.StreamReader(), asyncio.Queue()
            events = [{"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": text}}},
                      {"type": "result", "result": text}]
            reader.feed_data("".join(json.dumps(e) + "\n" for e in events).encode())
            self.assertEqual((await read_turn(reader, queue))[0], text)
            self.assertEqual([queue.get_nowait() for _ in range(queue.qsize())], expected)

    async def test_protocol_reuses_child_and_keeps_turns_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.py"
            path.write_text('import json,sys\nfor line in sys.stdin:\n m=json.loads(line)\n print(json.dumps({"type":"result","result":m["message"]["content"],"session_id":"session-one"}), flush=True)\n')
            conversation = Conversation([sys.executable, "-u", str(path)], tmp, 10)
            try:
                self.assertEqual(await conversation.ask("first context"), ("first context", "session-one"))
                process = conversation.proc
                self.assertEqual(await conversation.ask("second context"), ("second context", "session-one"))
                self.assertIs(conversation.proc, process)
            finally:
                await conversation.close()
            self.assertIsNotNone(process.returncode)

    async def test_partial_protocol_never_signals_safe_retry(self):
        for data in (b'{}\n', b'not-json\n', b'{"type":"result","is_error":true}\n'):
            reader = asyncio.StreamReader()
            reader.feed_data(data)
            reader.feed_eof()
            with self.assertRaises(RuntimeError) as raised:
                await read_turn(reader)
            self.assertNotIsInstance(raised.exception, StreamUnavailable)
        empty = asyncio.StreamReader()
        empty.feed_eof()
        with self.assertRaises(StreamUnavailable):
            await read_turn(empty)

    async def test_response_is_bounded_and_long_sentences_are_chunked(self):
        reader, queue = asyncio.StreamReader(), asyncio.Queue()
        reader.feed_data((json.dumps({"type":"result","result":"x" * 1801}) + "\n").encode())
        await read_turn(reader, queue)
        self.assertEqual([len(queue.get_nowait()) for _ in range(queue.qsize())], [600, 600, 600, 1])
        reader.feed_data((json.dumps({"type":"result","result":"x" * (MAX_RESPONSE + 1)}) + "\n").encode())
        with self.assertRaises(RuntimeError):
            await read_turn(reader)

    async def test_complete_assistant_text_without_deltas_is_used_once(self):
        reader, queue = asyncio.StreamReader(), asyncio.Queue()
        events = [
            {"type": "assistant", "parent_tool_use_id": "child", "message": {"content": [
                {"type": "text", "text": "subagent content"}]}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "A complete answer."}]}},
            {"type": "result", "subtype": "success", "session_id": "session-2"},
        ]
        reader.feed_data("".join(json.dumps(e) + "\n" for e in events).encode())
        result = await read_turn(reader, queue)
        self.assertEqual(result, ("A complete answer.", "session-2"))
        self.assertEqual(queue.get_nowait(), "A complete answer.")
        self.assertTrue(queue.empty())

    async def test_empty_error_malformed_and_partial_results_are_not_replayed(self):
        cases = [
            ([{"type": "result", "result": ""}], "empty_result"),
            ([{"type": "result", "subtype": "error_max_turns"}], "error_max_turns"),
            ([{"type": "result", "is_error": True, "subtype": "private-detail"}], "error_result"),
            ([{"type": "stream_event", "event": [1]}], "invalid_event"),
            ([{"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "Partial."}}}],
             "eof_before_result"),
        ]
        for events, reason in cases:
            reader = asyncio.StreamReader()
            reader.feed_data("".join(json.dumps(e) + "\n" for e in events).encode())
            reader.feed_eof()
            with self.assertRaises(StreamFailure) as raised:
                await read_turn(reader)
            self.assertNotIsInstance(raised.exception, StreamUnavailable)
            self.assertEqual(raised.exception.reason, reason)

    async def test_failed_process_is_reaped_and_logs_only_fixed_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.py"
            path.write_text('import sys\nsys.stdin.readline()\nsys.stderr.write("private-provider-detail\\n")\nsys.exit(7)\n')
            conversation = Conversation([sys.executable, "-u", str(path)], tmp, 10)
            with self.assertLogs("telegram-caller", level="WARNING") as logs:
                with self.assertRaises(StreamUnavailable):
                    await conversation.ask("private-user-message")
            logged = "\n".join(logs.output)
            self.assertIn("reason=eof_before_output", logged)
            self.assertIn("stage=read", logged)
            self.assertIn("stderr_bytes=", logged)
            self.assertNotIn("private-", logged)
            self.assertNotIn(tmp, logged)
            self.assertIsNone(conversation.proc)
            self.assertIsNone(conversation.stderr)

    async def test_failed_call_stream_restarts_next_turn_with_last_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(caller, "BASE", Path(tmp)), \
             patch.object(caller, "CLAUDE_CALL_PROCESS", "stream"), \
             patch.object(caller, "think", AsyncMock()) as fallback:
            call = caller.Call(None, 1)
            call.session_id = "last-success"
            call._memory_snapshot = ""
            call.conversation = SimpleNamespace(ask=AsyncMock(side_effect=StreamFailure("eof_before_result")))
            with self.assertRaises(StreamFailure):
                await call.model_turn("failed turn", None)
            fallback.assert_not_awaited()
            self.assertIsNone(call.conversation)
            fresh = SimpleNamespace(ask=AsyncMock(return_value=("ok", "last-success")), close=AsyncMock())
            with patch.object(caller, "Conversation", return_value=fresh) as spawn:
                self.assertEqual(await call.model_turn("next turn", None), ("ok", "last-success"))
            args = spawn.call_args.args[0]
            self.assertEqual(args[args.index("--resume") + 1], "last-success")
            await call.close()

    async def test_no_output_falls_back_then_retries_stream_before_disabling(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(caller, "BASE", Path(tmp)), \
             patch.object(caller, "CLAUDE_CALL_PROCESS", "stream"), \
             patch.object(caller, "think", AsyncMock(return_value=("fallback", "fallback-session"))) as fallback:
            call = caller.Call(None, 1)
            broken = SimpleNamespace(ask=AsyncMock(side_effect=StreamUnavailable("eof_before_output")))
            with patch.object(caller, "Conversation", return_value=broken) as spawn:
                result = await call.model_turn("first", None)
                call.session_id = result[1]
                self.assertFalse(call.stream_disabled)
                await call.model_turn("second", None)
                self.assertTrue(call.stream_disabled)
                await call.model_turn("third", None)
                self.assertEqual(spawn.call_count, 2)
                args = spawn.call_args.args[0]
                self.assertEqual(args[args.index("--resume") + 1], "fallback-session")
            self.assertEqual(fallback.await_count, 3)
            await call.close()

    async def test_close_still_stops_child_when_stdin_close_fails(self):
        def broken():
            raise BrokenPipeError()
        conversation = Conversation([], ".", 1)
        process = conversation.proc = SimpleNamespace(returncode=None, stdin=SimpleNamespace(close=broken))
        with patch("claude_stream.stop", AsyncMock()) as stop:
            await conversation.close()
            stop.assert_awaited_once_with(process)
        self.assertIsNone(conversation.proc)

    async def test_timeout_reaps_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.py"
            path.write_text('import sys,time\nfor line in sys.stdin:\n time.sleep(30)\n')
            conversation = Conversation([sys.executable, "-u", str(path)], tmp, .2)
            with self.assertRaises(TimeoutError):
                await conversation.ask("hello")
            self.assertIsNone(conversation.proc)

    async def test_barge_in_cancels_model_and_voice_without_losing_speech(self):
        call = caller.Call(SimpleNamespace(send_frame=AsyncMock()), 1)
        stopped = asyncio.Event()
        async def slow(*_):
            try:
                await asyncio.sleep(30)
            finally:
                stopped.set()
        call.model_turn = slow
        task = asyncio.create_task(call.respond("hello"))
        await asyncio.sleep(.02)
        call.ear.turns.put_nowait(b"new speech")
        call.ear.speech_started.set()
        self.assertFalse(await asyncio.wait_for(task, 2))
        self.assertTrue(stopped.is_set())
        self.assertEqual(call.ear.turns.get_nowait(), b"new speech")
        await call.close()

    async def test_hello_while_voice_loads_does_not_cut_the_greeting(self):
        sent = []
        async def send_frame(_chat, _device, chunk):
            sent.append(chunk)
        call = caller.Call(SimpleNamespace(send_frame=send_frame), 1)
        loading = asyncio.Event()
        async def slow_voice(_text):
            await loading.wait()
            return b"\1" * caller.FRAME_BYTES * 3
        call.synthesize = slow_voice
        with patch.object(caller, "BARGE_IN", True):
            task = asyncio.create_task(call.say("Hello, how can I help?", opening=True))
            await asyncio.sleep(.02)
            # The owner says "hello?" while the voice is still loading.
            self.assertFalse(call.ear.active)
            loud = (b"\xff\x7f\x01\x80" * (caller.FRAME_BYTES // 4))
            for _ in range(60):
                call.ear.feed(loud)
            self.assertFalse(call.ear.speech_started.is_set())
            loading.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(len(sent), 3)
        self.assertTrue(call.ear.active)
        await call.close()

    async def test_incoming_call_slot_reserved_before_answer_await(self):
        assistant = object.__new__(caller.Assistant)
        assistant.owner_id, assistant.call, assistant.call_job = 7, None, None
        assistant.answer = AsyncMock(side_effect=lambda *_: asyncio.sleep(30))
        # Use a real waiting coroutine, rather than returning an unawaited coroutine from a mock.
        started = []
        async def answer(chat_id):
            started.append(chat_id)
            await asyncio.sleep(30)
        assistant.answer = answer
        update = SimpleNamespace(chat_id=7)
        await assistant.on_ring(None, update)
        first = assistant.call_job
        await assistant.on_ring(None, update)
        self.assertIs(assistant.call_job, first)
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(started, [7])

    async def test_declined_call_still_texts_the_message(self):
        # py-tgcalls fails play() with CallDeclined, then reports DISCARDED_CALL to on_hangup
        # before play()'s caller has run. The hang-up must not cancel the fallback text.
        assistant = object.__new__(caller.Assistant)
        assistant.owner_id, assistant.call, assistant.call_job = 7, None, None
        ringing = asyncio.get_running_loop().create_future()
        assistant.calls = SimpleNamespace(play=lambda *a, **k: ringing)
        assistant.client = SimpleNamespace(send_message=AsyncMock())
        assistant.launch_call(assistant.place_call, "Take your medicine")
        job = assistant.call_job
        await asyncio.sleep(0)
        ringing.set_exception(caller.CallDeclined(7))
        await assistant.on_hangup(None, SimpleNamespace(chat_id=7))
        await asyncio.gather(job, return_exceptions=True)
        assistant.client.send_message.assert_awaited_once()
        self.assertIn("Take your medicine", assistant.client.send_message.await_args.args[1])
        self.assertIsNone(assistant.call_job)

    async def test_memory_change_closes_old_persistent_session(self):
        call = caller.Call(None, 1, memory_context=lambda:"new facts")
        old = SimpleNamespace(close=AsyncMock())
        call.conversation, call.session_id, call._memory_snapshot = old, "old", "old facts"
        with patch.object(caller, "CLAUDE_CALL_PROCESS", "per-turn"), patch.object(caller, "think", AsyncMock(return_value=("ok", "new"))) as think:
            await call.model_turn("hello", None)
            self.assertIsNone(think.await_args.args[1])
            self.assertEqual(think.await_args.kwargs["context"], "new facts")
        old.close.assert_awaited_once()

    async def test_voice_is_lazy_and_validates_model_choice(self):
        with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            voice = VoiceWorker("missing")
            await voice.close()
            spawn.assert_not_awaited()
        with self.assertRaises(ValueError):
            VoiceWorker("missing", "../../other")

    async def test_cleanup_error_still_releases_the_call_and_leaves_telegram(self):
        assistant = object.__new__(caller.Assistant)
        assistant.calls = SimpleNamespace(record=AsyncMock(),leave_call=AsyncMock())
        assistant.memory_context = lambda: ""
        call = SimpleNamespace(persian=False,converse=AsyncMock(),ended=asyncio.Event(),
                               close=AsyncMock(side_effect=RuntimeError("cleanup")))
        with patch.object(caller,"Call",return_value=call):
            with self.assertRaises(RuntimeError):
                await assistant.talk(7,"hello",False)
        self.assertIsNone(assistant.call)
        assistant.calls.leave_call.assert_awaited_once_with(7)


class ModelTests(unittest.TestCase):
    def test_bad_download_keeps_existing_file(self):
        import io
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kokoro-v1.0.onnx"
            path.write_bytes(b"previous model")
            with patch.object(models.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaises(RuntimeError):
                    models.install(Path(tmp), path.name)
            self.assertEqual(path.read_bytes(), b"previous model")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [path.name])

    def test_env_is_data_never_shell_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings"
            path.write_text('TTS_EN=kokoro\nKOKORO_MODEL="int8"\nOTHER=secret\n')
            self.assertEqual(models.settings(path), {"TTS_EN":"kokoro", "KOKORO_MODEL":"int8"})


if __name__ == "__main__":
    unittest.main()
