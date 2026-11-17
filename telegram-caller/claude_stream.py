"""One bounded stream-json conversation per call. No policy or credentials live here."""
import asyncio
import json
import logging
import os
import re
import time

from processes import stop

log = logging.getLogger("telegram-caller")
SENTENCE_END = re.compile(r"[.!?؟…]+[\"')\]»]*\s|\n")
MAX_RESPONSE = 64000
MAX_LINE = 1024 * 1024


class StreamFailure(RuntimeError):
    """A fixed diagnostic code, never provider output or user content."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__("Claude stream failed: " + reason)


class StreamUnavailable(StreamFailure):
    """Failed before any protocol output. Only this case permits a one-shot retry."""


async def read_turn(reader, sentences=None, stats=None):
    buf, full, fallback, sid, seen, first = "", "", "", None, False, True
    started = time.monotonic()

    async def emit(text):
        text = text.replace("[END]", "").strip()
        while text and sentences is not None:
            cut = text.rfind(" ", 0, 600) if len(text) > 600 else len(text)
            cut = cut if cut > 0 else 600
            await sentences.put(text[:cut])
            text = text[cut:].lstrip()

    while True:
        try:
            line = await reader.readline()
        except ValueError:
            raise StreamFailure("line_limit") from None
        if not line:
            if not seen:
                raise StreamUnavailable("eof_before_output")
            raise StreamFailure("eof_before_result")
        if len(line) > MAX_LINE:
            raise StreamFailure("line_limit")
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            raise StreamFailure("invalid_json") from None
        if not isinstance(event, dict):
            raise StreamFailure("invalid_event")
        seen = True
        if stats is not None:
            stats["events"] += 1
        value = event.get("session_id")
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,128}", value):
            sid = value
        if event.get("parent_tool_use_id"):
            continue
        if event.get("type") == "result":
            if event.get("is_error") or str(event.get("subtype", "")).startswith("error"):
                subtype = event.get("subtype")
                reason = subtype if subtype in ("error_max_turns", "error_during_execution",
                                                "error_max_budget_usd") else "error_result"
                raise StreamFailure(reason)
            result = event.get("result") or full or fallback
            if not isinstance(result, str) or len(result) > MAX_RESPONSE:
                raise StreamFailure("response_limit")
            if not result.strip():
                raise StreamFailure("empty_result")
            await emit(buf)
            if not full and result:
                await emit(result)
            return result.strip(), sid
        if event.get("type") == "assistant" and not full:
            # Some CLI responses contain complete messages without text deltas.
            message = event.get("message")
            blocks = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(blocks, list):
                raise StreamFailure("invalid_event")
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if not isinstance(text, str) or len(fallback) + len(text) > MAX_RESPONSE:
                        raise StreamFailure("response_limit")
                    fallback += text
        if event.get("type") != "stream_event":
            continue
        inner = event.get("event") or {}
        if not isinstance(inner, dict):
            raise StreamFailure("invalid_event")
        delta = inner.get("delta") or {}
        if not isinstance(delta, dict):
            raise StreamFailure("invalid_event")
        if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if not isinstance(text, str) or len(full) + len(text) > MAX_RESPONSE:
                raise StreamFailure("response_limit")
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
        self.stderr_bytes = 0
        self.lock = asyncio.Lock()

    async def drain_stderr(self, reader):
        while chunk := await reader.read(8192):
            self.stderr_bytes += len(chunk)

    async def ask(self, text, sentences=None):
        async with self.lock:
            started, stage = time.monotonic(), "spawn"
            stats = {"events": 0}
            stderr_start = self.stderr_bytes
            try:
                async with asyncio.timeout(self.timeout):
                    if self.proc is None:
                        self.proc = await asyncio.create_subprocess_exec(
                            *self.args, "--input-format", "stream-json", cwd=self.cwd, env=self.env,
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, limit=MAX_LINE,
                            start_new_session=os.name == "posix")
                        self.stderr = asyncio.create_task(self.drain_stderr(self.proc.stderr))
                    if self.proc.returncode is not None:
                        raise StreamUnavailable("exited_between_turns")
                    message = {"type": "user", "message": {"role": "user", "content": text},
                               "parent_tool_use_id": None, "session_id": ""}
                    stage = "write"
                    self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
                    await self.proc.stdin.drain()
                    stage = "read"
                    return await read_turn(self.proc.stdout, sentences, stats)
            except BaseException as exc:
                # Never log stderr, argv, session IDs, stdout or exception strings.
                reason = exc.reason if isinstance(exc, StreamFailure) else type(exc).__name__
                logger = log.debug if isinstance(exc, asyncio.CancelledError) else log.warning
                logger("claude stream failed stage=%s reason=%s exit_code=%s events=%d stderr_bytes=%d elapsed_ms=%.0f",
                       stage, reason, self.proc.returncode if self.proc else None, stats["events"],
                       self.stderr_bytes - stderr_start, (time.monotonic() - started) * 1000)
                await self.close()
                raise

    async def close(self):
        proc, stderr = self.proc, self.stderr
        self.proc = self.stderr = None
        try:
            try:
                if proc and proc.returncode is None and proc.stdin:
                    try:
                        proc.stdin.close()
                        await asyncio.wait_for(proc.wait(), 1)
                    except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
                        pass
            finally:
                await stop(proc)
        finally:
            if stderr:
                stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)
