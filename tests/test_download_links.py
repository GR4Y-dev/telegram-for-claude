"""Real FastMCP/Starlette app, private tiny files, fake Telegram; no sockets/accounts."""
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

os.environ["MCP_SECRET"] = "d" * 48
os.environ["DATA_DIR"] = tempfile.gettempdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-connector"))
import httpx
import server
import transfers
import download_links as links
from transfers import MB, Transfers, TransferError


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = links.DownloadLinks(Path(self.tmp.name) / "exports", 10 * MB)
        self.manager = Transfers(0)
        self.body = b"0123456789"
        self.message = SimpleNamespace(media=object(), web_preview=None,
            file=SimpleNamespace(name="movie.mp4", size=10, ext=".mp4", mime_type="video/mp4"))
        async def download(message, file):
            self.assertIsInstance(file, transfers.Transfer)
            self.assertTrue(file.folder.is_relative_to(self.store.folder))
            file.write(self.body)
        self.client = SimpleNamespace(download_media=AsyncMock(side_effect=download))
        for obj, key, value in ((server, "_download_links", self.store),
                (server, "_transfers", self.manager), (server, "DOMAIN", "telegram.example.test"),
                (server, "tg", AsyncMock(return_value=self.client)),
                (server, "resolve_chat", AsyncMock(return_value=object())),
                (server, "_get_message", AsyncMock(return_value=self.message)),
                (server.mcp, "_session_manager", None)):
            p = patch.object(obj, key, value)
            p.start()
            self.addCleanup(p.stop)
        self.app = server.create_app()
        await self.start_app()
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://telegram.example.test")

    async def start_app(self):
        self.events, self.replies = asyncio.Queue(), asyncio.Queue()
        self.life_task = asyncio.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}},
            self.events.get, self.replies.put))
        await self.events.put({"type": "lifespan.startup"})
        self.assertEqual((await self.replies.get())["type"], "lifespan.startup.complete")

    async def stop_app(self):
        await self.events.put({"type": "lifespan.shutdown"})
        await self.life_task
        self.assertEqual((await self.replies.get())["type"], "lifespan.shutdown.complete")

    async def asyncTearDown(self):
        await self.http.aclose()
        await self.stop_app()

    async def create(self):
        return await server.get_download_link("me", 1)

    def assert_clean(self):
        self.assertEqual(list(self.store.folder.iterdir()), [])
        self.assertEqual(self.store.entries, [])
        self.assertFalse(self.manager.lock.locked())

    async def test_creation_full_download_metadata_headers_and_permissions(self):
        with patch.object(links.secrets, "token_urlsafe", wraps=links.secrets.token_urlsafe) as random:
            result = await self.create()
        random.assert_called_once_with(32)
        self.assertNotIn(server.SECRET, json.dumps(result))
        token = urlsplit(result["link"]).path.split("/")[2]
        self.assertEqual(len(token), 43)
        self.assertEqual(result["size_bytes"], len(self.body))
        self.assertEqual(result["mime_type"], "video/mp4")
        self.assertTrue(result["expires_at"].endswith("+00:00"))
        self.assertEqual(result["download_command"], f'curl -fL -o "movie.mp4" "{result["link"]}"')
        self.assertNotIn(token, json.dumps({k: v for k, v in result.items() if k not in ("link", "download_command")}))
        self.assertNotIn(token, repr(self.store.entries))
        entry = self.store.entries[0]
        if os.name == "posix":
            self.assertEqual(entry.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.store.folder.stat().st_mode & 0o777, 0o700)
        response = await self.http.get(result["link"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.body)
        self.assertEqual(response.headers["content-length"], "10")
        self.assertEqual(response.headers["content-type"], "video/mp4")
        for k, v in links.HEADERS.items():
            self.assertEqual(response.headers[k], v)
        self.assertEqual(response.headers["content-disposition"], "attachment; filename*=UTF-8''movie.mp4")

    async def test_ranges_suffix_open_ended_unsatisfiable_and_head(self):
        result = await self.create()
        for _ in range(8):
            r = await self.http.head(result["link"])
            self.assertEqual((r.status_code, r.content), (200, b""))
            self.assertEqual(r.headers["content-length"], "10")
        self.assertEqual(self.store.entries[0].remaining, 5)
        for header, content, span in (("bytes=2-5", b"2345", "2-5"),
                ("bytes=7-", b"789", "7-9"), ("bytes=-2", b"89", "8-9")):
            r = await self.http.get(result["link"], headers={"Range": header})
            self.assertEqual((r.status_code, r.content), (206, content))
            self.assertEqual(r.headers["content-range"], f"bytes {span}/10")
            self.assertEqual(int(r.headers["content-length"]), len(content))
        r = await self.http.get(result["link"], headers={"Range": "bytes=100-200"})
        self.assertEqual(r.status_code, 416)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn("sandbox", r.headers["content-security-policy"])

    async def test_five_downloads_then_indistinguishable_404_and_deleted(self):
        result = await self.create()
        for _ in range(5):
            self.assertEqual((await self.http.get(result["link"])).content, self.body)
        for url in (result["link"], "/files/unknown/movie.mp4"):
            r = await self.http.get(url)
            self.assertEqual((r.status_code, r.text), (404, "Not Found"))
        self.assert_clean()

    async def test_expiry_and_revoke(self):
        result = await self.create()
        self.store.entries[0].expires = 0
        self.assertEqual((await self.http.get(result["link"])).status_code, 404)
        self.assert_clean()
        results = [await self.create(), await self.create()]
        self.assertEqual((await server.revoke_download_links())["revoked"], 2)
        for r in results:
            self.assertEqual((await self.http.get(r["link"])).status_code, 404)
        self.assert_clean()

    async def test_filename_is_cosmetic_and_traversal_cannot_read_other_files(self):
        outside = Path(self.tmp.name) / "secret.txt"
        outside.write_text("PRIVATE")
        result = await self.create()
        base = result["link"].rsplit("/", 1)[0]
        r = await self.http.get(base + "/secret.txt")
        self.assertEqual(r.content, self.body)
        for tail in ("/%2e%2e%2fsecret.txt", "/../secret.txt", "/%2fetc%2fpasswd"):
            r = await self.http.get(base + tail)
            self.assertEqual(r.status_code, 404)
            self.assertNotIn(b"PRIVATE", r.content)
        for url in ("/files/", "/files", "/", "/files/%E2%98%83/file"):
            self.assertEqual((await self.http.get(url)).status_code, 404)
        for method in ("POST", "PUT", "DELETE", "OPTIONS"):
            self.assertEqual((await self.http.request(method, result["link"])).status_code, 405)

    async def test_shell_safe_filename_unicode_disposition_and_active_mime_fallback(self):
        self.message.file.name = '../$(evil)`cmd`%PATH%!"سلام.svg'
        self.message.file.mime_type = "image/svg+xml"
        result = await self.create()
        for char in '$`%!?"/\\':
            self.assertNotIn(char, result["file_name"])
        r = await self.http.get(result["link"])
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        self.assertIn("filename*=UTF-8''", r.headers["content-disposition"])
        self.assertIn("%D8", r.headers["content-disposition"])
        self.assertIn("sandbox", r.headers["content-security-policy"])
        self.message.file.name = server.SECRET + ".html"
        self.message.file.mime_type = "text/html\r\nInjected: true"
        self.assertNotIn(server.SECRET, json.dumps(await self.create()))

    async def test_missing_or_invalid_domain_does_not_download(self):
        for domain in ("", "https://example.test", "example.test/path", "x.test:8000",
                       "user@x.test", "a..test", server.SECRET + ".test"):
            with patch.object(server, "DOMAIN", domain), self.assertRaisesRegex(TransferError, "Set DOMAIN="):
                await self.create()
        self.client.download_media.assert_not_awaited()
        self.assertEqual(links.DownloadLinks(self.store.folder, MB, 9999).minutes, 1440)

    async def test_all_file_types_original_not_thumbnail_and_no_file(self):
        for name, mime in (("movie.mp4", "video/mp4"), (None, "image/jpeg"),
                ("round.mp4", "video/mp4"), ("animation.gif", "image/gif"),
                ("doc.pdf", "application/pdf"), ("voice.ogg", "audio/ogg"), ("song.mp3", "audio/mpeg")):
            self.message.file.name, self.message.file.mime_type = name, mime
            result = await self.create()
            self.assertEqual(result["size_bytes"], 10)
            self.assertEqual(set(self.client.download_media.await_args.kwargs), {"file"})
        self.message.file = None
        with self.assertRaisesRegex(TransferError, "no file"):
            await self.create()

    async def test_count_and_total_size_evict_oldest_with_notice(self):
        results = [await self.create() for _ in range(6)]
        self.assertEqual(len(self.store.entries), 5)
        self.assertEqual(results[-1]["removed_older_links"], 1)
        self.assertIn("Removed 1 oldest", results[-1]["storage_note"])
        self.assertEqual((await self.http.get(results[0]["link"])).status_code, 404)
        await server.revoke_download_links()
        self.store.max_bytes = 15
        first, second = await self.create(), await self.create()
        self.assertEqual(second["removed_older_links"], 1)
        self.assertEqual((await self.http.get(first["link"])).status_code, 404)
        self.assertEqual(sum(e.size for e in self.store.entries), 10)

    async def test_size_metadata_streaming_export_and_attachment_limits(self):
        for declared, actual, budget in ((20, b"x", 10), (0, b"x" * 11, 10),
                                         (2, b"xxx", 10), (20, b"x", 30)):
            self.message.file.size, self.body, self.store.max_bytes = declared, actual, budget
            with self.assertRaises(TransferError):
                await self.create()
            self.assert_clean()
        self.message.file.size = MB + 1
        with patch.object(server, "MAX_ATTACHMENT_MB", 1), self.assertRaises(TransferError):
            await self.create()
        self.assert_clean()

    async def test_disk_reserve_before_and_during_writes_on_export_filesystem(self):
        self.manager.reserve = 100
        for free in (109, 100):
            self.message.file.size = 10 if free == 109 else 0
            def usage(folder):
                self.assertTrue(Path(folder).is_relative_to(self.store.folder))
                return SimpleNamespace(free=free)
            with patch.object(transfers.shutil, "disk_usage", side_effect=usage), \
                    self.assertRaisesRegex(TransferError, "free disk space"):
                await self.create()
            self.assert_clean()

    async def test_provider_failure_cancellation_timeout_and_busy_cleanup(self):
        entered = asyncio.Event()
        async def slow(message, file):
            file.write(b"123")
            entered.set()
            await asyncio.sleep(60)
        self.client.download_media.side_effect = slow
        task = asyncio.create_task(self.create())
        await entered.wait()
        with self.assertRaisesRegex(TransferError, "busy"):
            await self.create()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_clean()
        with patch.object(transfers, "timeout_seconds", return_value=.01), self.assertRaisesRegex(TransferError, "too long"):
            await self.create()
        self.assert_clean()
        async def fail(message, file):
            file.write(b"123")
            raise OSError("synthetic-private-provider-detail")
        self.client.download_media.side_effect = fail
        with self.assertRaises(TransferError) as error:
            await self.create()
        self.assertNotIn("synthetic-private", str(error.exception))
        self.assert_clean()

    async def test_revoke_during_export_prevents_later_publication(self):
        async def download(message, file):
            file.write(self.body)
            await server.revoke_download_links()
        self.client.download_media.side_effect = download
        with self.assertRaisesRegex(TransferError, "revoked while"):
            await self.create()
        self.assert_clean()

    async def test_concurrent_requests_cannot_exceed_five_and_last_response_cleans(self):
        result = await self.create()
        entry = self.store.entries[0]
        release, started = asyncio.Event(), asyncio.Queue()
        async def delayed_app(scope, receive, send):
            async def delayed_send(message):
                if message["type"] == "http.response.start" and message["status"] == 200:
                    await started.put(True)
                    await release.wait()
                await send(message)
            await self.app(scope, receive, delayed_send)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delayed_app)) as http:
            tasks = [asyncio.create_task(http.get(result["link"])) for _ in range(5)]
            try:
                for _ in tasks:
                    await asyncio.wait_for(started.get(), 5)
                self.assertEqual(entry.readers, 5)
                self.assertEqual((await self.http.get(result["link"])).status_code, 404)
                self.assertTrue(entry.path.exists())
                release.set()
                responses = await asyncio.gather(*tasks)
                self.assertTrue(all(r.content == self.body for r in responses))
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assert_clean()

    async def test_revoke_and_response_cancellation_release_disk_without_oversubscription(self):
        result = await self.create()
        entry = self.store.entries[0]
        started = asyncio.Event()
        async def delayed_app(scope, receive, send):
            async def delayed_send(message):
                if message["type"] == "http.response.body":
                    started.set()
                    await asyncio.sleep(60)
                await send(message)
            await self.app(scope, receive, delayed_send)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=delayed_app)) as http:
            task = asyncio.create_task(http.get(result["link"]))
            try:
                await asyncio.wait_for(started.wait(), 5)
                await server.revoke_download_links()
                self.assertEqual((await self.http.get(result["link"])).status_code, 404)
                self.assertTrue(entry.path.exists())
                self.store.max_bytes = 10
                with self.assertRaisesRegex(TransferError, "busy with active downloads"):
                    await self.create()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_clean()

    async def test_access_logs_never_include_token_even_at_info_level(self):
        result = await self.create()
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root, access = logging.getLogger(), logging.getLogger("uvicorn.access")
        old_level = access.level
        root.addHandler(handler)
        access.setLevel(logging.INFO)
        try:
            await self.http.get(result["link"])
            # This is the actual uvicorn access-record format, also at elevated log levels.
            access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", urlsplit(result["link"]).path, "1.1", 200)
        finally:
            root.removeHandler(handler)
            access.setLevel(old_level)
        token = urlsplit(result["link"]).path.split("/")[2]
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(result["link"], output.getvalue())

    async def test_real_mcp_endpoint_still_lists_and_calls_tools(self):
        headers = {"Accept": "application/json, text/event-stream"}
        endpoint = f"/{server.SECRET}/mcp"
        r = await self.http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 200)
        names = {t["name"] for t in r.json()["result"]["tools"]}
        self.assertTrue({"get_download_link", "revoke_download_links", "read_attachment", "send_file"} <= names)
        r = await self.http.post(endpoint, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_download_link", "arguments": {"chat": "me", "message_id": 1}}})
        self.assertEqual(r.status_code, 200)
        result = json.loads(r.json()["result"]["content"][0]["text"])
        self.assertEqual((await self.http.get(result["link"])).content, self.body)
        self.assertEqual((await self.http.post("/wrong/mcp", json={})).status_code, 404)

    async def test_background_and_startup_cleanup(self):
        # Startup only removes owned export names; unrelated DATA_DIR stays intact.
        await self.stop_app()
        stale = self.store.folder / ("a" * 32 + ".bin")
        stale.write_bytes(b"old")
        partial = self.store.folder / "telegram-transfer-abandoned"
        partial.mkdir()
        (partial / "payload").write_bytes(b"partial")
        other = Path(self.tmp.name) / "account.json"
        other.write_text("keep")
        self.store.sweep_seconds = .01
        server.mcp._session_manager = None  # Simulate a fresh process.
        self.app = server.create_app()
        await self.start_app()
        self.assertFalse(stale.exists() or partial.exists())
        self.assertEqual(other.read_text(), "keep")
        await self.create()
        self.store.entries[0].expires = 0
        for _ in range(100):
            if not self.store.entries:
                break
            await asyncio.sleep(.01)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
