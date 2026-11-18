"""Offline copy jobs: real Telethon types and disk transfers, fake Telegram I/O."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-connector"))
from telethon import TelegramClient, errors
from telethon.extensions import html
from telethon.tl import types
from copy_jobs import CopyJobs, rewrite_html
from transfers import MB, Transfers
import video_tools

SOURCE = -1000000001111
TARGET = -1000000002222


def message(source_id, text="", *, data=None, name="lesson.pdf", attributes=(),
            mime="application/pdf", grouped_id=None, entities=None, media=None, **kwargs):
    if data is not None:
        document = types.Document(source_id, 42, b"fresh", datetime.now(timezone.utc),
            mime, len(data), 1, [types.DocumentAttributeFilename(name), *attributes])
        media = types.MessageMediaDocument(document=document)
    result = types.Message(source_id, types.PeerChannel(1111), message=text,
        date=datetime.now(timezone.utc), media=media, grouped_id=grouped_id, entities=entities, **kwargs)
    result._data = data
    result._thumb = b"thumbnail"
    return result


class Clock:
    def __init__(self):
        self.now = time.time()
        self.waits = []
        self.on_sleep = None

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        if self.on_sleep:
            self.on_sleep(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeClient:
    def __init__(self, clock, messages=(), protected=False):
        self.clock = clock
        self.source = types.Channel(1111, "Source", types.ChatPhotoEmpty(), None,
                                    broadcast=True, noforwards=protected)
        self.target = types.Channel(2222, "Target", types.ChatPhotoEmpty(), None, broadcast=True)
        self.me = types.User(3, first_name="Owner", premium=False)
        self.permission = SimpleNamespace(has_left=False, is_creator=True, is_admin=True,
                                          post_messages=True, participant=SimpleNamespace())
        self.messages = {m.id: m for m in messages}
        self.target_messages, self.calls, self.uploads, self.downloads, self.reads = [], [], [], [], []
        self.upload_data = {}
        self.send_errors = deque()
        self.album_error = None
        self.accept_error = None
        self.gate_kind, self.gate, self.entered = None, None, asyncio.Event()
        self.accept_before_gate = False
        self.next_id = 100

    async def get_me(self):
        return self.me

    async def resolve(self, client, chat):
        return self.source if str(chat) in (str(SOURCE), "source") else self.target

    async def get_permissions(self, target, who):
        return self.permission

    async def get_messages(self, entity, ids=None, limit=None, **kwargs):
        if ids is None:
            return sorted(self.messages.values(), key=lambda m: m.id, reverse=True)[:limit]
        self.reads.append(ids)
        if isinstance(ids, list):
            return [self.messages.get(n) for n in ids]
        return self.messages.get(ids)

    async def iter_messages(self, entity, *, reverse=False, min_id=0, max_id=0, limit=None):
        values = self.messages.values() if entity.id == self.source.id else self.target_messages
        values = sorted(values, key=lambda m: m.id, reverse=not reverse)
        values = [m for m in values if m.id > min_id and (not max_id or m.id < max_id)]
        for m in values[:limit]:
            yield m

    async def download_media(self, msg, file, thumb=None, progress_callback=None):
        self.downloads.append((msg.id, thumb))
        data = msg._data if thumb is None else msg._thumb
        # Like Telethon's _download_file: write chunks, then report f.tell().
        for start in range(0, len(data or b""), 4):
            file.write(data[start:start + 4])
            if progress_callback:
                progress_callback(file.tell(), len(data))
        return file

    async def upload_file(self, file, *, file_size=None, file_name=None, progress_callback=None):
        path = Path(file)
        data = path.read_bytes()
        handle = types.InputFile(len(self.uploads) + 1, 1, file_name or path.name, "hash")
        self.upload_data[handle.id] = data
        self.uploads.append((handle, path, file_size))
        if progress_callback:
            progress_callback(len(data), len(data))
        return handle

    def _format(self, text, kw):
        if kw.get("parse_mode") == "html":
            return html.parse(text)
        return text, kw.get("formatting_entities") or []

    async def _before(self, kind):
        if self.send_errors:
            raise self.send_errors.popleft()
        if self.gate_kind == kind and not self.accept_before_gate:
            self.entered.set()
            await self.gate.wait()

    async def _after(self, kind):
        if self.gate_kind == kind and self.accept_before_gate:
            self.entered.set()
            await self.gate.wait()
        if self.accept_error:
            error, self.accept_error = self.accept_error, None
            raise error

    def _sent(self, text, entities, media=None, reply_to=None):
        self.next_id += 1
        m = message(self.next_id, text, media=media, entities=entities, out=True,
                    reply_to=types.MessageReplyHeader(reply_to_msg_id=reply_to) if reply_to else None)
        m.date = datetime.fromtimestamp(self.clock(), timezone.utc)
        self.target_messages.append(m)
        return m

    async def send_message(self, entity, text, **kw):
        self.calls.append(("text", text, kw))
        await self._before("text")
        text, entities = self._format(text, kw)
        sent = self._sent(text, entities, reply_to=kw.get("reply_to"))
        await self._after("text")
        return sent

    def _sent_media(self, media):
        if isinstance(media, types.InputMediaUploadedDocument):
            doc = types.Document(200, 42, b"new", datetime.now(timezone.utc), media.mime_type,
                                 len(self.upload_data[media.file.id]), 1, media.attributes)
            return types.MessageMediaDocument(document=doc)
        if isinstance(media, types.InputMediaUploadedPhoto):
            photo = types.Photo(200, 42, b"new", datetime.now(timezone.utc),
                                [types.PhotoSize("x", 20, 20, len(self.upload_data[media.file.id]))], 1)
            return types.MessageMediaPhoto(photo=photo)
        return media

    async def send_file(self, entity, file, **kw):
        self.calls.append(("album" if isinstance(file, list) else "file", file, kw))
        await self._before("file")
        if isinstance(file, list):
            if self.album_error:
                error, self.album_error = self.album_error, None
                raise error
            sent = [self._sent(text, entities, self._sent_media(media), kw.get("reply_to"))
                    for media, text, entities in zip(file, kw["caption"], kw["formatting_entities"])]
        else:
            text, entities = self._format(kw["caption"], kw)
            sent = self._sent(text, entities, self._sent_media(file), kw.get("reply_to"))
        await self._after("file")
        return sent


class CopyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.client = FakeClient(self.clock)
        self.managers = []
        self.notify = AsyncMock()
        self.video_upload = AsyncMock(side_effect=video_tools.VideoUnavailable("No decoder in this test."))
        self.limit = 2000 * MB
        self.get_client = AsyncMock(return_value=self.client)
        self.manager = self.make_manager()

    def make_manager(self, budget=60):
        manager = CopyJobs(self.tmp.name, get_client=self.get_client, resolve_chat=self.client.resolve,
            transfers=Transfers(0), upload_limit=AsyncMock(side_effect=lambda c: self.limit),
            video_upload=self.video_upload, notify=self.notify, sends_per_hour=budget,
            clock=self.clock, sleep=self.clock.sleep)
        self.managers.append(manager)
        return manager

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.shutdown()

    def source(self, *messages, protected=False):
        self.client.messages = {m.id: m for m in messages}
        self.client.source.noforwards = protected

    async def start(self, first=1, last=None, **kw):
        result = await self.manager.start("source", "target", first, last, pace_seconds=0, **kw)
        return self.manager.jobs[result["job_id"]]

    async def finish(self, job, manager=None):
        await (manager or self.manager).tasks[job["job_id"]]
        return job

    async def copy(self, *messages, protected=False, **kw):
        self.source(*messages, protected=protected)
        return await self.finish(await self.start(**kw))

    async def test_order_gaps_formatting_replies_and_fixed_end(self):
        entities = [types.MessageEntityBold(0, 5)]
        self.source(message(1, "hello **literal**", entities=entities), message(3, "reply",
                    reply_to=types.MessageReplyHeader(reply_to_msg_id=1)))
        job = await self.start()
        self.client.messages[7] = message(7, "arrived later")
        await self.finish(job)
        self.assertEqual(job["status"], "done")
        self.assertEqual([m.message for m in self.client.target_messages], ["hello **literal**", "reply"])
        self.assertEqual(self.client.calls[0][2]["formatting_entities"][0].to_dict(), entities[0].to_dict())
        self.assertIsNone(self.client.calls[0][2]["parse_mode"])
        self.assertEqual(self.client.calls[1][2]["reply_to"], 101)
        self.assertEqual(job["next_source_id"], 4)
        self.assertEqual(job["skipped"], [])
        self.assertIn([2, 2], job["deleted_ranges"])

    async def test_rewrite_mapped_fallback_unresolved_and_query(self):
        job = dict(from_chat_id=SOURCE, to_chat_id=TARGET, from_username="SourceName", to_username=None,
            from_message_id=10, next_source_id=20, end_source_id=30, map={"5": [50], "15": [150], "32": [320]},
            skipped=[{"source_id": 12}], deleted_ranges=[], links_left_unchanged=[])
        value = "https://t.me/c/1111/5?single https://telegram.me/SourceName/12?x=1&amp;y=2 "
        value += "https://t.me/c/1111/11 https://t.me/c/1111/29 https://t.me/other/5"
        result = rewrite_html(job, value)
        self.assertIn("https://t.me/c/2222/50?single", result)
        self.assertIn("https://t.me/c/2222/150?x=1&amp;y=2", result)
        self.assertEqual(result.count("https://t.me/c/2222/150"), 2)
        self.assertIn("https://t.me/c/1111/29", result)
        self.assertIn("https://t.me/other/5", result)
        rewrite_html(job, value)
        self.assertEqual(job["links_left_unchanged"], [29])
        job["to_username"] = "TargetName"
        self.assertEqual(rewrite_html(job, "https://t.me/c/1111/5"), "https://t.me/TargetName/50")

    async def test_link_seeds_and_utf16_entity_offsets(self):
        url = "https://t.me/c/1111/1"
        text = "😀 " + url + " bold"
        offset = len(("😀 " + url + " ").encode("utf-16-le")) // 2
        entities = [types.MessageEntityUrl(3, len(url)), types.MessageEntityBold(offset, 4)]
        job = await self.copy(message(2, text, entities=entities), first=2, link_map={"1": 987654})
        sent = self.client.target_messages[0]
        self.assertIn("https://t.me/c/2222/987654", sent.message)
        bold = next(e for e in sent.entities if isinstance(e, types.MessageEntityBold))
        self.assertEqual(sent.message.encode("utf-16-le")[bold.offset * 2:(bold.offset + bold.length) * 2].decode("utf-16-le"), "bold")
        self.assertEqual(self.client.calls[0][2]["parse_mode"], "html")
        self.assertEqual(job["map"]["1"], [987654])
        self.assertEqual(job["stats"]["copied_messages"], 1)

    async def test_existing_uncopied_links_do_not_fall_forward(self):
        job = await self.copy(message(1, "https://t.me/c/1111/2"), message(2, "later"),
                              message(3, "seed"), link_map={"3": 999})
        self.assertEqual(self.client.target_messages[0].message, "https://t.me/c/1111/2")
        self.assertEqual(job["links_left_unchanged"], [2])
        self.assertEqual(len(self.client.target_messages), 2)

    async def test_protected_filename_mime_attributes_thumbnail_and_cleanup(self):
        attrs = [types.DocumentAttributeAudio(3, voice=True), types.DocumentAttributeAnimated()]
        m = message(1, "caption", data=b"voice", name="../سلام.oga", attributes=attrs, mime="audio/ogg")
        m.document.thumbs = [types.PhotoSize("s", 20, 20, 9)]
        job = await self.copy(m, protected=True)
        self.assertEqual(job["status"], "done")
        media = self.client.calls[0][1]
        self.assertIsInstance(media, types.InputMediaUploadedDocument)
        self.assertEqual(media.file.name, "../سلام.oga")
        self.assertEqual([a.to_dict() for a in media.attributes], [a.to_dict() for a in m.document.attributes])
        self.assertEqual(media.mime_type, "audio/ogg")
        self.assertFalse(media.force_file)
        self.assertIsNotNone(media.thumb)
        self.assertEqual(self.client.uploads[0][1].name, "سلام.oga")
        self.assertTrue(all(not path.exists() for _, path, _ in self.client.uploads))
        self.assertFalse(self.manager.transfers.lock.locked())
        self.assertEqual(job["stats"]["bytes"], 5)

    async def test_reference_copy_and_restricted_fallback(self):
        m = message(1, data=b"pdf")
        job = await self.copy(m)
        self.assertEqual(job["status"], "done")
        self.assertIs(self.client.calls[0][1], m.media)
        self.assertEqual(self.client.downloads, [])
        self.client.send_errors.append(errors.ChatForwardsRestrictedError(None))
        self.client.messages = {2: message(2, data=b"pdf")}
        job = await self.finish(await self.start(first=2))
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.client.downloads, [(2, None)])
        self.assertIsInstance(self.client.calls[-1][1], types.InputMediaUploadedDocument)

    async def test_playable_document_promoted_but_other_types_preserved(self):
        self.video_upload.side_effect = None
        self.video_upload.return_value = dict(attributes=[types.DocumentAttributeVideo(2, 100, 80, supports_streaming=True)],
                                              mime_type="video/mp4", thumb=None)
        await self.copy(message(1, data=b"mp4", name="movie.mp4", mime="video/mp4"))
        media = self.client.calls[-1][1]
        self.assertTrue(any(isinstance(a, types.DocumentAttributeVideo) and a.supports_streaming for a in media.attributes))
        self.assertEqual(media.file.name, "movie.mp4")
        self.assertFalse(media.force_file)
        self.video_upload.side_effect = video_tools.VideoError("Cannot decode")
        self.source(message(2, data=b"avi", name="movie.avi", mime="video/x-msvideo"), protected=True)
        await self.finish(await self.start(first=2))
        self.assertTrue(self.client.calls[-1][1].force_file)
        self.assertEqual(self.client.calls[-1][1].mime_type, "video/x-msvideo")

    async def test_oversize_pauses_skip_resumes_without_holes(self):
        big = message(2, data=b"x", grouped_id=7)
        big.document.size = 3000 * MB
        self.source(message(1, data=b"a", grouped_id=7), big, message(4, "after"), protected=True)
        job = await self.finish(await self.start())
        self.assertEqual(job["status"], "paused")
        self.assertEqual(job["paused_at_source_id"], 2)
        self.assertIn("3000.0 MB", job["pause_reason"])
        self.assertIn("2000 MB", job["pause_reason"])
        self.assertEqual(self.client.calls, [])
        await self.manager.control(job["job_id"], "skip_current")
        await self.finish(job)
        self.assertEqual(job["status"], "done")
        self.assertEqual(list(job["map"]), ["1", "4"])
        self.assertEqual(job["skipped"], [dict(source_id=2, reason="skipped by the user")])

    async def test_service_unsupported_empty_and_web_preview_skipped(self):
        service = types.MessageService(1, types.PeerChannel(1111), datetime.now(timezone.utc), action=types.MessageActionPinMessage())
        job = await self.copy(service, message(2, media=types.MessageMediaUnsupported()), message(3),
                              message(4, media=types.MessageMediaWebPage(types.WebPageEmpty(3))))
        self.assertEqual(job["status"], "done")
        self.assertEqual([s["source_id"] for s in job["skipped"]], [1, 2, 3, 4])
        self.assertEqual(self.client.calls, [])

    async def test_album_with_captions_and_type_fallback(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                first = 10 if fallback else 1
                self.client.calls.clear()
                self.source(message(first, "first", data=b"a", grouped_id=9, entities=[types.MessageEntityBold(0, 5)]),
                            message(first + 1, "second", data=b"b", grouped_id=9), protected=True)
                if fallback:
                    self.client.album_error = errors.MediaGroupedInvalidError(None)
                job = await self.finish(await self.start(first=first))
                self.assertEqual(job["status"], "done")
                self.assertEqual([c[0] for c in self.client.calls], ["album", "file", "file"] if fallback else ["album"])
                self.assertEqual(len(job["map"]), 2)
                self.assertEqual(self.client.calls[0][2]["caption"], ["first", "second"])
                self.assertIsInstance(self.client.calls[0][2]["formatting_entities"][0][0], types.MessageEntityBold)
                self.assertTrue(all(not path.exists() for _, path, _ in self.client.uploads))
                self.assertEqual(sum(source_id in (first, first + 1) for source_id, _ in self.client.downloads), 2)

    async def test_album_across_page_boundary(self):
        messages = [message(i, str(i)) for i in range(1, 100)]
        messages += [message(i, data=b"a", grouped_id=10) for i in range(100, 103)]
        self.manager.sends_per_hour = 200
        job = await self.copy(*messages)
        albums = [c for c in self.client.calls if c[0] == "album"]
        self.assertEqual(len(albums), 1)
        self.assertEqual(len(albums[0][1]), 3)
        self.assertEqual(job["stats"]["copied_messages"], 102)

    async def test_flood_and_premium_wait_retry_then_pause(self):
        self.client.send_errors.extend([errors.FloodWaitError(None, capture=70), errors.FloodPremiumWaitError(None, capture=80)])
        job = await self.copy(message(1, "text"))
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.clock.waits, [75, 85])
        self.assertEqual(job["stats"]["flood_wait_seconds_total"], 160)
        self.assertEqual(len(self.client.target_messages), 1)
        self.client.send_errors.extend(errors.FloodWaitError(None, capture=90) for _ in range(6))
        self.source(message(2, "next"))
        job = await self.finish(await self.start(first=2))
        self.assertEqual(job["status"], "paused")
        self.assertIn("five retries", job["pause_reason"])

    async def test_transient_refetch_and_exhausted_backoff(self):
        self.client.send_errors.append(errors.FileReferenceExpiredError(None))
        job = await self.copy(message(1, data=b"pdf"))
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.client.reads.count(1), 2)
        self.assertEqual(self.clock.waits, [10])
        self.client.send_errors.extend(TimeoutError() for _ in range(5))
        self.source(message(2, "text"))
        job = await self.finish(await self.start(first=2))
        self.assertEqual(job["status"], "paused")
        self.assertEqual(self.clock.waits[-4:], [10, 30, 90, 300])
        self.assertTrue(job["last_error"])

    async def test_budget_waits_and_is_shared_across_jobs_and_restarts(self):
        self.manager.sends_per_hour = 1
        observations = []
        self.clock.on_sleep = lambda _: observations.extend((j["status"], j["stats"]["current"]["phase"])
                                                          for j in self.manager.jobs.values() if j["status"] == "waiting")
        job = await self.copy(message(1, "first"), message(2, "second"))
        self.assertEqual(job["status"], "done")
        self.assertIn(("waiting", "waiting_send_budget"), observations)
        self.assertGreaterEqual(self.clock.waits[0], 3600)
        self.manager = self.make_manager(budget=1)
        self.source(message(3, "third"))
        job = await self.finish(await self.start(first=3))
        self.assertEqual(job["status"], "done")
        self.assertGreaterEqual(self.clock.waits[-1], 3600)

    async def test_album_larger_than_budget_sends_items_in_order(self):
        self.manager.sends_per_hour = 1
        job = await self.copy(message(1, data=b"a", grouped_id=1), message(2, data=b"b", grouped_id=1))
        self.assertEqual(job["status"], "done")
        self.assertEqual([c[0] for c in self.client.calls], ["file", "file"])

    async def _crash(self, *, accepted, messages, kind="text"):
        self.source(*messages)
        self.client.gate_kind, self.client.gate = kind, asyncio.Event()
        self.client.accept_before_gate = accepted
        job = await self.start()
        await asyncio.wait_for(self.client.entered.wait(), 2)
        await self.manager.shutdown()
        saved = json.loads((Path(self.tmp.name) / "copy-jobs" / (job["job_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "running")
        self.assertIsNotNone(saved["pending"])
        self.client.gate_kind = None
        restarted = self.make_manager()
        restarted.startup()
        job = restarted.jobs[job["job_id"]]
        await self.finish(job, restarted)
        self.assertEqual(job["status"], "done")
        return job

    async def test_restart_pending_visible_does_not_duplicate(self):
        job = await self._crash(accepted=True, messages=[message(1, "already sent"), message(3, "next")])
        self.assertEqual([m.message for m in self.client.target_messages], ["already sent", "next"])
        self.assertEqual(job["map"], {"1": [101], "3": [102]})
        self.assertEqual(job["stats"]["copied_messages"], 2)
        self.assertIsNone(job["pending"])

    async def test_restart_pending_not_visible_resends(self):
        await self._crash(accepted=False, messages=[message(1, "not sent")])
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(len(self.client.target_messages), 1)

    async def test_restart_recovers_each_album_item(self):
        job = await self._crash(accepted=True, kind="file", messages=[message(1, data=b"a", grouped_id=7),
                                                                    message(2, data=b"bb", grouped_id=7)])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(job["map"]), 2)
        self.assertEqual(job["stats"]["copied_files"], 2)

    async def test_caption_split_and_partial_restart(self):
        text = "long caption " * 100
        job = await self._crash(accepted=False, messages=[message(1, text, data=b"pdf",
                                        entities=[types.MessageEntityBold(0, len(text))])])
        self.assertEqual(job["map"]["1"], [101, 102])
        self.assertEqual([c[0] for c in self.client.calls], ["file", "text", "text"])
        self.assertEqual(self.client.target_messages[0].message, "")
        self.assertEqual(self.client.target_messages[1].message, text)
        self.assertEqual(self.client.target_messages[1].entities[0].length, len(text))

    async def test_premium_caption_limit_and_non_bmp_characters(self):
        self.client.me.premium = True
        job = await self.copy(message(1, "x" * 1500, data=b"pdf"))
        self.assertEqual(len(job["map"]["1"]), 1)
        self.client.me.premium = False
        self.source(message(2, "😀" * 600, data=b"pdf"))
        job = await self.finish(await self.start(first=2))
        self.assertEqual(len(job["map"]["2"]), 2)

    async def test_uncertain_timeout_recovers_before_retry(self):
        self.client.accept_error = TimeoutError()
        job = await self.copy(message(1, "text"))
        self.assertEqual(job["status"], "done")
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(self.client.target_messages), 1)

    async def test_only_one_job_manual_pause_resume_cancel_and_seeds(self):
        self.source(message(1, "text"))
        self.client.gate_kind, self.client.gate = "text", asyncio.Event()
        job = await self.start()
        await asyncio.wait_for(self.client.entered.wait(), 2)
        with self.assertRaisesRegex(ValueError, "Another copy job"):
            await self.start()
        await self.manager.control(job["job_id"], "pause", {"4": 444})
        self.assertEqual(job["status"], "paused")
        self.assertEqual(job["map"]["4"], [444])
        self.client.gate_kind = None
        await self.manager.control(job["job_id"], "resume")
        await self.finish(job)
        self.assertEqual(job["status"], "done")
        with self.assertRaisesRegex(ValueError, "different target"):
            await self.manager.control(job["job_id"], "add_links", {"4": 445})
        await self.manager.control(job["job_id"], "add_links", {"5": 555})
        self.assertEqual(job["map"]["5"], [555])
        self.client.gate_kind, self.client.gate = "text", asyncio.Event()
        self.client.entered.clear()
        self.source(message(2, "next"))
        job = await self.start(first=2)
        await asyncio.wait_for(self.client.entered.wait(), 2)
        await self.manager.control(job["job_id"], "cancel")
        self.assertEqual(job["status"], "cancelled")
        self.assertIsNotNone(job["pending"])
        with self.assertRaisesRegex(ValueError, "cancelled"):
            await self.manager.control(job["job_id"], "resume")

    async def test_busy_transfer_waits_and_disk_reserve_pauses(self):
        await self.manager.transfers.lock.acquire()
        self.clock.on_sleep = lambda _: self.manager.transfers.lock.release()
        job = await self.copy(message(1, data=b"pdf"), protected=True)
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.clock.waits, [3])
        self.clock.on_sleep = None
        self.source(message(2, data=b"pdf"), protected=True)
        with patch("transfers.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
            job = await self.finish(await self.start(first=2))
        self.assertEqual(job["status"], "paused")
        self.assertIn("free disk space", job["pause_reason"])
        self.assertFalse(self.manager.transfers.lock.locked())

    async def test_permissions_and_input_validation(self):
        self.source(message(1, "text"))
        for first, last in ((0, 1), (2, 1), (True, 1)):
            with self.assertRaises(ValueError):
                await self.start(first=first, last=last)
        self.client.permission.is_creator = False
        self.client.permission.post_messages = False
        with self.assertRaisesRegex(ValueError, "permission"):
            await self.start()
        self.client.target.broadcast, self.client.target.megagroup = False, True
        self.client.permission.is_admin = False
        self.client.target.default_banned_rights = types.ChatBannedRights(None, send_messages=True)
        with self.assertRaisesRegex(ValueError, "permission"):
            await self.start()
        self.assertEqual(self.manager.jobs, {})

    async def test_status_is_bounded_and_notification_failure_does_not_fail_job(self):
        self.notify.side_effect = RuntimeError("private provider detail")
        job = await self.copy(*[message(i, str(i)) for i in range(1, 31)])
        status = self.manager.status()
        self.assertEqual(status["status"], "done")
        self.assertEqual(len(status["last_map_entries"]), 10)
        self.assertGreater(status["messages_per_hour"], 0)
        self.assertEqual(self.manager.status("all")["total_jobs"], 1)
        path = Path(self.tmp.name) / "copy-jobs" / (job["job_id"] + ".json")
        self.assertEqual(json.loads(path.read_text())["map"], job["map"])
        self.assertFalse(list(path.parent.glob("*.tmp")))
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    async def test_rate_ignores_time_before_a_resume(self):
        self.source(message(1, "one"), message(2, "two"))
        self.client.send_errors.append(RuntimeError("unexpected"))
        job = await self.finish(await self.start())
        self.assertEqual(job["status"], "failed")
        self.clock.now += 3000  # the job sits failed for 50 minutes
        await self.manager.control(job["job_id"], "resume")
        self.clock.now += 60
        await self.finish(job)
        self.clock.now = job["sends"][-1][0] + 60
        status = self.manager.status(job["job_id"])
        # Two sends in the ~2 minutes since resume, not in the hour that includes the failure.
        self.assertGreater(status["messages_per_hour"], 30)

    async def test_uploaded_media_accepted_by_real_telethon_conversion(self):
        await self.copy(message(1, data=b"pdf"), protected=True)
        media = self.client.calls[0][1]
        _, converted, _ = await TelegramClient._file_to_media(SimpleNamespace(), media)
        self.assertIs(converted, media)

    async def test_long_text_and_entities_split_without_breaking_emoji(self):
        text = "a" + "😀" * 2200
        job = await self.copy(message(1, text, entities=[types.MessageEntityBold(0, 4401)]))
        self.assertEqual(job["status"], "done")
        self.assertEqual("".join(m.message for m in self.client.target_messages), text)
        self.assertEqual(len(job["map"]["1"]), 2)
        for m in self.client.target_messages:
            length = len(m.message.encode("utf-16-le")) // 2
            self.assertLessEqual(length, 4096)
            self.assertEqual(m.entities[0].offset, 0)
            self.assertEqual(m.entities[0].length, length)

    async def test_photo_and_sticker_keep_their_type(self):
        photo = types.Photo(20, 42, b"photo", datetime.now(timezone.utc), [types.PhotoSize("x", 20, 20, 3)], 1)
        p = message(1, media=types.MessageMediaPhoto(photo=photo))
        p._data = b"jpg"
        sticker = types.DocumentAttributeSticker("hi", types.InputStickerSetEmpty())
        job = await self.copy(p, message(2, data=b"webp", name="sticker.webp", mime="image/webp", attributes=[sticker]),
                              protected=True)
        self.assertEqual(job["status"], "done")
        self.assertIsInstance(self.client.calls[0][1], types.InputMediaUploadedPhoto)
        self.assertIsInstance(self.client.calls[1][1], types.InputMediaUploadedDocument)
        self.assertFalse(self.client.calls[1][1].force_file)
        self.assertIsInstance(self.client.calls[1][1].attributes[1], types.DocumentAttributeSticker)

    async def test_reference_contact_and_refused_location(self):
        contact = types.MessageMediaContact("123", "Test", "Contact", "", 123)
        location = types.MessageMediaGeo(types.GeoPoint(1.0, 2.0, 0))
        job = await self.copy(message(1, media=contact))
        self.assertEqual(job["status"], "done")
        self.assertIs(self.client.calls[0][1], contact)
        self.source(message(2, media=location))
        self.client.send_errors.append(errors.MediaInvalidError(None))
        job = await self.finish(await self.start(first=2))
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["skipped"][0]["source_id"], 2)

    async def test_recovery_rejects_incoming_old_and_already_mapped_matches(self):
        self.source(message(1, "same"))
        self.client.gate_kind, self.client.gate = "text", asyncio.Event()
        job = await self.start(link_map={"9": 100})
        await asyncio.wait_for(self.client.entered.wait(), 2)
        await self.manager.shutdown()
        for target_id, outgoing, age in ((99, True, 0), (101, False, 0), (102, True, 120)):
            m = message(target_id, "same", out=outgoing)
            m.date = datetime.fromtimestamp(self.clock() - age, timezone.utc)
            self.client.target_messages.append(m)
        self.client.next_id = 102
        self.client.gate_kind = None
        restarted = self.make_manager()
        restarted.startup()
        job = restarted.jobs[job["job_id"]]
        await self.finish(job, restarted)
        self.assertEqual(job["map"]["1"], [103])
        self.assertEqual(len(self.client.calls), 2)

    async def test_no_send_when_pending_cannot_be_persisted(self):
        self.source(message(1, "text"))
        job = await self.start()
        with patch.object(self.manager, "_save", side_effect=OSError("private storage detail")):
            await self.finish(job)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.client.calls, [])
        self.assertNotIn("private storage detail", job["last_error"])

    async def test_simultaneous_starts_do_not_create_two_workers(self):
        self.source(message(1, "text"))
        self.client.gate_kind, self.client.gate = "text", asyncio.Event()
        results = await asyncio.gather(self.start(), self.start(), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual(len(self.manager.jobs), 1)

    async def test_resume_retains_flood_deadline(self):
        self.source(message(1, "text"))
        self.client.send_errors.append(errors.FloodWaitError(None, capture=70))
        sleeping = asyncio.Event()
        async def wait(seconds):
            sleeping.set()
            await asyncio.Event().wait()
        self.manager.sleep = wait
        job = await self.start()
        await asyncio.wait_for(sleeping.wait(), 2)
        deadline = job["wait_until"]
        await self.manager.control(job["job_id"], "pause")
        self.manager.sleep = self.clock.sleep
        await self.manager.control(job["job_id"], "resume")
        await self.finish(job)
        self.assertEqual(job["status"], "done")
        self.assertGreaterEqual(self.clock(), deadline)
        self.assertEqual(self.clock.waits, [75])

    async def test_real_telethon_album_keeps_each_items_attributes(self):
        from telethon.tl import functions
        await self.copy(message(1, "bold", data=b"a", grouped_id=1, entities=[types.MessageEntityBold(0, 4)]),
                        message(2, "plain", data=b"b", grouped_id=1), protected=True)
        owner = self
        class Transport:
            _file_to_media = TelegramClient._file_to_media
            _send_album = TelegramClient._send_album
            async def get_input_entity(self, value):
                return value
            async def __call__(self, request):
                if isinstance(request, functions.messages.UploadMediaRequest):
                    return owner.client._sent_media(request.media)
                owner.assertIsInstance(request, functions.messages.SendMultiMediaRequest)
                owner.assertEqual([p.message for p in request.multi_media], ["bold", "plain"])
                owner.assertIsInstance(request.multi_media[0].entities[0], types.MessageEntityBold)
                return request
            def _get_response_message(self, ids, response, entity):
                return response.multi_media
        call = self.client.calls[0]
        result = await TelegramClient.send_file(Transport(), types.InputPeerSelf(), call[1], **call[2])
        self.assertEqual(len(result), 2)

    async def test_album_with_different_replies_is_sent_individually(self):
        job = await self.copy(message(1, data=b"a", grouped_id=1), message(2, data=b"b", grouped_id=1,
                               reply_to=types.MessageReplyHeader(reply_to_msg_id=1)))
        self.assertEqual(job["status"], "done")
        self.assertEqual([c[0] for c in self.client.calls], ["file", "file"])
        self.assertEqual(self.client.calls[1][2]["reply_to"], job["map"]["1"][0])

    async def test_message_protection_and_oversized_thumbnail(self):
        m = message(1, data=b"pdf", noforwards=True)
        m.document.thumbs = [types.PhotoSize("x", 20, 20, 200_000)]
        m._thumb = b"x" * 200_000
        job = await self.copy(m)
        self.assertEqual(job["status"], "done")
        self.assertIsInstance(self.client.calls[0][1], types.InputMediaUploadedDocument)
        self.assertIsNone(self.client.calls[0][1].thumb)

    async def test_telegram_rpc_timeout_is_retried(self):
        self.client.send_errors.append(errors.TimeoutError(None))
        job = await self.copy(message(1, "text"))
        self.assertEqual(job["status"], "done")
        self.assertEqual(self.clock.waits, [10])


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_lifespan_starts_and_stops_copy_worker(self):
        os.environ.setdefault("MCP_SECRET", "c" * 48)
        os.environ.setdefault("DATA_DIR", tempfile.gettempdir())
        import server
        @asynccontextmanager
        async def export_lifespan():
            yield
        manager = SimpleNamespace(startup=Mock(), shutdown=AsyncMock())
        with patch.object(server, "_get_copy_jobs", return_value=manager), \
             patch.object(server._download_links, "lifespan", export_lifespan):
            app = server.create_app()
            async with app.router.lifespan_context(app):
                manager.startup.assert_called_once()
                manager.shutdown.assert_not_awaited()
            manager.shutdown.assert_awaited_once()

    async def test_tools_and_notification_budget(self):
        os.environ.setdefault("MCP_SECRET", "c" * 48)
        os.environ.setdefault("DATA_DIR", tempfile.gettempdir())
        import server
        tools = {t.name: t for t in await server.mcp.list_tools()}
        self.assertTrue(tools["copy_status"].annotations.readOnlyHint)
        self.assertFalse(tools["start_copy"].annotations.readOnlyHint)
        self.assertFalse(tools["control_copy"].annotations.readOnlyHint)
        self.assertIn("clear yes", tools["start_copy"].description)
        manager = SimpleNamespace(start=AsyncMock(return_value={"job_id": "test"}),
                                  control=AsyncMock(return_value={"status": "paused"}), status=lambda _: {"status": "done"})
        with patch.object(server, "_get_copy_jobs", return_value=manager):
            self.assertEqual(await server.start_copy("s", "t", 1), {"job_id": "test"})
            self.assertEqual(await server.copy_status(), {"status": "done"})
            self.assertEqual(await server.control_copy("test", "pause"), {"status": "paused"})
        client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=1)))
        before = list(server._sent_times)
        with patch.object(server, "CALLER", "@assistant"), patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock(return_value=object())):
            await server._copy_notice("Copy finished")
        self.assertEqual(list(server._sent_times), before)
        self.assertEqual(client.send_message.await_args.args[1], "/text Copy finished")


if __name__ == "__main__":
    unittest.main()
