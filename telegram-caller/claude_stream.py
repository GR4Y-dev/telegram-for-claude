"""One bounded stream-json conversation per call. No policy or credentials live here."""
import asyncio
import json
import logging
import os
import re
import time

from processes import drain, stop

log = logging.getLogger("telegram-caller")
SENTENCE_END = re.compile(r"[.!?؟…]+[\"')\]»]*\s|\n")
MAX_RESPONSE = 64000
MAX_LINE = 1024 * 1024


class StreamUnavailable(RuntimeError):
    """Failed before any protocol output. Only this case permits a one-shot retry."""


async def read_turn(reader, sentences=None):
    buf, full, sid, seen, first = "", "", None, False, True
    started = time.monotonic()

    async def emit(text):
        text = text.replace("[END]", "").strip()
        while text and sentences is not None:
            cut = text.rfind(" ", 0, 600) if len(text) > 600 else len(text)
            cut = cut if cut > 0 else 600
            await sentences.put(text[:cut])
            text = text[cut:].lstrip()

    while True:
        line = await reader.readline()
        if not line:
            if not seen:
                raise StreamUnavailable("Claude stream unavailable")
            raise RuntimeError("Claude stream ended before its result")
        if len(line) > MAX_LINE:
            raise RuntimeError("Claude output exceeded the line limit")
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            raise RuntimeError("Invalid Claude protocol output") from None
        if not isinstance(event, dict):
            raise RuntimeError("Invalid Claude protocol output")
        seen = True
        value = event.get("session_id")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
            sid = value
        if event.get("parent_tool_use_id"):
            continue
        if event.get("type") == "result":
            if event.get("is_error"):
                raise RuntimeError("Claude failed; check login and usage")
            result = event.get("result") or full
            if not isinstance(result, str) or len(result) > MAX_RESPONSE:
                raise RuntimeError("Claude response exceeded the limit")
            await emit(buf)
            if not full and result:
                await emit(result)
            return result.strip(), sid
        if event.get("type") != "stream_event":
            continue
        inner = event.get("event") or {}
        delta = inner.get("delta") or {}
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if not isinstance(text, str) or len(full) + len(text) > MAX_RESPONSE:
                raise RuntimeError("Claude response exceeded the limit")
            if first:
                log.debug("call first_token_ms=%.0f", (time.monotonic() - started) * 1000)
                first = False
            full += text
            buf += text
            while match := SENTENCE_END.search(buf):
                piece, buf = buf[:match.end()], buf[match.end():]
                await emit(piece)
            # A very long sentence must not hold back all audio or grow an unbounded queue.
            while len(buf) > 600:
                split = buf.rfind(" ", 0, 600)
                split = split if split > 0 else 600
                await emit(buf[:split])
                buf = buf[split:]
        elif inner.get("type") == "content_block_stop":
            await emit(buf)
            buf = ""


class Conversation:
    def __init__(self, args, cwd, timeout, *, env=None):
        self.args, self.cwd, self.timeout, self.env = list(args), cwd, timeout, env
        self.proc = self.stderr = None
        self.lock = asyncio.Lock()

    async def ask(self, text, sentences=None):
        async with self.lock:
            try:
                async with asyncio.timeout(self.timeout):
                    if self.proc is None:
                        self.proc = await asyncio.create_subprocess_exec(
                            *self.args, "--input-format", "stream-json", cwd=self.cwd, env=self.env,
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, limit=MAX_LINE,
                            start_new_session=os.name == "posix")
                        self.stderr = asyncio.create_task(drain(self.proc.stderr))
                    if self.proc.returncode is not None:
                        raise StreamUnavailable("Claude process exited between turns")
                    message = {"type": "user", "message": {"role": "user", "content": text},
                               "parent_tool_use_id": None, "session_id": ""}
                    self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                    await self.proc.stdin.drain()
                    return await read_turn(self.proc.stdout, sentences)
            except BaseException:
                await self.close()
                raise

    async def close(self):
        proc, stderr = self.proc, self.stderr
        self.proc = self.stderr = None
        try:
            if proc and proc.returncode is None and proc.stdin:
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), 1)
                except asyncio.TimeoutError:
                    pass
            await stop(proc)
        finally:
            if stderr:
                stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)
