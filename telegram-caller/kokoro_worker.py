"""Disposable English voice worker. Closing it releases ONNX's native memory too."""
import json
import sys
from pathlib import Path


def main():
    import numpy as np
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    model, voices, threads = sys.argv[1:]
    options = ort.SessionOptions()
    options.intra_op_num_threads = max(1, min(int(threads), 2))
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    # Libraries may print diagnostics; reserve stdout exclusively for the framed protocol.
    output = sys.stdout.buffer
    sys.stdout = sys.stderr
    session = ort.InferenceSession(str(Path(model)), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    voice = Kokoro.from_session(session, voices)
    while line := sys.stdin.buffer.readline(16385):
        if len(line) > 16384:
            raise ValueError("Voice input exceeded the limit")
        request = json.loads(line)
        text = request["text"]
        if not isinstance(text, str) or not 0 < len(text) <= 2000:
            raise ValueError("Invalid voice input")
        samples, rate = voice.create(text, voice=request["voice"], lang=request["lang"])
        if rate != 24000 or len(samples) > rate * 90:
            raise ValueError("Voice output exceeded the limit")
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        output.write((json.dumps({"size": len(pcm), "rate": rate}) + "\n").encode())
        output.write(pcm)
        output.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never echo the text, model metadata or filesystem paths into service logs.
        sys.exit(1)
