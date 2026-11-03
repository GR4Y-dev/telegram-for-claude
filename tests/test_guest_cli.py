"""Run the actual pinned Linux CLI inside guest isolation against a fake local API.
No real credentials, AI requests or Telegram messages. Invoked explicitly by CI.
"""
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
from guest_runtime import GuestRuntime

CHECKSUM = "fe503f65c6289d59c23e5b21ae44f03583f997dd33a2cbfc75ab4f96fb8fc73f"
requests = []


class API(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1024 * 1024:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(length))
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":12}')
            return
        requests.append(body)
        result = "Synthetic reply " + str(len(requests))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            {"type":"message_start","message":{"id":"msg_test","type":"message","role":"assistant","content":[],"model":"claude-haiku-4-5-20251001","stop_reason":None,"stop_sequence":None,"usage":{"input_tokens":12,"output_tokens":1}}},
            {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}},
            {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":result}},
            {"type":"content_block_stop","index":0},
            {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":None},"usage":{"output_tokens":5}},
            {"type":"message_stop"},
        ]
        for event in events:
            self.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
        self.wfile.flush()


async def main():
    server = ThreadingHTTPServer(("0.0.0.0",0), API)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        binary = root / "claude"
        urllib.request.urlretrieve("https://downloads.claude.ai/claude-code-releases/2.1.286/linux-x64/claude",binary)
        with binary.open("rb") as stream:
            assert hashlib.file_digest(stream,"sha256").hexdigest() == CHECKSUM
        binary.chmod(0o755)
        auth = root / "auth.json"
        auth.write_text(json.dumps({"ANTHROPIC_API_KEY":"synthetic-test-only"}))
        auth.chmod(0o600)
        runtime = GuestRuntime(root/"guests",binary,auth)
        base = runtime.base_args
        def test_args(*args,**kwargs):
            result = base(*args,**kwargs)
            # Test fixture only: route API traffic to this runner, with no real key.
            return result[:-1] + ["--add-host","host.docker.internal:host-gateway", "--env",
                f"ANTHROPIC_BASE_URL=http://host.docker.internal:{server.server_port}"] + result[-1:]
        runtime.base_args = test_args
        engine = await runtime.conversation(7,22,1,None,"Synthetic guest test.","claude-haiku-4-5-20251001")
        try:
            first = await engine.ask("Synthetic first turn")
            process = engine.proc
            second = await engine.ask("Synthetic second turn")
            assert first[0] == "Synthetic reply 1" and second[0] == "Synthetic reply 2"
            assert first[1] == second[1] and engine.proc is process
            await engine.close()
            engine = await runtime.conversation(7,22,1,second[1],"Synthetic guest test.","claude-haiku-4-5-20251001")
            third = await engine.ask("Synthetic resumed turn")
            assert third[1] == second[1] and len(requests[-1]["messages"]) == 5
            await engine.close()
            engine = await runtime.conversation(7,23,1,second[1],"Different guest.","claude-haiku-4-5-20251001")
            other = await engine.ask("Independent guest")
            assert other[1] != second[1] and len(requests[-1]["messages"]) == 1
            tools = {tool["name"] for request in requests for tool in request.get("tools",[])}
            assert tools <= {"WebSearch","WebFetch","web_search"}, tools
            print("CLI 2.1.286: process reuse, restart/resume, guest separation, web-only tools, cleanup")
        finally:
            await engine.close()
            server.shutdown()


asyncio.run(main())
