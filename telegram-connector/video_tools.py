"""Small, bounded video jobs shared by the connector and owner chat.

Only local, self-contained media is accepted. No shell, playlists, network protocols,
or arbitrary FFmpeg options are exposed. Linux children also get OS resource limits.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

MB = 1024 * 1024
MAX_FRAMES = 8
IMAGE_SIDE = 1568
MAX_FRAME_BYTES = MB
MAX_AUDIO_SECONDS = 300
MAX_AUDIO_BYTES = MAX_AUDIO_SECONDS * 16000 * 2 + 4096
MAX_DURATION = 2 * 60 * 60
JOB_SECONDS = 60
FORMATS = "mov,matroska,webm,avi,gif,mpeg,mpegts,ogg"
# .ts is also TypeScript: transport streams need a video MIME type or Telegram flag.
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".gif", ".mpeg", ".mpg", ".ogv"}
_lock = asyncio.Lock()


class VideoError(ValueError):
    """Only fixed, user-safe error messages leave the decoder boundary."""


class VideoUnavailable(VideoError):
    pass


class VideoBusy(VideoError):
    pass


@contextmanager
def _machine_slot():
    """Host caller and connector container share this lock via the Compose bind mount."""
    if os.name != "posix":
        yield
        return
    import fcntl
    fd = None
    try:
        try:
            folder = Path(os.environ.get("VIDEO_LOCK_DIR", "/run/lock/telegram-video"))
            if folder.is_symlink():
                raise VideoUnavailable("Video lock directory is unsafe; check its permissions.")
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(folder / "job.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise VideoUnavailable("Video lock is not a regular file.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise VideoBusy("Video processing is busy. Try again when the other video finishes.") from None
        except OSError:
            raise VideoUnavailable("Video processing lock is unavailable; check shared lock permissions.") from None
        yield
    finally:
        if fd is not None:
            os.close(fd)


def is_video(message=None, *, name="", mime=""):
    if message is not None:
        if any(getattr(message, flag, False) for flag in ("video", "video_note", "gif")):
            return True
        file = getattr(message, "file", None)
        name, mime = getattr(file, "name", "") or "", getattr(file, "mime_type", "") or ""
    return mime.startswith("video/") or mime == "image/gif" or Path(name).suffix.lower() in VIDEO_EXTS


def available():
    return (all(shutil.which(p) for p in ("ffmpeg", "ffprobe")) and
            (os.name != "posix" or all(shutil.which(p) for p in ("nice", "prlimit"))))


@asynccontextmanager
async def job():
    if not available():
        raise VideoUnavailable("Video tools are unavailable; showing the preview instead.")
    if _lock.locked():
        raise VideoBusy("Video processing is busy. Try again when the other video finishes.")
    async with _lock:
        try:
            with _machine_slot():
                async with asyncio.timeout(JOB_SECONDS):
                    yield
        except TimeoutError:
            raise VideoError("Video processing reached its time limit. Try a shorter time range or use the preview.") from None


def _command(program, args):
    command = [program, "-hide_banner", "-loglevel", "error", "-max_alloc", str(64 * MB), *args]
    if os.name == "posix":
        # exec wrappers, not a shell or preexec_fn (the parent may have threads).
        command = ["nice", "-n", "10", "prlimit", "--as=805306368", "--cpu=12",
                   f"--fsize={16 * MB}", "--nofile=64", "--", *command]
    return command


async def _run(program, args, *, limit=64 * 1024):
    proc = None
    try:
        async with asyncio.timeout(15):
            proc = await asyncio.create_subprocess_exec(
                *_command(program, args), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=64 * 1024,
                **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS}
                   if os.name == "nt" else {}))
            output = bytearray()
            while chunk := await proc.stdout.read(64 * 1024):
                if len(output) + len(chunk) > limit:
                    raise VideoError("Video decoder output exceeded the safe limit.")
                output.extend(chunk)
            if await proc.wait() != 0:
                raise VideoError("This video cannot be decoded within the safe limits.")
            return bytes(output)
    except TimeoutError:
        raise VideoError("Video decoding took too long. Try a shorter clip.") from None
    except OSError:
        raise VideoUnavailable("Video tools are unavailable; showing the preview instead.") from None
    finally:
        if proc is not None:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            # A full stdout pipe can keep asyncio's process wait pending even after
            # kill. Discard the remaining bounded pipe buffers before reaping it.
            async with asyncio.timeout(5):
                while await proc.stdout.read(64 * 1024):
                    pass
                await proc.wait()


def _input(path):
    # format_whitelist also excludes concat/HLS/DASH and image-sequence file reads.
    # MOV external data references/absolute paths default to disabled. Do not enable
    # them. Supplying MOV-only options to other demuxers would reject valid GIF/WebM.
    return ["-protocol_whitelist", "file", "-format_whitelist", FORMATS,
            "-probesize", str(5 * MB), "-analyzeduration", "5000000", "-threads", "1",
            "-i", str(Path(path).resolve())]


@dataclass(frozen=True)
class Info:
    duration: float
    width: int
    height: int
    audio: bool
    codec: str
    format: str

    def describe(self):
        return f"Video: {self.duration:.1f} seconds, {self.width} × {self.height} pixels."

    @property
    def streamable(self):
        return self.codec == "h264" and "mp4" in self.format.split(",")


async def probe(path, *, max_duration=MAX_DURATION):
    raw = await _run("ffprobe", [*_input(path), "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,width,height,duration",
        "-of", "json"])
    try:
        data = json.loads(raw)
        streams = data["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
        width, height = int(video["width"]), int(video["height"])
        if not math.isfinite(duration) or duration <= 0 or (max_duration and duration > max_duration):
            raise VideoError("Video duration is unknown or exceeds the 2-hour processing limit. Send a shorter clip or send it as a file.")
        if min(width, height) <= 0 or width * height > 25_000_000:
            raise VideoError("Video dimensions exceed the 25-megapixel decode limit.")
        return Info(duration, width, height, any(s.get("codec_type") == "audio" for s in streams),
                    video.get("codec_name", ""), data.get("format", {}).get("format_name", ""))
    except (KeyError, TypeError, StopIteration, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, VideoError):
            raise
        raise VideoError("This file has no readable video metadata.") from None


def time_range(info, start=0, end=None):
    start, end = float(start), info.duration if end is None else float(end)
    if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= info.duration:
        raise VideoError("Choose a time range inside the video, with end_seconds greater than start_seconds.")
    return start, end


async def frame(path, second, *, side=IMAGE_SIDE):
    # Seek on disk, decode a single frame; never load the movie into the parent.
    return await _run("ffmpeg", ["-nostdin", "-filter_threads", "1", "-filter_complex_threads", "1",
        "-ss", f"{second:.3f}", *_input(path), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-frames:v", "1", "-vf", f"scale=w='min({side},iw)':h='min({side},ih)':force_original_aspect_ratio=decrease",
        # FFmpeg 5/6 (Debian, Ubuntu) refuse limited-range YUV for MJPEG; use full-range.
        "-pix_fmt", "yuvj420p", "-threads", "1", "-c:v", "mjpeg", "-q:v", "6", "-f", "image2pipe", "pipe:1"],
        limit=MAX_FRAME_BYTES)


async def inspect(path, folder, check_disk, *, start=0, end=None, frames=MAX_FRAMES, audio=True):
    """Call inside job() and a private temporary-directory lifetime."""
    info = await probe(path)
    start, end = time_range(info, start, end)
    count = max(1, min(int(frames), MAX_FRAMES))
    pictures = []
    for i in range(count):
        # Midpoints avoid the often-empty final frame and sample very short GIFs too.
        second = start + (end - start) * (i + .5) / count
        picture = await frame(path, second)
        if not picture:
            # Input seeking drops a frame that began before the seek point (a GIF or
            # still frame shown for a while): retry at the start of this segment.
            second = start + (end - start) * i / count
            picture = await frame(path, second)
        if picture:
            pictures.append((second, picture))
    audio_path = None
    seconds = min(end - start, MAX_AUDIO_SECONDS)
    if audio and info.audio:
        check_disk(MAX_AUDIO_BYTES)
        output_folder = Path(tempfile.mkdtemp(prefix="decoded-", dir=folder))
        audio_path = output_folder / "video-audio.wav"
        await _run("ffmpeg", ["-nostdin", "-y", "-filter_threads", "1", "-ss", f"{start:.3f}",
            *_input(path), "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", f"{seconds:.3f}",
            "-ac", "1", "-ar", "16000", "-threads", "1", "-c:a", "pcm_s16le",
            "-fs", str(MAX_AUDIO_BYTES), "-f", "wav", str(audio_path.resolve())])
        check_disk(0)
        if not audio_path.exists() or not 44 < audio_path.stat().st_size <= MAX_AUDIO_BYTES:
            raise VideoError("The video's audio exceeded the safe limit.")
        os.chmod(audio_path, 0o600)
    description = (info.describe() + f" Sampled {len(pictures)} still frames from {start:.1f} to {end:.1f}s. "
                   "These are snapshots, so brief actions between them may be missed.")
    if audio_path:
        description += f" Audio transcript covers {start:.1f} to {start + seconds:.1f}s only."
    elif not info.audio:
        description += " No audio track."
    return description, pictures, audio_path
