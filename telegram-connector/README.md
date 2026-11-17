# Telegram connector: reference

Setting it up for the first time? Follow `../README.md` (the main guide). This page is for later.

Lets Claude use Telegram **as your own account**: read chats (also far back, by date),
open attachments (photos, PDFs, Word, Excel, text files), sample video frames and understand voice messages
(Persian too), look at profiles, send messages and files now or scheduled for later,
edit, forward and delete messages, and ring you through the call assistant.
Claude always shows you exactly what will happen and waits for your "yes" before it sends,
schedules, edits, forwards or deletes anything.

## Everyday commands (on the server, inside this folder)

| What | Command |
|---|---|
| See if it's running | `docker compose ps` |
| See recent activity / errors | `docker compose logs --tail 50 telegram` |
| Restart | `docker compose restart` |
| Turn it off / on again | `docker compose stop` / `docker compose start` |
| Log in to Telegram again | `docker compose run --rm --no-deps telegram python login.py` then `docker compose restart` |
| Update now | ask Claude "update the server", or on the server `bash ../update.sh` |

**To cut Claude off instantly:** in Telegram, go to **Settings → Devices** and end the session called
"Claude connector". You can also remove the connector in claude.ai settings.

**If the connector address ever leaks,** make a new one (the old one stops working at once), then
put the new address in claude.ai's connector settings:

```
sed -i "s/^MCP_SECRET=.*/MCP_SECRET=$(openssl rand -hex 24)/" .env && docker compose up -d && echo "https://$(grep '^DOMAIN=' .env | cut -d= -f2)/$(grep '^MCP_SECRET=' .env | cut -d= -f2)/mcp"
```

## Voice messages

- **With Telegram Premium:** works by itself. Telegram writes out the voice message.
- **Without Premium:** add an OpenAI API key (platform.openai.com → API keys; about $0.006 per
  minute of audio). Run `nano .env`, add a line `TRANSCRIBE_API_KEY=` followed by your key, save with
  Ctrl+O, Enter, Ctrl+X, then `docker compose up -d`. The default model handles Persian well.
  For Groq instead (has a free tier), also add
  `TRANSCRIBE_URL=https://api.groq.com/openai/v1/audio/transcriptions` and
  `TRANSCRIBE_MODEL=whisper-large-v3`.

## Send files downloaded by WhatsApp (optional)

If your WhatsApp connector runs on the same Docker server, Telegram can send files its
`download_media` tool already saved. The next update (`/update`, or the nightly one) turns this
on by itself when it finds the WhatsApp store volume and `.env` has no `COMPOSE_FILE` line yet.
To do it by hand, in `/opt/telegram-for-claude/telegram-connector`:

```bash
docker volume inspect whatsapp-connector_whatsapp-store >/dev/null
sed -i '/^COMPOSE_FILE=/d' .env
printf '\nCOMPOSE_FILE=docker-compose.yml:docker-compose.whatsapp.yml\n' >> .env
docker compose up -d --build
```

This assumes the standard Compose files. If you already use other overrides, keep them in
`COMPOSE_FILE` too. For a different volume name, add `WHATSAPP_STORE_VOLUME=your-volume-name`
to `.env` and inspect that name instead. The volume must already exist. Without this opt-in,
Telegram runs normally without WhatsApp. Setup and automatic updates keep using the override
because Compose reads `COMPOSE_FILE` from `.env`.

After WhatsApp `download_media`, pass its exact path as `send_file(whatsapp_file=...)`, for
example `/home/app/store/120363399247816401@g.us/message/inmplant treatment.zip`. Spaces and
non-ASCII names are preserved; do not quote or URL-encode the path inside the parameter.
Use exactly one source. Caption, schedule, reply and `as_document` work as usual.

The store is mounted **read-only** at `/whatsapp-store`. Only regular files within it can
be sent, up to `MAX_WHATSAPP_MB` (default 2000 MB, streamed from disk); paths and symlinks escaping it are refused.
WhatsApp runs as uid **10001**, so its files may be private. The Telegram image runs as root
by default and can read these files on a standard Docker named volume. If you override its
user or use rootless/user-remapped Docker, grant the Telegram container's mapped user read
permission on files and search (`x`) permission on every parent directory, or use a shared
group/ACL. New downloads need those permissions too; the read-only mount cannot change them.

To disconnect, remove the `COMPOSE_FILE` line (or just this override from it) and run
`docker compose up -d` again. This keeps the WhatsApp volume and downloads.

## Large files

Ask Claude "Send this link to my Saved Messages" or "Send the attachment from this chat
as `report.pdf`". Normal file sending still requires your approval. Re-sending with a new
filename uses the same private disk transfer as a web download. Without a new name,
Telegram copies the existing attachment directly; forwarding and saving to `me` also
avoid a server download and are not held to the extraction limit.

| Operation | Default limit |
|---|---|
| Download a link and send it | 2000 MB |
| Download a Telegram attachment to rename and re-send | 2000 MB |
| Send an existing WhatsApp download | 2000 MB |
| Extract an attachment into Claude's context | 20 MB; existing parser limits still apply |
| Read a video | 2000 MB downloaded; 8 frames, up to 5 minutes of audio per request, 2-hour source duration |
| Export an original attachment to the PC | 2000 MB per file; 4000 MB total exports, at most 5 links |
| Transcribe through the external speech service | 25 MB |

