"""Offline security regression tests. No real accounts or messages."""
import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["MCP_SECRET"] = "s" * 48

os.environ["DATA_DIR"] = tempfile.gettempdir()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-connector"))
import server as module
import httpx


class AttachmentLimits(unittest.TestCase):
    def test_large_image_rejected_before_decode(self):
        from PIL import Image as PILImage
        data = io.BytesIO()
        PILImage.new("1", (5001, 5000)).save(data, "PNG")
        with self.assertRaisesRegex(ValueError, "25 megapixels"):
            module._to_image(data.getvalue())

    def test_compressed_office_document_cannot_expand_without_bound(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("large.xml", b"0" * (33 * 1024 * 1024))
        self.assertLess(len(data.getvalue()), 100000)
        for reader in (module._read_docx, module._read_xlsx):
            with self.assertRaisesRegex(ValueError, "safe reading limit"):
                reader(data.getvalue())

    def test_sparse_spreadsheet_has_row_and_column_limits(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws["A1"] = "visible"
        ws.cell(10001, 1, "beyond-row-limit")
        ws.cell(1, 101, "beyond-column-limit")
        data = io.BytesIO()
        wb.save(data)
        wb.close()
        text = module._read_xlsx(data.getvalue())[0]
        self.assertIn("visible", text)
        self.assertNotIn("beyond-row-limit", text)
        self.assertNotIn("beyond-column-limit", text)

    def test_pdf_extraction_stops_after_fifty_pages(self):
        import pymupdf
        with pymupdf.open() as doc:
            for _ in range(60):
                doc.new_page().insert_text((20, 20), "A page of readable text. " * 3)
            data = doc.tobytes()
        text = "\n".join(x for x in module._read_pdf(data, 1) if isinstance(x, str))
        self.assertIn("start_page=51", text)
        self.assertNotIn("--- page 51 ---", text)

    def test_huge_scanned_page_renders_at_bounded_size(self):
        import pymupdf
        from PIL import Image as PILImage
        with pymupdf.open() as doc:
            doc.new_page(width=100000, height=100000)
            data = doc.tobytes()
        parts = module._read_pdf(data, 1)
        picture = next(p for p in parts if isinstance(p, module.Image))
        with PILImage.open(io.BytesIO(picture.data)) as im:
            self.assertLessEqual(max(im.size), module.IMAGE_SIDE)

class DownloadSafety(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def addresses(*ips):
        return [(2, 1, 6, "", (ip, 443)) for ip in ips]

    async def test_private_reserved_mixed_or_empty_dns_is_refused(self):
        for ips in [[], ["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"],
                    ["::1"], ["fc00::1"], ["224.0.0.1"], ["ff02::1"],
                    ["93.184.216.34", "192.168.1.1"]]:
            with self.assertRaises(ValueError):
                module._check_public("example.test", self.addresses(*ips))

    async def test_connection_uses_checked_ip_with_original_tls_name(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})
        real_client = httpx.AsyncClient
        options = []
        def client(**kwargs):
            options.append(kwargs)
            return real_client(transport=httpx.MockTransport(respond), **kwargs)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=self.addresses("93.184.216.34"))) as dns, \
             patch.object(module.httpx, "AsyncClient", client):
            async with module._fetch_url("https://example.test/note.txt") as item:
                data, name = item.path.read_bytes(), item.path.name
        self.assertEqual((data, name), (b"hello", "note.txt"))
        dns.assert_awaited_once()
        self.assertEqual(requests[0].url.host, "93.184.216.34")
        self.assertEqual(requests[0].headers["host"], "example.test")
        self.assertEqual(requests[0].extensions["sni_hostname"], "example.test")
        self.assertIs(options[0]["trust_env"], False)

    async def test_redirect_to_private_address_is_blocked(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=[
                self.addresses("93.184.216.34"), self.addresses("127.0.0.1")])), \
             patch.object(module.httpx, "AsyncClient", client):
            with self.assertRaises(ValueError):
                async with module._fetch_url("https://example.test/redirect"):
                    self.fail("private redirect accepted")
        self.assertEqual(len(requests), 1)

    async def test_credentials_and_non_web_schemes_are_rejected(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock()) as dns:
            for url in ["https://user:pass@example.test/file", "https://@example.test/file",
                        "file:///etc/passwd", "ftp://example.test"]:
                with self.assertRaises(ValueError):
                    async with module._fetch_url(url):
                        self.fail("invalid URL accepted")
            dns.assert_not_awaited()

    async def test_all_redirect_hops_and_original_tls_port_are_checked(self):
        requests = []
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"location": "https://second.test:8443/file.txt"})
            return httpx.Response(200, content=b"ok")
        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=self.addresses("93.184.216.34"))) as dns, \
             patch.object(module.httpx, "AsyncClient", client):
            async with module._fetch_url("https://first.test/file") as item:
                self.assertEqual(item.path.read_bytes(), b"ok")
        self.assertEqual(dns.await_count, 2)
        self.assertEqual(requests[1].headers["host"], "second.test:8443")
        self.assertEqual(requests[1].extensions["sni_hostname"], "second.test")
        self.assertEqual(requests[1].url.port, 8443)


class SendSafety(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_sends_reserve_capacity_and_release_on_cancellation(self):
        entered = asyncio.Event()
        async def waiting():
            with module._send_budget():
                entered.set()
                await asyncio.sleep(60)
        module._sent_times.clear()
        with patch.object(module, "MAX_SENDS_PER_HOUR", 1):
            task = asyncio.create_task(waiting())
            await entered.wait()
            try:
                with self.assertRaisesRegex(RuntimeError, "Safety limit"):
                    with module._send_budget():
                        self.fail("parallel send bypassed quota")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            with module._send_budget():
                pass
        self.assertEqual(module._pending_sends, 0)

    async def test_transcription_response_does_not_disclose_provider_body(self):
        response = SimpleNamespace(status_code=403, text="synthetic-private-provider-detail")
        @asynccontextmanager
        async def client(**kwargs):
            yield SimpleNamespace(post=AsyncMock(return_value=response))
        with patch.object(module.httpx, "AsyncClient", client):
            with self.assertRaises(RuntimeError) as raised:
                await module._speech_to_text(b"audio", "voice.ogg", None)
        self.assertIn("403", str(raised.exception))
        self.assertNotIn("synthetic-private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
