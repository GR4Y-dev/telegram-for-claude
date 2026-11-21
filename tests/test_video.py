"""Video resource boundaries and Telegram/CLI integration; no provider calls.

Real decoder checks generate tiny media in a temporary directory when FFmpeg exists.
CI requires those checks, including inside the Debian connector image.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ["MCP_SECRET"] = "v" * 48
os.environ["DATA_DIR"] = tempfile.gettempdir()
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "telegram-connector"), str(ROOT / "telegram-caller")]
import video_tools as video
import server
from transfers import Transfers, MB
from PIL import Image


def jpeg():
    out = io.BytesIO()
    Image.new("RGB", (80, 40), "red").save(out, "JPEG")
    return out.getvalue()


def message(**changes):
    fields = dict(file=SimpleNamespace(size=3, name="clip.mp4", ext=".mp4", mime_type="video/mp4"),
                  media=object(), web_preview=None, message="caption", video=True,
                  voice=False, audio=False, video_note=False, gif=False, sticker=False,
                  photo=False, document=SimpleNamespace(size=3), out=True, id=1,
                  date=datetime.now(timezone.utc), reply_to=None)
    fields.update(changes)
    return SimpleNamespace(**fields)


class VideoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        env = patch.dict(os.environ, {"VIDEO_LOCK_DIR": str(self.folder / "lock")})
        env.start()
        self.addCleanup(env.stop)

    def test_decoder_failures_name_the_exit_code_or_signal(self):
        self.assertEqual(video._exit("ffmpeg", 1), "ffmpeg exit code 1")
        self.assertEqual(video._exit("ffmpeg", -25), "ffmpeg stopped by signal 25: output size limit")
        self.assertEqual(video._exit("ffmpeg", 137), "ffmpeg stopped by signal 9: killed, often memory")
        self.assertEqual(video._exit("ffmpeg", 3221225477), "ffmpeg exit code 3221225477")

    async def test_repackage_probe_accepts_a_camera_stream_without_duration(self):
        raw = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264"},
                           {"codec_type": "audio", "codec_name": "ac3"}],
               "format": {"format_name": "mpegts"}}
        with patch.object(video, "_run", AsyncMock(return_value=json.dumps(raw).encode())):
            with self.assertRaisesRegex(video.VideoError, "duration is unknown"):
                await video.probe("00015.MTS", max_duration=None)
            info = await video.probe("00015.MTS", max_duration=None, need_duration=False)
        self.assertEqual((info.duration, info.codec, info.audio_codec), (0.0, "h264", "ac3"))

    async def test_detection_includes_round_gif_and_video_documents(self):
        for m in (message(), message(video=False, video_note=True), message(video=False, gif=True),
                  message(video=False), message(video=False, file=SimpleNamespace(name="clip.mkv", mime_type="application/octet-stream"))):
            self.assertTrue(video.is_video(m))
        self.assertFalse(video.is_video(SimpleNamespace()))
        self.assertFalse(video.is_video(name="notes.txt", mime="text/plain"))
        self.assertFalse(video.is_video(name="source.ts", mime="application/typescript"))
        self.assertTrue(video.is_video(name="recording.ts", mime="video/mp2t"))

    async def test_metadata_dimensions_duration_and_ranges_are_checked(self):
        good = {"streams": [{"codec_type": "video", "width": 640, "height": 480, "codec_name": "h264"}],
                "format": {"duration": "60", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
        with patch.object(video, "_run", AsyncMock(return_value=json.dumps(good).encode())):
            info = await video.probe("file.mp4")
        self.assertTrue(info.streamable)
        for start, end in ((-1, 10), (10, 9), (0, 61), (float("nan"), 5), (0, float("inf"))):
            with self.assertRaises(video.VideoError):
                video.time_range(info, start, end)
        for duration in ("NaN", "inf", "0", "7201", "garbage"):
            good["format"]["duration"] = duration
            with patch.object(video, "_run", AsyncMock(return_value=json.dumps(good).encode())):
                with self.assertRaises(video.VideoError):
                    await video.probe("file.mp4")
        good["format"]["duration"] = "2"
        good["streams"][0]["width"] = 100000
        with patch.object(video, "_run", AsyncMock(return_value=json.dumps(good).encode())):
            with self.assertRaisesRegex(video.VideoError, "megapixel"):
                await video.probe("file.mp4")

    async def test_evenly_spaced_frames_audio_cap_and_fixed_decoder_arguments(self):
        info = video.Info(600, 640, 480, True, "h264", "mp4")
        async def run(program, args, **kwargs):
            Path(args[-1]).write_bytes(b"RIFF" + b"x" * 100)
            return b""
        with patch.object(video, "probe", AsyncMock(return_value=info)), \
             patch.object(video, "frame", AsyncMock(return_value=jpeg())) as frame, \
             patch.object(video, "_run", AsyncMock(side_effect=run)) as run:
            checks = []
            description, pictures, audio = await video.inspect("video.mp4", self.folder, checks.append, frames=100)
        self.assertEqual(len(pictures), 8)
        self.assertEqual([t for t, _ in pictures], [37.5 + 75 * n for n in range(8)])
        self.assertIn("0.0 to 300.0s only", description)
        self.assertEqual(checks, [video.MAX_AUDIO_BYTES, 0])
        args = run.await_args.args[1]
        self.assertEqual(args[args.index("-t") + 1], "300.000")
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")
        self.assertNotIn("concat", args[args.index("-format_whitelist") + 1])
        self.assertEqual(args[args.index("-threads") + 1], "1")
        self.assertTrue(audio.is_file())

    async def test_disk_reserve_stops_audio_before_decoder(self):
        def no_space(_):
            raise ValueError("free disk space")
        with patch.object(video, "probe", AsyncMock(return_value=video.Info(5, 80, 40, True, "h264", "mp4"))), \
             patch.object(video, "frame", AsyncMock(return_value=jpeg())), patch.object(video, "_run", AsyncMock()) as run:
            with self.assertRaisesRegex(ValueError, "free disk"):
                await video.inspect("file.mp4", self.folder, no_space)
            run.assert_not_awaited()

    async def test_busy_timeout_and_cancellation_release_job(self):
        with patch.object(video, "available", return_value=True):
            async with video.job():
                with self.assertRaisesRegex(video.VideoError, "busy"):
                    async with video.job():
                        self.fail("second video accepted")
            with patch.object(video, "JOB_SECONDS", .01):
                with self.assertRaisesRegex(video.VideoError, "time limit"):
                    async with video.job():
                        await asyncio.sleep(1)
            self.assertFalse(video._lock.locked())

    @unittest.skipUnless(os.name == "posix", "shared file lock is a Linux deployment boundary")
    def test_host_and_container_style_workers_share_one_video_slot(self):
        code = ("import video_tools as v\ntry:\n with v._machine_slot(): pass\n"
                "except v.VideoBusy:\n raise SystemExit(7)\n")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "telegram-connector")}
        with video._machine_slot():
            result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr.decode())
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    async def test_decoder_output_cap_and_cancellation_kill_and_reap(self):
        for cancel in (False, True):
            reader = asyncio.StreamReader()
            if not cancel:
                reader.feed_data(b"x" * 100)
            proc = SimpleNamespace(stdout=reader, returncode=None, kill=lambda: None, wait=AsyncMock(return_value=-1))
            def kill():
                proc.returncode = -1
                reader.feed_eof()
            proc.kill = kill
            with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
                task = asyncio.create_task(video._run("ffmpeg", ["-i", "safe"], limit=10))
                if cancel:
                    await asyncio.sleep(.01)
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else video.VideoError):
                    await task
                self.assertIsNotNone(proc.returncode)
                proc.wait.assert_awaited()
                self.assertNotIn("shell", spawn.await_args.kwargs)

    async def test_real_child_with_full_stdout_pipe_is_killed_without_hanging(self):
        # Exercise OS pipes even on machines where FFmpeg execution is unavailable.
        code = "import os\nwhile True: os.write(1, b'x' * 65536)\n"
        with patch.object(video, "_command", return_value=[sys.executable, "-u", "-c", code]):
            with self.assertRaisesRegex(video.VideoError, "output exceeded"):
                await asyncio.wait_for(video._run("ffmpeg", [], limit=100), 5)

    async def test_connector_video_reads_disk_frames_and_transcript_and_cleans(self):
        manager = Transfers(0)
        paths = []
        async def download(m, file):
            paths.append(file.path)
            file.write(b"abc")
        async def inspect(path, folder, check_disk, **kwargs):
            self.assertEqual(Path(path).read_bytes(), b"abc")
            self.assertEqual((kwargs["start"], kwargs["end"]), (1, 2))
            audio = Path(folder) / "audio.wav"
            audio.write_bytes(b"audio")
            return "Video: 3s, 80 x 40", [(1.5, jpeg())], audio
        client = SimpleNamespace(download_media=download)
        with patch.object(server, "_transfers", manager), \
             patch.object(video, "available", return_value=True), patch.object(video, "inspect", AsyncMock(side_effect=inspect)), \
             patch.object(server, "TRANSCRIBE_API_KEY", "synthetic"), \
             patch.object(server, "_speech_to_text", AsyncMock(return_value="hello")):
            result = await server._read_video(client, object(), message(), "clip.mp4", "header", "en", 1, 2)
        self.assertTrue(any(isinstance(x, server.Image) for x in result))
        self.assertIn("hello", result[-1])
        self.assertTrue(all(not p.parent.exists() for p in paths))
        self.assertFalse(manager.lock.locked())

    async def test_missing_decoder_preview_does_not_download_movie(self):
        client = SimpleNamespace(download_media=AsyncMock(return_value=jpeg()))
        with patch.object(video, "available", return_value=False):
            result = await server._read_video(client, object(), message(), "clip.mp4", "header", None, 0, None)
        self.assertIn("preview picture", result[0])
        self.assertEqual(client.download_media.await_args.kwargs["thumb"], -1)

    async def test_silent_round_video_does_not_start_a_second_download(self):
        client = SimpleNamespace(download_media=AsyncMock(side_effect=lambda m, file: file.write(b"abc")))
        with patch.object(server, "_transfers", Transfers(0)), \
             patch.object(video, "available", return_value=True), \
             patch.object(video, "inspect", AsyncMock(return_value=("No audio track.", [(1, jpeg())], None))), \
             patch.object(server, "TRANSCRIBE_API_KEY", "synthetic"), \
             patch.object(server, "_transcribe", AsyncMock()) as transcribe:
            result = await server._read_video(client, object(), message(video_note=True), "clip.mp4", "header", "en", 0, None)
        transcribe.assert_not_awaited()
        self.assertIn("No audio track.", result)
        self.assertEqual(client.download_media.await_count, 1)

    async def test_oversized_video_still_explains_forwarding_without_downloading(self):
        source = message()
        source.file.size = (server.MAX_ATTACHMENT_MB + 1) * MB
        client = SimpleNamespace(download_media=AsyncMock())
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock()), \
             patch.object(server, "_get_message", AsyncMock(return_value=source)):
            result = await server.read_attachment("me", 1)
        self.assertIn("forward_messages", result[0])
        self.assertIn("didn't process", result[0])
        client.download_media.assert_not_awaited()

    async def test_large_video_send_options_preserve_playback_and_document_override(self):
        paths, options = [], []
        @asynccontextmanager
        async def source(*_):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "download.bin"
                path.write_bytes(b"abc")
                paths.append(path)
                yield SimpleNamespace(path=path, size=100 * MB, mime="video/mp4")
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)),
            send_file=AsyncMock(return_value=message()))
        async def upload(path, folder, **kwargs):
            options.append(kwargs)
            return {"supports_streaming": True, "attributes": [server.DocumentAttributeVideo(5, 80, 40)]}
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock()), patch.object(server, "_media_label", return_value="video"), \
             patch.object(server, "_fetch_url", source), patch.object(server, "_video_upload", AsyncMock(side_effect=upload)) as prepare:
            await server.send_file("me", url="https://example.test/video")
            self.assertFalse(client.send_file.await_args.kwargs["force_document"])
            self.assertTrue(client.send_file.await_args.kwargs["supports_streaming"])
            prepare.reset_mock()
            await server.send_file("me", url="https://example.test/video", as_document=True)
            self.assertTrue(client.send_file.await_args.kwargs["force_document"])
            prepare.assert_not_awaited()
        self.assertTrue(all(not p.exists() for p in paths))
        server._sent_times.clear()

    async def test_upload_metadata_thumbnail_and_round_rules(self):
        with patch.object(video, "available", return_value=True), \
             patch.object(video, "probe", AsyncMock(return_value=video.Info(10, 80, 80, False, "h264", "mov,mp4"))), \
             patch.object(video, "frame", AsyncMock(return_value=jpeg())):
            options = await server._video_upload("video.mp4", self.folder, round_video=True)
        attr = options["attributes"][0]
        self.assertEqual((attr.w, attr.h, attr.duration), (80, 80, 10))
        self.assertTrue(attr.round_message and options["supports_streaming"])
        self.assertLess(Path(options["thumb"]).stat().st_size, 20000)
        with patch.object(video, "available", return_value=True), \
             patch.object(video, "probe", AsyncMock(return_value=video.Info(61, 80, 80, True, "h264", "mp4"))):
            with self.assertRaisesRegex(ValueError, "60 seconds"):
                await server._video_upload("video.mp4", self.folder, round_video=True)

    async def test_whatsapp_video_and_copied_round_video_keep_playable_type(self):
        path = self.folder / "local.mp4"
        path.write_bytes(b"abc")
        source = message(video=False, video_note=True)
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)),
            send_file=AsyncMock(return_value=message()), download_media=AsyncMock())
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock()), patch.object(server, "_media_label", return_value="video"), \
             patch.object(server, "_read_whatsapp_file", return_value=str(path)), \
             patch.object(server, "_video_upload", AsyncMock(return_value={"supports_streaming": True})), \
             patch.object(server, "_get_message", AsyncMock(return_value=source)):
            await server.send_file("me", whatsapp_file="/home/app/store/local.mp4")
            self.assertTrue(client.send_file.await_args.kwargs["supports_streaming"])
            self.assertFalse(client.send_file.await_args.kwargs["force_document"])
            await server.send_file("me", from_chat="source", from_message_id=1)
            self.assertIs(client.send_file.await_args.args[1], source.media)
            client.download_media.assert_not_awaited()
        server._sent_times.clear()

    async def test_copy_as_document_reuploads_and_cleans_instead_of_ignoring_option(self):
        source, paths = message(), []
        async def download(m, file):
            self.assertIs(m, source)
            file.write(b"abc")
        async def send(entity, file, **kwargs):
            path = Path(file)
            paths.append(path)
            self.assertTrue(kwargs["force_document"])
            self.assertEqual(path.read_bytes(), b"abc")
            return message()
        client = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(premium=False)),
                                  download_media=download, send_file=send)
        with patch.object(server, "tg", AsyncMock(return_value=client)), \
             patch.object(server, "resolve_chat", AsyncMock()), patch.object(server, "_media_label", return_value="file"), \
             patch.object(server, "_get_message", AsyncMock(return_value=source)):
            await server.send_file("me", from_chat="source", from_message_id=1, as_document=True)
        self.assertTrue(paths and all(not p.exists() for p in paths))
        server._sent_times.clear()


@unittest.skipUnless(video.available(), "FFmpeg/ffprobe unavailable locally; required in Linux CI")
class RealVideoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        env = patch.dict(os.environ, {"VIDEO_LOCK_DIR": str(self.folder / "lock")})
        env.start()
        self.addCleanup(env.stop)
        self.path = self.folder / "sample.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000", "-t", "1", "-threads", "1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", str(self.path)],
            check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    async def test_tiny_video_frame_pixels_audio_and_metadata(self):
        async with video.job():
            description, frames, audio = await video.inspect(self.path, self.folder, lambda _: None)
        self.assertIn("160 × 90", description)
        self.assertGreaterEqual(len(frames), 7)
        for _, data in frames:
            with Image.open(io.BytesIO(data)) as picture:
                self.assertLessEqual(max(picture.size), video.IMAGE_SIDE)
        self.assertTrue(audio.read_bytes().startswith(b"RIFF"))
        self.assertLess(audio.stat().st_size, video.MAX_AUDIO_BYTES)
        async with video.job():
            info = await video.probe(self.path)
        self.assertTrue(info.audio and info.streamable)

    async def test_gif_and_silent_video(self):
        path = self.folder / "sample.gif"
        Image.new("RGB", (16, 16), "red").save(path, save_all=True,
            append_images=[Image.new("RGB", (16, 16), "blue")], duration=500, loop=0)
        async with video.job():
            description, frames, audio = await video.inspect(path, self.folder, lambda _: None, frames=2)
        self.assertEqual(len(frames), 2)
        self.assertIsNone(audio)
        self.assertIn("No audio track", description)

    async def test_camera_mts_repackages_to_streamable_mp4_under_the_cap(self):
        # Like a camera .MTS: H.264 + AC-3 in 192-byte M2TS packets with a padded mux rate.
        source, out = self.folder / "00000.MTS", self.folder / "out.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4", "-threads", "1",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-b:v", "2M",
            "-c:a", "ac3", "-b:a", "256k", "-f", "mpegts", "-mpegts_m2ts_mode", "1", "-muxrate", "3M", str(source)],
            check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cap = source.stat().st_size
        size = await video.remux_mp4(source, out, cap)
        self.assertEqual(size, out.stat().st_size)
        self.assertLess(size, cap)
        async with video.job():
            info = await video.probe(out)
        self.assertTrue(info.streamable)
        self.assertEqual(info.audio_codec, "aac")
        with self.assertRaises(video.VideoError):
            await video.remux_mp4(source, out, size // 2)

    async def test_playlists_and_corrupt_files_are_rejected(self):
        for content in (b"#EXTM3U\nhttp://127.0.0.1/private\n", b"ffconcat version 1.0\nfile '/etc/passwd'\n", b"not a movie"):
            path = self.folder / "hostile.mp4"
            path.write_bytes(content)
            async with video.job():
                with self.assertRaises(video.VideoError):
                    await video.probe(path)


if __name__ == "__main__":
    if os.environ.get("REQUIRE_FFMPEG") == "1" and not video.available():
        raise SystemExit("FFmpeg is required for this CI test")
    unittest.main()