Limits use 1024 × 1024 bytes per MB. Set the relevant transfer setting to at most 4000
for a Premium account; each upload checks `get_me().premium`, and normal accounts stay
capped at 2000. Existing explicit `.env` limits are preserved: change an old `MAX_UPLOAD_MB=50`
or `MAX_ATTACHMENT_MB=20` to 2000 if present, then run `docker compose up -d` after deployment.
Ask Claude to check `whoami` for the active limits and Premium status.

Downloads go to a private temporary directory on the container's disk, in bounded chunks.
One disk download/upload runs at a time. Another request receives a clear busy response;
chat reads and Telegram-side copies remain available. At least 2048 MB of free space is
reserved before and during downloads. A known-size file requires its full size plus that
reserve before downloading. The overall deadline covers download and upload: 120 seconds
plus 4 seconds per MB (about 2 hours 15 minutes for 2000 MB), with a 60-second HTTP idle
timeout. Results include the number of source bytes sent or scheduled, without progress
messages. The Claude app or network may impose a shorter request timeout; verify a large
transfer from the app you use before relying on multi-GB requests.

Temporary files are removed on success, errors, timeout and cancellation. A forced process
kill or host crash cannot run cleanup; after stopping the connector, an administrator can
inspect and remove only its abandoned `/tmp/telegram-transfer-*` directories. Container
recreation removes its writable-layer temporary files. Keep `/tmp` on disk, not a RAM-backed
mount, for these transfers. The optional WhatsApp mount remains read-only.

Ask "Show just this attachment's name, type and size" to use `read_attachment(metadata_only=True)`.
It also shows the caption without downloading, decoding or transcribing. Files above the
20 MB extraction limit still show metadata and directions for forwarding or re-sending.
The 60,000-character limit, 50 PDF text pages / 5 scanned pages, 25-megapixel image limit
and Office archive limits stay unchanged. Oversized pictures are sent as full-quality
documents to avoid decoding them during upload.

## Download original files to your PC

Ask Claude Code on your PC: **"Get the original video from that Telegram message and
save it in this project so we can edit it."** It can use the new tool, run the returned
download command, and then edit the local file with the tools on your PC. This works
with Claude Code CLI 2.1.286; no CLI changes are needed. The connector must already be
connected to that Claude Code session. Apps without a shell show a link you can tap.

| Tool | Result |
|---|---|
| `get_download_link(chat, message_id)` | Private HTTPS link, safe filename, byte size, MIME type, UTC expiry and a ready-to-run curl command |
| `revoke_download_links()` | Immediately disables all links, invalidates pending exports and deletes their cached files |

Videos, round videos, GIFs, photos at the largest size Telegram holds, documents,
voice messages and audio are supported. No conversion or thumbnail extraction occurs.
For a photo Telegram has compressed, its largest stored version is what can be exported.
Filename characters that are unsafe in a shell or on Windows are replaced.

The returned command looks like this (use the real link from the tool):

```sh
curl -fL -o "movie.mp4" "https://telegram.example.com/files/<token>/movie.mp4"
```

In Windows PowerShell, use `curl.exe -fL -o "movie.mp4" "<link>"`, or
`Invoke-WebRequest -Uri "<link>" -OutFile "movie.mp4"`. To resume a partial file with
curl, add `-C -`. Each GET, including a Range request, retry or invalid range, uses one
of the five attempts. HEAD checks do not count. A link is valid for 30 minutes by default;
request a fresh one if it expires. Downloads already started may finish after expiry or
revocation; their files are deleted when the response ends (including cancellation).
Slow responses have a size-based deadline, so they cannot hold disk space indefinitely.

Exports live in `DATA_DIR/exports` (0700 folder, 0600 files on Linux), with metadata only
in memory. The same Transfers slot, attachment byte ceiling and disk reserve apply,
including during every write on the data volume. Total export storage is capped by
`EXPORT_MAX_MB`, including files still being served; at most five cached links are kept.
Oldest links are evicted first, and the result tells you how many were removed. Unknown-size
attachments reserve their maximum permitted size before downloading. If active responses
still hold too much space, a new export gets a busy response. Partial downloads are removed
on failure or cancellation. A sweep runs every 30 seconds; requests enforce expiry immediately.
Restarts invalidate all links and remove old exports and abandoned partial downloads.
Run one connector process/worker for each data volume.

**Anyone holding the link can download the file until expiry or revocation.** Keep links
private; they grant access to that file without the MCP secret. Replies use attachment,
no-store, nosniff, no-referrer and restrictive CSP headers. Uvicorn access logging is
disabled and filtered; do not enable proxy/debug request logging for these URLs.
Revocation cannot recall a file someone has already downloaded. No tools are added to
the caller or guests; guests remain unable to use the owner's connector.

