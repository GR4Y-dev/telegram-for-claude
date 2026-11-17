"""Large fake streams, real tiny temp files, no accounts or multi-GB test files."""
import asyncio
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import sys
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ["MCP_SECRET"] = "t" * 48
os.environ["DATA_DIR"] = tempfile.gettempdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-connector"))
import httpx
import server
import transfers
from transfers import CHUNK_SIZE, MB, Transfers, download_url
from telethon import TelegramClient


class Stream(httpx.AsyncByteStream):
    def __init__(self, size, *, pause=None):
        self.size, self.pause, self.closed = size, pause, False

    async def __aiter__(self):
        remaining = self.size
        while remaining:
            size = min(remaining, CHUNK_SIZE)
            yield b"x" * size
            remaining -= size
            if self.pause:
                self.pause.set()
                await asyncio.sleep(60)

    async def aclose(self):
        self.closed = True


class Counter:
    """Discard bytes at the OS-write boundary; exercise all application streaming."""
    def __init__(self):
        self.total, self.largest = 0, 0

    def write(self, chunk):
        self.total += len(chunk)
        self.largest = max(self.largest, len(chunk))
        return len(chunk)

    def close(self):
        pass


class TransfersTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = Transfers(2 * MB)
        self.real_client = httpx.AsyncClient
        for obj, name, value in (
                (tempfile, "tempdir", self.tmp.name),
                (server, "_transfers", self.manager),
                (transfers.shutil, "disk_usage", lambda _: SimpleNamespace(free=10_000 * MB))):
            patcher = patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(server._sent_times.clear)

    @contextmanager
    def response(self, size, headers=None, *, pause=None):
        stream = Stream(size, pause=pause)
        def respond(request):
            return httpx.Response(200, stream=stream, headers=headers or {})
        def client(**kwargs):
            return self.real_client(transport=httpx.MockTransport(respond), **kwargs)
        loop = asyncio.get_running_loop()
        with patch.object(httpx, "AsyncClient", client), patch.object(loop, "getaddrinfo",
                AsyncMock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])):
            yield stream

    def assert_clean(self):
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.assertFalse(self.manager.lock.locked())

    async def test_fake_two_gb_stream_has_constant_memory_and_bounded_writes(self):
        peaks = []
        for size in (32 * MB, 2000 * MB):
            with self.response(size):
                async with self.manager.file(2000 * MB) as item:
                    item.stream.close()
                    counter = item.stream = Counter()
                    tracemalloc.start()
                    try:
                        await download_url("https://example.test/large.bin", item)
                        peaks.append(tracemalloc.get_traced_memory()[1])
                    finally:
                        tracemalloc.stop()
                    self.assertEqual(item.size, size)
                    self.assertEqual(counter.total, size)
                    self.assertLessEqual(counter.largest, CHUNK_SIZE)
                    self.assertEqual(item.path.stat().st_size, 0)  # no huge test artifact
            self.assert_clean()
        self.assertLess(max(peaks), 6 * MB)
        self.assertLess(peaks[1], peaks[0] + 2 * MB)

    async def test_telethon_upload_and_download_use_bounded_chunks_for_two_gb(self):
        size = 2000 * MB
        class Source:
            remaining, largest, closed = size, 0, False
            def read(self, count=-1):
                self.assert_bounded(count)
                count = min(count, self.remaining)
                self.remaining -= count
                self.largest = max(self.largest, count)
                return b"x" * count
            def assert_bounded(self, count):
                if not 0 < count <= 512 * 1024:
                    raise AssertionError("Telethon tried an unbounded read")
            def close(self):
                self.closed = True
        class Client:
            _log = defaultdict(lambda: logging.getLogger("transfer-test"))
            uploaded = 0
            async def __call__(self, request):
                self.uploaded += len(request.bytes)
                return True
            async def _iter_download(self, *args, **kwargs):
                async for chunk in Stream(size):
                    yield chunk
        client, source = Client(), Source()
        async with self.manager.file(size, size) as item:
            item.stream.close()
            counter = item.stream = Counter()
            tracemalloc.start()
            try:
                await TelegramClient._download_file(client, object(), file=item, file_size=size)
                self.assertEqual(counter.total, size)
                with patch("telethon.helpers.open", return_value=source, create=True), \
                     patch("telethon.helpers.os.path.getsize", return_value=size):
                    await TelegramClient.upload_file(client, str(item.path), file_name="large.bin")
                self.assertLess(tracemalloc.get_traced_memory()[1], 6 * MB)
            finally:
                tracemalloc.stop()
            self.assertEqual(client.uploaded, size)
            self.assertTrue(source.closed)
            self.assertLessEqual(counter.largest, CHUNK_SIZE)
            self.assertEqual(item.path.stat().st_size, 0)
        self.assert_clean()

    async def test_file_private_and_removed_on_success_or_upload_exception(self):
        for fail in (False, True):
            with self.response(3, {"content-length": "3", "content-disposition": 'attachment; filename="../../note.txt"'}):
                try:
                    async with server._fetch_url("https://example.test/file") as item:
                        self.assertEqual(item.path.read_bytes(), b"xxx")
                        self.assertEqual(item.path.name, "note.txt")
                        if os.name == "posix":
                            self.assertEqual(item.path.stat().st_mode & 0o777, 0o600)
                            self.assertEqual(item.folder.stat().st_mode & 0o777, 0o700)
                        if fail:
                            raise RuntimeError("upload failed")
                except RuntimeError:
                    self.assertTrue(fail)
            self.assert_clean()

    async def test_reserve_before_and_during_download(self):
        for free, declared in ((2 * MB - 1, None), (3 * MB, 2 * MB)):
            with patch.object(transfers.shutil, "disk_usage", lambda _: SimpleNamespace(free=free)):
                with self.assertRaisesRegex(ValueError, "free disk space"):
                    async with self.manager.file(4 * MB, declared):
                        self.fail("reserve ignored")
            self.assert_clean()
        async with self.manager.file(MB) as item:
            item.write(b"ok")
            with patch.object(transfers.shutil, "disk_usage", lambda _: SimpleNamespace(free=2 * MB)):
                with self.assertRaisesRegex(ValueError, "free disk space"):
                    item.write(b"!")
            self.assertEqual(item.size, 2)
        self.assert_clean()

    async def test_size_headers_missing_dishonest_and_over_limit(self):
        for size, headers in ((10, {"content-length": str(2 * MB)}),
                              (MB + 1, {}), (4, {"content-length": "3"}),
                              (2, {"content-length": "3"}), (1, {"content-length": "-1"}),
                              (1, {"content-encoding": "gzip"})):
            with self.response(size, headers), self.assertRaises(ValueError):
                async with server._fetch_url("https://example.test/file.bin", MB):
                    self.fail("invalid size accepted")
            self.assert_clean()
        with self.response(MB, {"content-length": str(MB)}):
            async with server._fetch_url("https://example.test/exact.bin", MB) as item:
                self.assertEqual(item.size, MB)
        self.assert_clean()

    async def test_cancel_during_download_cleans_file_and_releases_busy_slot(self):
        entered = asyncio.Event()
        async def download():
            async with server._fetch_url("https://example.test/slow.bin", MB):
                self.fail("not cancelled")
        with self.response(MB, pause=entered) as stream:
            job = asyncio.create_task(download())
            await entered.wait()
            with self.assertRaisesRegex(ValueError, "busy"):
                async with self.manager.file(MB):
                    self.fail("parallel transfer accepted")
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            self.assertTrue(stream.closed)
        self.assert_clean()

    async def test_overall_timeout_cleans_up_and_scales_to_size(self):
        self.assertGreater(transfers.timeout_seconds(2000 * MB), transfers.timeout_seconds(MB))
        with patch.object(transfers, "timeout_seconds", lambda _: .02), self.response(MB, pause=asyncio.Event()):
            with self.assertRaisesRegex(ValueError, "took too long"):
                async with server._fetch_url("https://example.test/slow.bin", MB):
                    self.fail("deadline ignored")
        self.assert_clean()

    async def test_telegram_unknown_or_incorrect_size_cannot_bypass_guard(self):
        for claimed in (0, 1):
            message = SimpleNamespace(file=SimpleNamespace(size=claimed))
            async def download(_, file):
                file.write(b"x" * CHUNK_SIZE)
                file.write(b"x" * CHUNK_SIZE)
            client = SimpleNamespace(download_media=download)
            with self.assertRaises(ValueError):
                async with server._download_attachment(client, message, "file.bin", CHUNK_SIZE):
                    self.fail("metadata bypassed write limit")
            self.assert_clean()

    async def test_premium_limit_checked_and_configuration_can_only_lower_it(self):
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)))
        self.assertEqual(await server._upload_limit(client, 4000), 2000 * MB)
        client.get_me.return_value.premium = True
        self.assertEqual(await server._upload_limit(client, 4000), 4000 * MB)
        self.assertEqual(await server._upload_limit(client, 500), 500 * MB)
        self.assertEqual(client.get_me.await_count, 3)

    async def test_send_file_uploads_path_and_cleans_on_upload_cancellation(self):
        entered = asyncio.Event()
        paths = []
        async def send(_, file, **kwargs):
            self.assertIsInstance(file, str)
            paths.append(Path(file))
            self.assertEqual(Path(file).read_bytes(), b"xxx")
            entered.set()
            await asyncio.sleep(60)
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)), send_file=send)
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock(return_value=object())), self.response(3):
            job = asyncio.create_task(server.send_file("me", url="https://example.test/file.txt"))
            await entered.wait()
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(server._pending_sends, 0)
        self.assert_clean()

    async def test_renamed_attachment_streams_and_reports_size_with_options(self):
        message = SimpleNamespace(file=SimpleNamespace(size=3), media=object(), web_preview=None)
        observed = []
        async def download(_, file):
            self.assertIsInstance(file, transfers.Transfer)
            file.write(b"abc")
        async def send(_, file, **kwargs):
            path = Path(file)
            observed.append(path)
            self.assertEqual(path.name, "renamed.txt")
            self.assertEqual(path.read_bytes(), b"abc")
            self.assertEqual(kwargs["caption"], "caption")
            self.assertEqual(kwargs["reply_to"], 9)
            self.assertTrue(kwargs["force_document"])
            return SimpleNamespace(id=17, date=datetime.now(timezone.utc))
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)),
                                 download_media=download, send_file=send)
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock(return_value=object())), \
             patch.object(server, "_get_message", AsyncMock(return_value=message)), \
             patch.object(server, "_media_label", return_value="file"):
            result = await server.send_file("me", from_chat="source", from_message_id=1,
                file_name="../../renamed.txt", caption="caption", reply_to_message_id=9, as_document=True)
        self.assertEqual(result["size_bytes"], 3)
        self.assertTrue(result["sent"])
        self.assertTrue(all(not path.exists() for path in observed))
        self.assert_clean()

    async def test_large_attachment_metadata_and_copy_never_download_or_extract(self):
        message = SimpleNamespace(file=SimpleNamespace(size=3000 * MB, name="archive.zip",
            ext=".zip", mime_type="application/zip"), media=object(), web_preview=None,
            message="caption", voice=False, audio=False, video_note=False, video=False,
            gif=False, sticker=False, out=True, id=1, date=datetime.now(timezone.utc), reply_to=None)
        client = SimpleNamespace(download_media=AsyncMock(),
            send_file=AsyncMock(return_value=SimpleNamespace(id=18, date=datetime.now(timezone.utc))))
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock(return_value=object())), \
             patch.object(server, "_get_message", AsyncMock(return_value=message)), \
             patch.object(server, "_media_label", return_value="file"), \
             patch.object(server, "read_file") as extract:
            preview = await server.read_attachment("source", 1, metadata_only=True)
            self.assertIn("3000.0 MB", preview[0])
            self.assertIn("caption", preview[0])
            refused = await server.read_attachment("source", 1)
            self.assertIn("bigger than 20 MB", refused[0])
            self.assertIn("forward_messages", refused[0])
            await server.send_file("me", from_chat="source", from_message_id=1)
            self.assertIs(client.send_file.await_args.args[1], message.media)
            extract.assert_not_called()
            client.download_media.assert_not_awaited()
        self.assert_clean()

    async def test_metadata_only_voice_does_not_transcribe(self):
        message = SimpleNamespace(file=SimpleNamespace(size=MB, name="voice.ogg", ext=".ogg",
            mime_type="audio/ogg"), media=object(), web_preview=None, message="", voice=True,
            photo=False, out=True, id=1, date=datetime.now(timezone.utc), reply_to=None)
        with patch.object(server, "tg", AsyncMock()), \
             patch.object(server, "resolve_chat", AsyncMock()), \
             patch.object(server, "_get_message", AsyncMock(return_value=message)), \
             patch.object(server, "_transcribe", AsyncMock()) as transcribe:
            self.assertIn("voice.ogg", (await server.read_attachment("me", 1, metadata_only=True))[0])
            transcribe.assert_not_awaited()

    async def test_upload_timeout_cleans_file_and_releases_send_budget(self):
        async def send(*args, **kwargs):
            await asyncio.sleep(60)
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)), send_file=send)
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock()), \
             patch.object(transfers, "timeout_seconds", return_value=.05), self.response(3):
            with self.assertRaisesRegex(ValueError, "took too long"):
                await server.send_file("me", url="https://example.test/slow-upload")
        self.assertEqual(server._pending_sends, 0)
        self.assert_clean()

    def test_oversized_photo_is_sent_as_document_before_telethon_decode(self):
        from PIL import Image
        path = Path(self.tmp.name) / "large.jpeg-extra"
        Image.new("1", (5001, 5000)).save(path, format="PNG")
        self.assertTrue(server._document_upload(path, False))
        path.unlink()

    async def test_failed_disk_write_cleans_up(self):
        with self.response(3), patch.object(transfers.Transfer, "write", side_effect=OSError("synthetic-private-detail")):
            with self.assertRaisesRegex(ValueError, "download failed") as raised:
                async with server._fetch_url("https://example.test/file"):
                    self.fail("disk failure ignored")
            self.assertNotIn("synthetic-private-detail", str(raised.exception))
        self.assert_clean()

    async def test_written_files_limit_utf8_bytes_not_only_character_count(self):
        client = SimpleNamespace(send_file=AsyncMock())
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock()), patch.object(server, "MAX_EXTRACT_MB", 1):
            with self.assertRaisesRegex(ValueError, "UTF-8 bytes"):
                await server.send_file("me", text_content="🙂" * (MB // 2), file_name="note.txt")
        client.send_file.assert_not_awaited()
        self.assertEqual(server._pending_sends, 0)

    def test_filenames_are_labels_not_paths(self):
        for name in ("../../etc/passwd", "C:\\private\\note.txt", "../..", "CON", "file\x00.txt", "🙂" * 300 + ".txt"):
            safe = transfers.safe_name(name)
            self.assertEqual(Path(safe).name, safe)
            self.assertNotIn("\\", safe)
            self.assertNotIn("\x00", safe)
            self.assertLessEqual(len(safe.encode()), 200)


if __name__ == "__main__":
    unittest.main()
