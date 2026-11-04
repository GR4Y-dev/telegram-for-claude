"""Bounded child-process cleanup. Never retain provider stderr or log its contents."""
import asyncio
import os
import signal


async def drain(reader):
    while await reader.read(8192):
        pass


async def stop(proc):
    if proc is None:
        return
    if os.name == "posix" or proc.returncode is None:
        try:
            if os.name == "posix" and getattr(proc, "pid", None):
                # All our subprocesses start a new session; wrappers cannot leave children behind.
                os.killpg(proc.pid, signal.SIGKILL)
            elif os.name == "nt" and getattr(proc, "pid", None):
                # A venv launcher on Windows owns another Python process.
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), 5)
                if killer.returncode and proc.returncode is None:
                    proc.kill()
            else:
                proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        raise RuntimeError("A child process did not stop in time") from None
