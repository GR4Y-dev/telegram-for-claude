# Call and update performance

Kokoro now lives in a lazy, disposable subprocess. The assistant no longer imports or
loads ONNX/Kokoro at startup. Closing a call ends that process, releasing its native
allocations as well as Python buffers. ONNX uses at most two CPU threads, no CPU memory
arena, and no spinning worker threads. A bounded pipeline avoids growing sentence/audio
queues. A tiny WebRTC detector distinguishes speech from noise; sustained speech can
cancel playback, synthesis and model work without discarding the new utterance.

Calls use one stream-json Claude process with the original tool restrictions. Each turn
still gets current time and facts. Text chat keeps its existing separate conversation.
When supplied call facts change, the old process/session closes before the next turn.
Call slots are reserved before ringing/answering awaits, preventing overlapping calls.
Hang-up and time limits cancel outstanding work and reap child processes.

DEBUG timing events cover end-of-turn silence, transcription, first model token, synthesis
and first audio; they never contain text. Telethon routine messages are suppressed.
For timing diagnostics, set this application's logger to DEBUG without enabling provider
HTTP tracing. The default INFO logging stays quiet about message content and identities.

## Measurements

Server baselines were supplied in the request. No deployment or live provider calls were
performed. Local figures are Windows process-tree working sets, **not** Linux RSS+swap.
They are not evidence that the server's 100 MB idle target has been met.

| Metric | Before | After |
|---|---|---|
| Caller idle RSS + swap (server) | about 69 + 446 = 515 MB | not measured on server; no voice child while idle |
| Caller in-call peak (server) | about 559 MB RSS | not measured on server |
| Local voice child peak, fp32 / int8 | no comparable old-process sample | 492.6 / 285.2 MiB |
| Local voice child memory after close | resident model in old design | no child process remaining, both models |
| Model + voices bytes | 353,746,785 | int8: 120,575,669 after removing unused fp32 (233,171,116 fewer bytes) |
| Caller venv (server) | about 345 MB | same main dependencies plus tiny VAD wheel; server size not measured |
| Connector image bytes | not supplied | same slim base; Docker context excludes secrets/data; CI build |
| Claude startup overhead | reported 4–6 seconds each turn | one startup per call in stream mode; live latency unmeasured |
| Local fp32 cold / warm synthesis | no comparable old sample | 8.171 / 3.672 seconds |
| Local int8 cold / warm synthesis | no comparable old sample | 7.703 / 3.531 seconds |
| End-of-turn pause | 1.2 seconds | unchanged; configurable |

Local voice method: Python 3.12, Kokoro 0.6.1, ONNX CPU provider, affinity limited to two
logical CPUs, 2 ONNX threads, `bm_lewis` in `en-gb`. A fresh child synthesized a fixed
English greeting, then a short second sentence. Its process-tree RSS was sampled every
20 ms. Both children exited after close. Audio was valid 24 kHz PCM, about 4.74 seconds
for the greeting. This was a functional and resource test; subjective listening is
still required, so fp32 remains the safe default.

CLI compatibility method: checksum-verified official **2.1.286** Windows native binary,
clean test home/config, dummy API key, and a local HTTP server returning synthetic
Messages API streams. Two user inputs produced two results with the same process and
session ID; the second API request contained both turns (three conversation messages).
No real account, Telegram connector or paid API request was used. The deployed Linux
wrapper and subscription authentication remain unverified; `per-turn` is the fallback.

## Server verification after review

Before and after a few English/Persian calls, collect `/proc/<pid>/status` RSS, high-water
RSS and swap for the caller and children, plus `systemctl show telegram-caller` memory
current/peak, `du -sh telegram-caller/venv telegram-caller/models`, and Docker image bytes.
Measure cold and subsequent turns separately. Check a real hang-up during transcription,
barge-in on an actual phone, live web/weather, `/call`, scheduled calls, text chat, facts
and reminders. Repeat several calls to catch memory growth. Do not paste transcripts,
account identifiers, .env contents or connector URLs into public measurements.

## Updates and disk safety

Updates build changed/missing service images only. The first run records its baseline.
Both automatic and `--now` paths hold the existing cross-project lock, reject symlink
locks and stop for local tracked/untracked edits (ignored data stays untouched). The
GitHub test gate and failed-install code rollback remain. There is no global Docker
prune, so other applications' resources and useful cache survive. Check `docker system df`
and free space deliberately when needed. Base/security image refresh is an explicit
maintenance `docker compose build --pull` followed by `bash ../update.sh --now`.
Code rollback does not guarantee atomic rollback of already-restarted services.

Optional: an administrator may cap journald with a drop-in containing `[Journal]` and
`SystemMaxUse=200M`. It changes system-wide log retention, so setup never applies it.
A shared tunnel is not introduced; keeping independent installs avoids an unnecessary
dependency between the projects.
