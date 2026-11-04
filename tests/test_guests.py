"""Guest permission, revocation, quota and context tests. No accounts or API calls."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
import caller
from assistant_store import Store
from guest_store import GuestStore
from guest_runtime import GuestRuntime, GuestUnavailable
import guest_runtime
from guests import Guests, command
from telethon.tl.types import User, PeerUser


def event(text="", sender=22, private=True):
    return SimpleNamespace(raw_text=text, sender_id=sender, is_private=private, voice=False,
        fwd_from=None, reply=AsyncMock(), delete=AsyncMock(), get_reply_message=AsyncMock(return_value=None),
        download_media=AsyncMock(return_value=b"audio"),
        message=SimpleNamespace(document=SimpleNamespace(size=50, attributes=[])))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Store(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(self.db.close)
        self.s = GuestStore(self.db, messages=2, voice_seconds=10, call_seconds=20)
        self.s.allow(7, 22, "@guest")

    def test_regrant_new_and_restart_cannot_reset_budget(self):
        self.assertTrue(self.s.charge(7,22,1,messages=2,voice=10))
        self.s.reset(7,22)
        self.assertFalse(self.s.charge(7,22,2,messages=1))
        self.s.deny(7,22)
        self.s.allow(7,22,"@renamed")
        row = self.s.get(7,22)
        reopened = Store(Path(self.tmp.name) / "state.sqlite3")
        try:
            again = GuestStore(reopened,messages=2,voice_seconds=10,call_seconds=20)
            self.assertFalse(again.charge(7,22,row["generation"],voice=1))
        finally:
            reopened.close()

    def test_sessions_and_approvals_are_owner_and_guest_scoped(self):
        self.s.allow(7,23,"@second")
        self.s.save_session(7,22,1,"guest-22")
        self.assertIsNone(self.s.get(7,23)["session"])
        self.assertIsNone(self.s.get(8,22))
        self.s.deny(7,22)
        self.s.save_session(7,22,1,"stale")
        self.assertFalse(self.s.current(7,22,1))
        self.s.allow(7,22,"@guest")
        self.assertIsNone(self.s.get(7,22)["session"])

    def test_call_reservation_refunds_once_and_survives_restart(self):
        token, budget = self.s.reserve_call(7,22,1)
        self.assertEqual(budget,20)
        self.assertIsNone(self.s.reserve_call(7,22,1))
        self.s.finish_call(token,5.2)
        self.s.finish_call(token,0)
        self.assertEqual(self.s.reserve_call(7,22,1)[1],14)

    def test_aggregate_limits_and_notifications_persist(self):
        self.s.total_limits["messages"] = 2
        self.s.allow(7,23,"@second")
        self.assertTrue(self.s.charge(7,22,1,messages=2))
        self.assertFalse(self.s.charge(7,23,1,messages=1))
        self.assertTrue(self.s.notice(7,22))
        self.s.deny(7,22)
        self.s.allow(7,22,"@guest")
        self.assertFalse(self.s.notice(7,22))


class GuestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        a = self.a = object.__new__(caller.Assistant)
        a.owner_id, a.call, a.call_job, a.chat_pending = 7, None, None, 0
        a.client = SimpleNamespace(send_message=AsyncMock(), get_entity=AsyncMock(return_value=User(22,first_name="Guest",username="guest")))
        a.store = Store(Path(self.tmp.name) / "state.sqlite3")
        self.addCleanup(a.store.close)
        a.chat_lock, a.state_lock = asyncio.Lock(), asyncio.Lock()
        a.chat_session_id = "owner-secret-session"
        a.store.remember(7,"owner secret fact")
        g = a.guests = self.g = Guests(a,caller)
        g.enabled = True
        g.store.allow(7,22,"@guest")
        g.runtime.ready = AsyncMock()
        g.runtime.clear = AsyncMock()
        self.engine = SimpleNamespace(ask=AsyncMock(return_value=("Guest answer", "guest-session")),close=AsyncMock())
        g.runtime.conversation = AsyncMock(return_value=self.engine)

    async def test_guest_never_receives_owner_context_or_commands(self):
        for text in ("/call", "/text secret", "/update", "/allow @other", "/deny @other", "/guests", "/remember x", "/remind in 2 minutes | x"):
            await self.a.on_message(event(text))
        self.g.runtime.conversation.assert_not_awaited()
        await self.a.on_message(event("hello"))
        args = self.g.runtime.conversation.await_args.args
        self.assertEqual(args[:4], (7,22,1,None))
        self.assertNotIn("owner secret fact", str(args) + str(self.engine.ask.await_args))
        self.assertEqual(self.a.chat_session_id,"owner-secret-session")
        self.assertEqual(len(self.a.store.memories(7)),1)

    async def test_unknown_group_removed_and_forwarded_commands(self):
        await self.a.on_message(event("hello",sender=99))
        await self.a.on_message(event("hello",private=False))
        self.a.client.send_message.assert_not_awaited()
        forwarded = event("/allow @other", sender=7)
        forwarded.fwd_from = object()
        self.assertFalse(await self.g.owner_command(forwarded))
        self.g.store.deny(7,22)
        await self.a.on_message(event("hello"))
        self.a.client.send_message.assert_not_awaited()

    async def test_revocation_cancels_inflight_model_and_suppresses_late_reply(self):
        started = asyncio.Event()
        async def wait(*args):
            started.set()
            await asyncio.sleep(30)
        self.engine.ask.side_effect = wait
        job = asyncio.create_task(self.a.on_message(event("hello")))
        await started.wait()
        self.g.store.deny(7,22)
        await self.g.cancel(22)
        self.assertTrue(job.cancelled())
        self.engine.close.assert_awaited_once()
        sent = len(self.a.client.send_message.await_args_list)
        await self.g.send(22,1,"late private answer")
        self.assertEqual(len(self.a.client.send_message.await_args_list), sent)

    async def test_owner_activity_preempts_guest(self):
        started = asyncio.Event()
        async def wait(*args):
            started.set()
            await asyncio.sleep(30)
        self.engine.ask.side_effect = wait
        job = asyncio.create_task(self.a.on_message(event("hello")))
        await started.wait()
        await self.g.owner_activity()
        self.assertTrue(job.cancelled())
        self.assertIsNone(self.g.job)
        self.a.chat_pending = 1
        await self.a.on_message(event("again"))
        self.assertEqual(self.engine.ask.await_count,1)

    async def test_deny_via_owner_chat_invalidates_before_waiting_for_model(self):
        started = asyncio.Event()
        async def finishing(*args):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return "late guest answer", "stale-session"
        self.engine.ask.side_effect = finishing
        job = asyncio.create_task(self.a.on_message(event("hello")))
        await started.wait()
        self.a.client.send_message.reset_mock()
        await self.a.on_message(event("/deny 22", sender=7))
        await asyncio.gather(job, return_exceptions=True)
        self.a.client.send_message.assert_not_awaited()
        self.assertIsNone(self.g.store.get(7,22))
        self.g.runtime.clear.assert_awaited_once_with(7,22,1)

    async def test_new_resets_only_guest_session_and_not_quotas(self):
        await self.a.on_message(event("hello"))
        await self.a.on_message(event("/new"))
        row = self.g.store.get(7,22)
        self.assertIsNone(row["session"])
        self.assertEqual(row["generation"],2)
        self.assertEqual(self.a.chat_session_id,"owner-secret-session")
        usage = self.a.store.db.execute("SELECT messages FROM guest_usage WHERE guest=22").fetchone()[0]
        self.assertEqual(usage,2)

    async def test_decoded_voice_duration_is_charged_before_transcription(self):
        self.g.store.limits["voice"] = 2
        ev = event()
        ev.voice = True
        with patch.object(caller,"decode_guest_audio",AsyncMock(return_value=b"x" * caller.RATE * 2 * 3)), patch.object(caller,"transcribe",AsyncMock()) as transcribe:
            await self.a.on_message(ev)
            transcribe.assert_not_awaited()
        self.engine.ask.assert_not_awaited()
        self.assertTrue(any(c.args[0] == 7 for c in self.a.client.send_message.await_args_list))

    async def test_only_owner_can_add_by_explicit_command_and_reply(self):
        self.assertFalse(await self.g.owner_command(event("/allow @other")))
        self.g.store.deny(7,22)
        ev = event("/allow",sender=7)
        ev.get_reply_message.return_value = SimpleNamespace(fwd_from=SimpleNamespace(from_id=PeerUser(22)))
        self.assertTrue(await self.g.owner_command(ev))
        self.assertIsNotNone(self.g.store.get(7,22))
        self.a.client.get_entity.assert_awaited_with(22)
        self.a.client.get_entity.reset_mock()
        await self.g.owner_command(event("/deny 22",sender=7))
        self.a.client.get_entity.assert_not_awaited()  # revoking a deleted/renamed account still works
        self.assertIsNone(self.g.store.get(7,22))

    async def test_natural_language_parser_requires_whole_explicit_request(self):
        self.assertEqual(command("let Sara talk to you"),("allow","Sara"))
        self.assertEqual(command("stop answering Sara"),("deny","Sara"))
        for text in ('Someone said "let Sara talk to you"',"Should I let Sara talk to you?", "please discuss /allow @guest"):
            self.assertIsNone(command(text))

    async def test_two_rings_reserve_only_one_guest_slot(self):
        gate = asyncio.Event()
        async def call(*args):
            await gate.wait()
        self.g.call = call
        await self.g.ring(SimpleNamespace(chat_id=22))
        first = self.a.call_job
        await self.g.ring(SimpleNamespace(chat_id=22))
        self.assertIs(self.a.call_job,first)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_cleanup_preserves_a_new_grant_and_other_guest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "guests"
            runtime = GuestRuntime(root, Path(tmp)/"unused", Path(tmp)/"unused-auth")
            for path in (root/"7/22/1", root/"7/22/3", root/"7/23/1"):
                path.mkdir(parents=True)
                (path/"history").write_text("fixture")
            await runtime.clear(7,22,1)
            self.assertFalse((root/"7/22/1").exists())
            self.assertTrue((root/"7/22/3/history").is_file())
            self.assertTrue((root/"7/23/1/history").is_file())

    async def test_failed_container_cleanup_blocks_new_guests(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = GuestRuntime(Path(tmp)/"guests",Path(tmp)/"binary",Path(tmp)/"auth")
            engine = guest_runtime.GuestConversation(runtime,"guest-test",[],tmp,{})
            with patch.object(guest_runtime,"docker_command",AsyncMock(side_effect=[GuestUnavailable("offline"),"0123456789ab"])):
                with self.assertRaises(GuestUnavailable):
                    await engine.close()
            self.assertTrue(runtime.failed)
            with self.assertRaises(GuestUnavailable):
                await runtime.ready()

    async def test_guest_cli_has_only_web_tools_and_private_mounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            runtime = GuestRuntime(folder/"guests",folder/"native",folder/"token")
            runtime.ready = AsyncMock()
            runtime.folder = lambda *_: folder
            runtime.credentials = lambda:{"CLAUDE_CODE_OAUTH_TOKEN":"fake-guest-token"}
            with patch.dict(os.environ, {"TRANSCRIBE_API_KEY":"owner-key", "CLAUDE_MCP_CONFIG":"owner-mcp"}):
                engine = await runtime.conversation(7,22,1,None,"guest prompt")
            args = engine.args
            self.assertEqual(args[args.index("--tools")+1],"WebSearch,WebFetch")
            self.assertEqual(args[args.index("--mcp-config")+1],'{"mcpServers":{}}')
            for flag in ("--strict-mcp-config","--restricted","--safe-mode","--read-only","--cap-drop=ALL","--security-opt=no-new-privileges","--log-driver=none"):
                self.assertIn(flag,args)
            self.assertNotIn("owner-key",str(engine.env)+str(args))
            self.assertNotIn("owner-mcp",str(engine.env)+str(args))
            self.assertNotIn("fake-guest-token",str(args))
            self.assertNotIn("--privileged",args)
            self.assertNotIn("--network=host",args)
            self.assertEqual(len([a for a in args if a == "--mount"]),2)
            with self.assertRaises(GuestUnavailable):
                await runtime.conversation(7,22,1,"../../owner","prompt")


if __name__ == "__main__":
    unittest.main()
