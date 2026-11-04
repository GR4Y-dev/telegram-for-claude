"""Selective image builds are based on source + image identity, never runtime secrets."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("build_images", Path(__file__).resolve().parents[1] / "build_images.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def test_rebuild_only_changed_or_missing_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            config = {"name":"sample", "services": {
                "bridge":{"build":{"context":"https://example.test/source.git#v1"}, "image":"bridge:v1"},
                "gateway":{"build":{"context":"."}},
                "tunnel":{"image":"tunnel:1"}}}
            def output(*args):
                return json.dumps(config) if args[0] == "docker" else str(state)
            def fingerprint(_, data):
                return data["context"]
            with patch.object(build, "run", side_effect=output), patch.object(build, "fingerprint", side_effect=fingerprint), patch.object(build, "image_id", return_value="image1"), patch.object(build.subprocess, "run") as execute:
                build.main(root)
                self.assertEqual(sum("build" in call.args[0] for call in execute.call_args_list), 2)
                execute.reset_mock()
                build.main(root)
                self.assertEqual(execute.call_args.args[0], ["docker","compose","up","-d","--no-build"])
                self.assertEqual(execute.call_count, 1)
                config["services"]["gateway"]["build"]["context"] = "changed-source"
                execute.reset_mock()
                build.main(root)
                self.assertEqual(execute.call_args_list[0].args[0], ["docker","compose","build","gateway"])
                execute.reset_mock()
                with patch.object(build, "image_id", side_effect=[None,"image2","image1"]):
                    build.main(root)
                self.assertEqual(execute.call_args_list[0].args[0], ["docker","compose","build","bridge"])
            self.assertNotIn("tunnel", state.read_text())

    def test_failed_start_does_not_mark_build_successful(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            config = {"name":"sample", "services": {}}
            with patch.object(build, "run", side_effect=[json.dumps(config), str(state)]), patch.object(build.subprocess, "run", side_effect=RuntimeError("failed")):
                with self.assertRaises(RuntimeError):
                    build.main(Path(tmp))
            self.assertFalse(state.exists())

    def test_mutable_remote_context_refused(self):
        for url in ("https://example.test/repo", "https://example.test/repo#main"):
            with self.assertRaises(ValueError):
                build.fingerprint(Path.cwd(), {"context":url})


if __name__ == "__main__":
    unittest.main()
