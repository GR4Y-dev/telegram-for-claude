"""Guest setup helpers: finding the native CLI and reading the login token; no real login."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "telegram-caller"))
import guest_setup

TOKEN = "sk-ant-oat01-" + "Ab3_-" * 20


class GuestSetupTests(unittest.TestCase):
    def test_token_is_read_through_terminal_colors_and_redraws(self):
        raw = ("\x1b[2K\x1b[1GOpen this link...\r\n\x1b]8;;https://example\x07link\x1b]8;;\x07\r\n"
               "\x1b[32mYour token:\x1b[39m \x1b[1m" + TOKEN + "\x1b[22m\r\n")
        self.assertEqual(guest_setup.extract_token(raw), TOKEN)
        self.assertIsNone(guest_setup.extract_token("Login cancelled\r\n"))
        self.assertTrue(guest_setup.TOKEN.fullmatch(TOKEN))
        self.assertFalse(guest_setup.TOKEN.fullmatch("sk-ant-api03-not-a-login-token-at-all"))

    def test_finds_native_binary_of_reviewed_version_and_skips_wrappers(self):
        with tempfile.TemporaryDirectory() as tmp:
            versions = {}
            def make(name, elf, version):
                path = Path(tmp, name, "claude")
                path.parent.mkdir()
                path.write_bytes((b"\x7fELF" if elf else b"#!/bin/sh\n") + b"rest")
                versions[os.path.realpath(path)] = version
                return path
            make("2.1.290", True, "2.1.290")
            make("2.1.300", False, "2.1.286")      # a wrapper script, never chosen
            wanted = make("2.1.286", True, "2.1.286")
            found = guest_setup.find_native([str(Path(tmp, "*", "claude"))],
                                            version=lambda p: versions.get(p, ""))
            self.assertEqual(found, os.path.realpath(wanted))
            self.assertIsNone(guest_setup.find_native([str(Path(tmp, "none", "*"))],
                                                      version=lambda p: versions.get(p, "")))

    @unittest.skipUnless(os.name == "posix" and guest_setup.shutil.which("script"), "needs util-linux script")
    def test_token_is_captured_from_a_real_terminal_session_and_the_recording_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "claude")
            fake.write_text('#!/bin/sh\n[ "$1" = setup-token ] || exit 2\n'
                            f'printf "\\033[32mToken:\\033[0m {TOKEN}\\n"\n')
            fake.chmod(0o700)
            self.assertEqual(guest_setup.run_setup_token(str(fake), tmp), TOKEN)
            self.assertEqual(sorted(os.listdir(tmp)), ["claude"])

    def test_auth_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "guest-auth.json")
            guest_setup.write_auth(auth, TOKEN)
            with self.assertRaises(FileExistsError):
                guest_setup.write_auth(auth, TOKEN)
            if os.name == "posix":
                self.assertEqual(os.stat(auth).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
