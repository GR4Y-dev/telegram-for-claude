"""Real Linux container boundary and cancellation tests; no Claude account or API.

CI builds the runtime image and runs this as the host administrator, matching setup.
RUN_GUEST_DOCKER_TESTS=1 is required so a developer's ordinary unit run stays offline.
"""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
from guest_runtime import GuestRuntime, GuestConversation, docker_command, clean_environment


@unittest.skipUnless(os.environ.get("RUN_GUEST_DOCKER_TESTS") == "1", "requires the CI Docker isolation step")
class IsolationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = GuestRuntime(self.root / "guests", "/bin/true", self.root / "unused-auth")
        self.folder = self.runtime.folder(7,22,1)

    async def test_owner_other_guest_socket_and_root_files_are_inaccessible(self):
        secret = self.root / "owner-private-canary"
        secret.write_text("synthetic private content")
        other = self.runtime.folder(7,23,1)
        (other / "private-canary").write_text("different guest")
        name = "guest-isolation-" + uuid.uuid4().hex
        script = (f'test ! -e "{secret}"; test ! -e "{other}/private-canary"; '
                  'test ! -e /var/run/docker.sock; test ! -e /root/.claude; '
                  'test "$(id -u)" = 65532; test ! -w /usr/local/bin/claude; '
                  'test -z "${TRANSCRIBE_API_KEY-}"; '
                  '! touch /usr/guest-write-probe 2>/dev/null; '
                  'grep -Eq "^NoNewPrivs:[[:space:]]+1" /proc/self/status; '
                  'echo own-workspace > /guest/work/probe; echo ISOLATED')
        result = await docker_command(self.runtime.base_args(self.folder,name,auth=False) + ["/bin/sh","-ceu",script])
        self.assertEqual(result,"ISOLATED")
        self.assertEqual((self.folder / "work/probe").read_text().strip(),"own-workspace")
        self.assertEqual(secret.read_text(),"synthetic private content")

    async def test_cancellation_removes_daemon_owned_container(self):
        name = "guest-cancel-" + uuid.uuid4().hex
        args = self.runtime.base_args(self.folder,name,auth=False) + ["/bin/sh","-c","sleep 60"]
        engine = GuestConversation(self.runtime,name,args,self.folder,clean_environment())
        job = asyncio.create_task(engine.ask("synthetic input"))
        try:
            for _ in range(40):
                running = await docker_command(self.runtime.docker_args() + ["ps","-q","--filter",f"name=^{name}$"])
                if running:
                    break
                await asyncio.sleep(.1)
            self.assertTrue(running)
            job.cancel()
            await asyncio.gather(job,return_exceptions=True)
            left = await docker_command(self.runtime.docker_args() + ["ps","-aq","--filter",f"name=^{name}$"])
            self.assertEqual(left,"")
        finally:
            await engine.close()


if __name__ == "__main__":
    unittest.main()
