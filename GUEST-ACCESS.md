# Letting other people use your assistant

Guest access is optional and off by default. Your existing owner setup keeps working.
After the one-time isolation setup below, send these commands in your private chat with
the assistant:

| Request | What happens |
|---|---|
| `/allow @username` | Approves that Telegram account and confirms its numeric ID to you |
| `/allow` as a reply to a forwarded message | Approves its original sender if Telegram exposes a real user ID |
| `/allow +123456789` | Resolves a phone number already known in the assistant account's contacts |
| `let Sara talk to you` | Works only for one matching contact; ambiguous names ask for an @username |
| `/deny @username` or `/deny 123456789` | Revokes access, cancels current work and clears that guest's local history |
| `stop answering Sara` | The same explicit removal path, with unambiguous identity |
| `/guests` | Lists approved accounts and their numeric IDs |
| `/callguest @username \| hello` | Calls that approved person only because you explicitly requested it |

These are commands handled by code. The AI cannot decide to approve someone. A forwarded
command alone grants nothing; reply to the forwarded message with your own `/allow`.
Hidden forward origins, groups, channels and bots cannot be approved. A phone/contact
lookup uses the **assistant account's** contacts; it does not read your private contact
list through your connector. Use an @username or a forward when a contact is unavailable.
Access is saved by Telegram user ID, so changing a display name does not change access.
Use the saved numeric ID to revoke someone who has renamed or deleted their account.

Guests get private text chat, voice notes, incoming calls, Persian/English replies, the
configured Jarvis/plain style, and web/weather lookups. Each guest has a separate
conversation, including resumption after restarts. `/new` clears only their conversation.
They cannot use your commands, saved facts, reminders, Telegram tools, conversation
history or server files. Unknown accounts and group messages get no reply. A guest's
first reply identifies the assistant as an AI and explains provider processing/storage.

Your messages and calls preempt guest work. There is at most one guest request and one
call at a time; busy requests are not queued. Guests may need to repeat an interrupted
request. You can still use owner chat while on your own call, as before.

## Limits

Default per-guest UTC-day limits are 30 messages, 10 voice-note minutes and 15 call minutes.
`/new`, refused commands, failed attempts, text/voice requests and model turns within calls
consume message allowance. Voice minutes are measured from decoded audio, not claimed
metadata. Calls reserve their remaining time before answering; normal hang-up refunds
unused time. A crash conservatively keeps the reservation. Calls stop at midnight UTC,
so one reservation cannot cross daily budgets. Setup and model errors do not grant extra
allowance. Re-approving someone, restarting, or `/new` never resets their daily counters.

`GUEST_MESSAGES_PER_DAY`, `GUEST_VOICE_MINUTES_PER_DAY` and `GUEST_CALL_MINUTES_PER_DAY`
adjust per-person limits (maximum 100, 30 and 60 respectively). There are also fixed
aggregate daily caps across all guests: 100 messages, 30 voice minutes and 60 call
minutes. At most ten guests can be approved. You and the guest get a limit notice at
most once per guest/day, including across re-approvals. Input/output lengths, processing
timeouts, model tool turns, storage and container resources are also bounded.

Guest transcript storage is separate, checked against 32 MiB before starting another model
container, with a 32 MiB per-file process limit. At the limit, use `/new` or have the owner
revoke/re-approve the guest. Deleting local history does not remove messages already
sent to Telegram, provider history or backups. Quota/approval records stay in the existing
`data/assistant.sqlite3`; old usage rows are cleaned after 32 days on startup.

## One-time setup on the Linux server

This feature intentionally refuses to run without isolation. It does not borrow the
owner's xWriter wrapper or entire login folder. An administrator should:

1. Locate the **existing native Linux Claude 2.1.286 binary**. Keep the owner's `CLAUDE_BIN`
   wrapper unchanged. Setup checks the ELF header and exact version; it never upgrades it.
2. Generate a separate long-lived token using that binary's `setup-token` command.
3. In `telegram-caller`, run `bash setup-guests.sh /absolute/path/to/native/claude`.
   It builds a small Debian runtime, asks for the token with hidden input, stores it in
   private `data/guest-auth.json`, adds guest settings to `.env`, and restarts the caller.
   The token is never placed in command arguments or the image.
4. Approve a test account, check text/voice/call behavior, then revoke it and verify silence.

For a dedicated API key instead, the private JSON file may contain exactly one field,
`ANTHROPIC_API_KEY`. The default uses `CLAUDE_CODE_OAUTH_TOKEN`. The file must be owned by
the caller's service user with mode 600. `GUEST_TOKEN_FILE` can point to that dedicated file.
Do not copy the owner's entire `.claude` directory. To turn the feature off, set
`GUEST_ENABLED=0` and restart; approvals remain saved but receive no service.

## Security boundary and verification

Each guest model process runs as a non-root user in a fresh, read-only Docker container
with no added capabilities, no privilege escalation, a private process namespace, a
512 MiB memory limit (no additional swap), one CPU, process/file limits, and no Docker
logs. Only the native CLI (read-only) and that guest's numeric-ID directory are mounted.
No owner home, account files, server data, Docker socket, other guest directory or owner
environment is mounted/passed. Temporary storage is limited. The controller force-removes
the actual container on cancellation; killing the Docker client alone would be insufficient.
Normal service stops await guest cleanup. If cleanup cannot be confirmed, new guest work
is refused until restart. Startup removes only orphan containers labelled for this
installation, including when guest access has since been switched off.
The Docker client also uses an empty private configuration, so it cannot inherit the
owner's proxy settings or remote Docker context.

Inside that boundary, Claude also gets safe/restricted mode, empty setting sources,
strict empty MCP configuration and only WebSearch/WebFetch. It never gets the owner's
prompt, saved facts, timezone, Telegram permissions or session pointer. Outbound web/API
networking is required; this does not replace network-level controls for an administrator's
private services. Root/Docker administrators remain trusted and can inspect guest data.

Tests cover command authorization, identities, revocation during a response, owner
preemption, persistent and aggregate budgets, real voice duration, isolated session flags,
and shared call-slot reservation. Linux CI additionally tries to read owner/other-guest
canaries and write outside the allowed directory, checks non-root/no-new-privileges, and
verifies cancellation removes the container. It also runs the checksum-pinned Linux CLI
2.1.286 against a synthetic local API, including restart/resume and cross-guest separation.
No CI test uses a real account or sends Telegram messages.

Real subscription authentication, voice quality, call audio/echo and server resource
headroom still need a controlled server smoke test before enabling guests for others.
See [performance notes](PERFORMANCE.md) for the measured baseline and remaining checks.
The [read-only measurement helper](tools/measure_resources.py) records process-tree
RSS/swap, service memory counters, model/venv file totals and selected image byte sizes.
Run it idle, during a call and after several calls; it never reads message contents,
environment variables or process arguments. Guest containers need a separate `docker stats`
sample because they belong to Docker's daemon, not the caller's process tree.