After deployment, check that `.env` contains `DOMAIN=telegram.your-domain.com` (your existing
tunnel hostname, without `https://` or a path), then recreate the connector with
`docker compose up -d --build` so Compose passes DOMAIN and the new settings through.
Keep the tunnel's hostname route pointed at `telegram:8000`, including `/files/*`, and
make sure Cloudflare cache rules do not override `no-store`. Check any Cloudflare/other
proxy logging or analytics that might record full URLs. Refresh Claude's connector tools,
download a small attachment, test a resume, then revoke its link and check it returns 404.
The MCP address stays `https://<DOMAIN>/<MCP_SECRET>/mcp`.

## Reading and sending videos

Ask "Watch this video and summarize it" or "Show what happens from 30 to 60 seconds".
`read_attachment` recognizes ordinary videos, round videos, GIFs and video documents.
It returns duration/resolution, at most eight JPEG stills spaced across the video (or
`start_seconds`–`end_seconds`), and speech from the first five minutes of that range.
Stills are at most 1568 pixels on the long side, never more than 1 MB each. They can miss
brief events between snapshots. Each video may be at most two hours long; the returned
text says exactly which audio range was processed. Ask for another range to continue.
Audio uses the existing `TRANSCRIBE_API_KEY` service; silent videos need no transcription.
Premium transcription remains available for round videos without that key.

The download streams to private disk under `MAX_ATTACHMENT_MB`. Decoding runs one shared job
at a time across the connector and caller, one codec/filter thread, at low priority, with a 60-second overall
processing deadline and 15-second child deadlines. Linux children have a 768 MiB address
space ceiling, 12-second CPU ceiling and 16 MiB output-file ceiling. Only self-contained
media formats and local-file input are permitted; playlists, external MOV references and
network protocols are disabled. Invalid/too-expensive videos return an explanation and
their preview when available. Missing FFmpeg/ffprobe also falls back to a preview.
Compose mounts only `/run/lock/telegram-video` into the container so both services share
one video slot. If you customize service users, give both access to that lock directory
and its `job.lock` file; an unavailable lock disables decoding rather than removing the
limit. The caller's optional `VIDEO_LOCK_DIR` must point at the same host directory.

`send_file` detects H.264 MP4 videos from links, the mounted WhatsApp store, and renamed
or converted Telegram attachments. It supplies duration, dimensions, a small thumbnail
when decoding succeeds, and Telegram's streaming flag. `as_document=True` sends the
original as a file. `video_note=True` sends a square MP4 of at most 60 seconds as a round
video, without a caption. Telegram-side copies preserve existing playable videos, round
videos and GIFs. Sending a copy as a document requires a bounded download/re-upload.
Unsupported codecs/containers and raw GIF uploads are kept as files with an explanation;
there is no expensive whole-video transcoding or second multi-GB remux. Arbitrary server
paths remain inaccessible; existing local files use the confined WhatsApp store mount.

The connector image uses `python:3.12-slim-bookworm` and Debian Bookworm's FFmpeg packages
(including security updates), rather than a new Python video dependency. This adds system
libraries and image disk space; CI reports the media layer and image size. Those sizes
have not been measured in the restricted local environment.
The caller already installs FFmpeg during setup. No deployment is performed by this PR.

## Settings (`.env`)

| Setting | What it does |
|---|---|
| `TIMEZONE` | Your time zone, used for scheduled messages and dates |
| `MAX_SENDS_PER_HOUR` | Safety limit for sends, files, forwards and edits together (default 30) |
| `MAX_ATTACHMENT_MB` | Telegram disk-download limit (default 2000, maximum 4000); document/image extraction stays at or below 20 MB, video output is separately bounded |
| `MAX_UPLOAD_MB` | Biggest file Claude may send from a web link (default 2000; up to 4000 with Premium) |
| `MAX_WHATSAPP_MB` | Biggest WhatsApp-downloaded file Claude may send (default 2000; up to 4000 with Premium) |
| `DISK_RESERVE_MB` | Free space downloads must leave on the temporary-file filesystem (default/minimum 2048) |
| `DOMAIN` | Existing public tunnel hostname used for download links; hostname only, required for exports |
| `LINK_MINUTES` | Download link lifetime, default 30 minutes, clamped to 1–1440 |
| `EXPORT_MAX_MB` | Total private export storage under DATA_DIR, default 4000 MB, minimum 1; oldest links removed first |
| `COMPOSE_FILE` | Optional Compose files, e.g. `docker-compose.yml:docker-compose.whatsapp.yml` |
| `WHATSAPP_STORE_VOLUME` | Existing WhatsApp volume name when enabled (default `whatsapp-connector_whatsapp-store`) |

## Safety limits built in
- At most 30 sends, files, forwards and edits per hour together, so the account never looks like spam.
- Reading chats does **not** mark them as read. Only `mark_read` does, when you ask for it.
- Claude can only edit your own messages. Deleting is at most 50 messages at once.
- Links to private or local addresses are refused.
- Transfers use private temporary files with automatic cleanup and a disk reserve; logs
  are capped at 3 × 10 MB per container. See the crash-cleanup caveat above.
- No joining groups, changing your profile, or touching your contacts or settings.
