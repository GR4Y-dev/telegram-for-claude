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
                    ["::1"], ["fc00::1"], ["93.184.216.34", "192.168.1.1"]]:
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
            data, name = await module._fetch_url("https://example.test/note.txt")
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
                await module._fetch_url("https://example.test/redirect")
        self.assertEqual(len(requests), 1)

    async def test_credentials_and_non_web_schemes_are_rejected(self):
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock()) as dns:
            for url in ["https://user:pass@example.test/file", "file:///etc/passwd", "ftp://example.test"]:
                with self.assertRaises(ValueError):
                    await module._fetch_url(url)
            dns.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

