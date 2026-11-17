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

The November 16 revision discards a failed stream before the next turn, rebuilding its
arguments from the last successful session. A failure before any protocol output gets
one per-turn fallback; the following turn tries a fresh stream. Two consecutive stream
startup failures select per-turn mode for the rest of that call. Partial/error results
are never automatically replayed. Complete assistant messages without text deltas can
still supply the answer. Guest calls also create a fresh isolated container after a
failed turn, preserving scope and quotas.

The reported November 4 `StreamUnavailable` lines alone cannot establish the underlying
provider/CLI cause: they mean stdout ended before a protocol event. New diagnostics
include stage, a fixed failure reason, process exit code when available, event count,
stderr byte count and elapsed time. No stderr contents, prompts, replies, command-line
arguments or session IDs are logged. Keep CLI 2.1.286 and the existing flags/wrapper.

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
| Connector image bytes | not supplied | Bookworm slim plus distro FFmpeg; size pending CI measurement; context excludes secrets/data |
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

Connector web and Telegram attachment downloads now use private disk files, with one
transfer at a time and a minimum 2048 MB free-space reserve. HTTP chunks are 256 KiB;
Telethon uploads read at most 512 KiB per chunk. Offline tests simulate a 2000 MB file
at both the HTTP and Telethon boundaries without writing multi-GB artifacts, and assert
Python allocation peaks below 6 MiB. This measures transfer buffering, not the whole
process RSS or native parser allocations. Document/image extraction retains its 20 MB
input ceiling. Video snapshots use a separate fixed budget: eight JPEGs of at most 1 MB
in the connector, four in owner chat, and a mono 16 kHz WAV of at most five minutes
(under 10 MB). Native decoder processes run one at a time across both services, at nice 10 with
one codec/filter thread, a 768 MiB address-space ceiling on Linux, 12-second CPU ceiling
per child, 15-second wall deadline per child and 60-second wall deadline per job. The
decoder probes at most 5 MB / five seconds and seeks on disk for each still. Complex
codecs, very long GOPs or malformed files may hit those limits and return a preview.
These are enforced budgets, not measured production RSS or latency claims.

FFmpeg comes from the image's pinned Debian Bookworm release, with security updates.
CI runs tiny generated movies both on its host and in the actual connector image and
reports the added distro layer size. No full-video transcoding/remux is performed.
After deployment, check a video with audio, a silent GIF, a round video, a specific
time range, a large playable upload and its "send as file" option on a phone. Check
owner video chat, guest refusal, CPU/RSS and cancellation cleanup while a call is active.

The owner's November 16 server figures (86 MB caller idle, 150 MB peak since restart,
25 GB free disk) are supplied context, not measurements of this revision. No voice-model,
call-library or other-application resource changes are made. After deploying, measure
connector RSS and disk free space during a file larger than 50 MB and a cancelled
transfer; check cleanup and a second transfer's busy response. Verify the Claude app's
request timeout, and test several call turns plus hang-up/recovery. Multi-GB throughput
and Premium uploads require a live check.

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
