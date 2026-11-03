"""A small, lazy controller; the large speech model stays in a separate process."""
import asyncio
import json
import os
from pathlib import Path
import sys

from processes import drain, stop


class VoiceWorker:
    def __init__(self, folder, model="fp32", threads=2):
        if model not in ("fp32", "int8"):
            raise ValueError("KOKORO_MODEL must be fp32 or int8")
        self.folder, self.model, self.threads = Path(folder), model, threads
        self.proc = self.stderr = None
        self.lock = asyncio.Lock()

    async def create(self, text, voice, lang):
        async with self.lock:
            try:
                async with asyncio.timeout(60):
                    if self.proc is None:
                        model = "kokoro-v1.0.int8.onnx" if self.model == "int8" else "kokoro-v1.0.onnx"
                        self.proc = await asyncio.create_subprocess_exec(
                            sys.executable, "-u", str(Path(__file__).with_name("kokoro_worker.py")),
                            str(self.folder / model), str(self.folder / "voices-v1.0.bin"), str(self.threads),
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, limit=16384,
                            start_new_session=os.name == "posix")
                        self.stderr = asyncio.create_task(drain(self.proc.stderr))
                    request = json.dumps({"text": text, "voice": voice, "lang": lang}, ensure_ascii=False)
                    self.proc.stdin.write((request + "\n").encode())
                    await self.proc.stdin.drain()
                    header = json.loads(await self.proc.stdout.readline())
                    size, rate = header["size"], header["rate"]
                    if type(size) is not int or not 0 < size <= 24000 * 2 * 90 or rate != 24000:
                        raise RuntimeError("Invalid voice output")
                    return await self.proc.stdout.readexactly(size), rate
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
