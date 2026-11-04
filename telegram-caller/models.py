"""Install only the chosen, checksum-pinned voice assets. Never execute .env."""
import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
ASSETS = {
    "kokoro-v1.0.onnx": (325532387, "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"),
    "kokoro-v1.0.int8.onnx": (92361271, "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb"),
    "voices-v1.0.bin": (28214398, "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
}


def settings(path):
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in ("TTS_EN", "KOKORO_DIR", "KOKORO_MODEL"):
                result[key.strip()] = value.strip().strip("\"'")
    return result


def valid(path, expected):
    size, digest = expected
    if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == digest


def install(folder, name):
    target, expected = folder / name, ASSETS[name]
    if valid(target, expected):
        return
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(RELEASE + name, timeout=60) as source:
            received = 0
            while chunk := source.read(1024 * 1024):
                received += len(chunk)
                if received > expected[0]:
                    raise RuntimeError("Voice download exceeds its pinned size")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if not valid(Path(temporary), expected):
            raise RuntimeError("Voice download failed its checksum; existing model kept")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--prune-unused", action="store_true",
                        help="after listening to the selected voice, remove the other known model")
    args = parser.parse_args()
    config = settings(args.env)
    if config.get("TTS_EN", "edge") != "kokoro":
        return
    model = config.get("KOKORO_MODEL", "fp32")
    if model not in ("fp32", "int8"):
        raise ValueError("KOKORO_MODEL must be fp32 or int8")
    folder = Path(config.get("KOKORO_DIR", args.env.resolve().parent / "models"))
    if not folder.is_absolute():
        folder = args.env.resolve().parent / folder
    folder.mkdir(parents=True, exist_ok=True)
    selected = "kokoro-v1.0.int8.onnx" if model == "int8" else "kokoro-v1.0.onnx"
    for name in (selected, "voices-v1.0.bin"):
        install(folder, name)
    if args.prune_unused:
        other = "kokoro-v1.0.onnx" if model == "int8" else "kokoro-v1.0.int8.onnx"
        # Never remove unknown/custom assets or the last known-good selected model.
        if valid(folder / other, ASSETS[other]):
            (folder / other).unlink()


if __name__ == "__main__":
    main()
