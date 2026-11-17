# Security review

## November 17 follow-up: large files, video and call recovery

Review scope: connector tools/downloads/parsers, caller commands and process handling,
guest authorization/runtime, setup and updater paths, and their regression tests. No
server, account or live call was accessed. The earlier review and its limits remain below.

- Large transfers retain DNS validation of every address, checked-IP connections with
  the original Host/TLS name, and redirect revalidation. Empty embedded credentials,
  multicast addresses and unexpected HTTP content encoding are rejected too. Streaming
  size checks, disk reserves and deadlines cover missing or misleading size metadata.
- Temporary directories/files are private (0700/0600 on Linux), filenames are labels
  rather than paths, and success/error/cancellation removes downloaded bytes. Oversized
  image uploads bypass automatic photo decoding. Parser input and expansion limits stay
  separate from the new transfer limit. A hard kill cannot run cleanup; operational
  cleanup guidance is in the connector reference.
- The shared hourly send quota now reserves capacity before awaiting Telegram or a
  download. Concurrent sends could previously all pass the check and exceed the limit.
  Failed/cancelled operations release reservations; completed sends retain their charge.
- Provider error bodies and exception tracebacks could disclose private request details.
  Changed error paths emit fixed diagnostic codes, exception types and counters only.
  Regression tests use synthetic details to check these do not appear in logs/results.
- Failed guest call streams are discarded so another turn cannot restart a container
  under a retired cleanup identity. A cleanup failure continues to disable guest work.
  Owner/guest scope, forwarded-command restrictions, quota enforcement and Docker
  permissions remain in place. The update gate, shared lock and rollback remain unchanged.
- Direct Python dependencies are pinned, including previously floating httpx, numpy and
  tzdata. Claude Code stays at the reviewed 2.1.286 contract and py-tgcalls/ntgcalls remain
  3.0.0/3.0.6. The connector adds distro FFmpeg/util-linux from its explicit Bookworm
  image release; there is no new Python video dependency.
- Video decoders use fixed argument lists, no shell, a local-file protocol whitelist,
  and a demuxer whitelist excluding playlists and image sequences. MOV external data
  references are disabled. Bounded output reads, image dimensions, source duration,
  audio length, wall deadlines and Linux OS memory/CPU/file limits constrain hostile
  media. Missing tools or a rejected decode produces a preview/explanation. These
  restrictions reduce exposure; native codecs are still part of the trust boundary.
- Owner chat passes bounded video frames through CLI image content blocks. Video
  captions/audio never invoke deterministic commands and video turns deny self-directed
  action tools. Guests cannot download or process videos. Voice-note byte limits now
  apply during download for owners and guests, before data is read into memory.
- The connector Docker context now includes the transfer/video helpers explicitly; its
  deny-by-default context continues to exclude sessions, secrets and runtime data.
- Written-file limits count UTF-8 bytes as well as characters, so multibyte text cannot
  exceed the advertised input budget. TypeScript `.ts` files still use text extraction;
  transport-stream videos need a video MIME type or Telegram video flag. Silent round
  videos do not start a second download for transcription, and oversized videos retain
  the forwarding/re-sending instructions.

`test_transfers` covers simulated 2 GB HTTP and Telethon streams, bounded memory,
cancellation during download/upload, deadlines, private temporary files, disk reserve,
Premium caps, renamed attachments and metadata previews. `test_runtime` and `test_guests`
cover safe stream recovery, protocol variants, process cleanup and diagnostic privacy.
CI includes these tests plus the existing Linux updater and container-isolation checks.
`test_video` and `test_video_chat` check decoder bounds, fallback, cancellation, disk
cleanup, time ranges, playable upload attributes, document overrides and guest refusal.
Tiny generated MP4/GIF and hostile-playlist tests run on CI and in the connector image.
The pinned CLI integration test also checks image content blocks against a fake API.
The public HTTPS smoke test is enabled in CI with `RUN_PUBLIC_DOWNLOAD=1`; local connector
tests use a deterministic HTTP fixture. This revision does not claim a new vulnerability
audit result until the CI dependency audit completes.

## November 3 review

This is a source and dependency review of this change, based on main commit
`e6c1e6af56979ae1f1a6198e27622589db85b3be`. It is not a penetration test of the running
server or a guarantee against every vulnerability.

## Fixed in this release

- **Dependency vulnerabilities:** replace Pillow 11.3.0 with 13.0.0. The resolved Python
  connector/caller requirements passed pip-audit 2.10.1 on this date with no known
  vulnerabilities (63 packages on the review machine). CI repeats the audit on Linux.
  See [Pillow's release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html).
- **DNS rebinding during file downloads:** validate every resolved address and connect to
  a validated IP, keeping the original Host header and TLS server name. Revalidate each
  redirect, reject embedded credentials and disable environment proxies for this download.
- **Attachment resource limits:** reject images above 25 megapixels before decoding, cap
  PDF text extraction at 50 pages per request and bound scanned-page rendering dimensions.
  Office ZIP contents are limited to 64 MB total, 32 MB per entry and 10,000 entries.
  Spreadsheet reads are bounded to 20 sheets, 10,000 rows and 100 columns per sheet.
- **Untested automatic updates:** require a completed, successful `tests` check from
  GitHub Actions for the exact target commit. Missing, skipped, incomplete, failed or
  unverified results cannot trigger installation. The lock now lives in the repository's
  private Git directory instead of a predictable writable `/tmp` path.
- **Assistant tool access:** restrict built-in Claude tools to WebSearch and WebFetch,
  load only the explicitly supplied MCP config, ignore user/project/local settings sources
  and explicitly deny Telegram sending/editing/deleting tools. Calls and forwarded-message
  replies also deny call_me, text_me and update_server. Direct owner chat retains these
  three existing self-directed actions.
- **Privacy and saved state:** numeric owner scoping on every fact/reminder database query,
  parameterized SQL, bounded storage, private file creation and atomic session writes.
  Session IDs are validated before being used as filenames; invalid state starts fresh.
  No routine transcript/reply logging, raw provider failure text or HTTP request URL logging.
  Command responses and saved facts keep literal characters; only Claude's Markdown bold
  (`**`) is removed from chat replies so it doesn't show as stars.
- **Forwarded content:** forwarded text and voice cannot invoke the deterministic command
  parser. Saved facts are supplied as reference data. Forgetting a fact resets the active
  text session (new facts reach the next turn without a reset), and changes to a call's supplied facts reset that call's session next turn.

## Validation and reproducibility

Run these from the repository root after installing both requirements files and ffmpeg:

```bash
SKIP_CLAUDE=1 python tests/test_caller.py
python tests/test_connector.py
python -m unittest discover -s tests -p test_chat.py -v
python -m unittest discover -s tests -p 'test_assistant_*.py' -v
python -m unittest discover -s tests -p test_security.py -v
bash tests/test_update.sh
pip-audit -r telegram-connector/requirements.txt -r telegram-caller/requirements.txt
bandit -r telegram-caller telegram-connector
```

The regressions cover owner isolation, forwarded commands, persistence, daylight-saving
changes, retries, concurrent completion, Unicode delivery, missing/invalid session state,
private-address downloads, redirect checks and attachment limits. Existing tests exercise
a fake Telegram account, speech playback logic and a public HTTPS download. No real
Telegram messages are sent by these tests. GitHub's `tests` job also checks shell syntax
and builds the connector Docker image. CI has read-only repository permissions.

Bandit 1.9.4 reported no high-severity findings. Remaining findings are best-effort cleanup
handlers and binding to `0.0.0.0` inside the intended Docker network. The supplied Compose
file publishes no connector host port. Exposing that container port directly would change
this assessment.

## Remaining limits and deployment

- The secret connector URL is a bearer credential. Anyone with it has the connector's
  available tools. Keep it out of chats, screenshots and logs; rotate it if disclosed.
  Tool descriptions asking Claude to seek approval are not a server-side approval gate.
- Prompt instructions are not a complete defense against prompt injection. The limited
  tool set reduces impact, but a malicious page/message could still influence answers or
  allowed self-directed actions. Do not add broad shell or arbitrary MCP permissions.
- The current setup runs the caller under the installing service account, usually root.
  Disabling model shell/file tools reduces exposure but does not isolate native parsers or
  trusted local administrators. The server, Claude CLI, speech providers, container base
  images and account sessions remain part of the trust boundary.
- The scan covers Python requirements and application source. It does not scan the host,
  Cloudflare configuration, native system libraries, the CLI binary or container image CVEs.
  Some transitive packages and container tags are not locked. Future installs must rerun
  the dependency audit; zero known findings today does not prove the absence of unknown bugs.
- Saved facts/reminders and conversation history are plaintext server data. Deleting a fact
  or completing a task removes it from the active database, not from backups, Telegram,
  previous transcript files, provider retention or recoverable filesystem pages.
- Reminder delivery requires one running assistant instance with Telegram connectivity.
  A crash after sending but before saving can duplicate a reminder. On recovery, a missed
  repeat sends once rather than replaying the backlog.
- This review does not verify deployment. After merging and updating the server, check
  one short reminder, snooze/done, a restart with a saved fact, ordinary chat and one call.
  Use an up-to-date Claude Code CLI that supports the documented tool/settings flags.
  Back up the assistant data directory with the service stopped before deployment.

The running service keeps its current behavior until the change is merged and installed.
