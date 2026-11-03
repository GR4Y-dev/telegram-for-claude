# Telegram call assistant: reference

Setting it up for the first time? Follow `../README.md` (the main guide). This page is for later.

Your AI assistant gets its own Telegram account (a second account) and talks with you on
Telegram calls, in Persian or English:

- **You call it** from your main account: it answers.
- **It calls you** when you send it `/call` (or `call me`, `زنگ بزن`), or when you ask Claude
  "call me" / "call me at 8 tomorrow and remind me about X". If you don't pick up, the message
  arrives as text.

On a call it takes turns: talk, then pause for about a second. It writes out what you said
(OpenAI speech-to-text), asks Claude Code on the server for an answer (you hear a soft tick
while it thinks) and starts speaking as soon as the first sentence is ready. Say "bye" /
"خداحافظ" to end, or just hang up. During a call Claude can search the web and read your
Telegram, but it can't send, edit or delete anything. It only ever talks to your main account.

## Messages it understands (from your main account only)

| Message | What it does |
|---|---|
| `/call` (or `call me`, `زنگ بزن`), optionally followed by what to say | Calls you |
| `/text <message>` | Texts you the message from this account (Claude's `text_me` uses it) |
| `/update` | Updates every project on the server that has automatic updates on, and texts you the result |
| Any text message | Replies as text, with a typing indicator and conversation memory. Ask it to call you, text you later or update the server, and it does |
| A voice note | Sends a transcript, then a text answer (up to 5 minutes / 20 MB) |
| `/new` | Starts a fresh text conversation; keeps saved facts and reminders and does not reset a call |

Text and voice notes share one conversation, which continues across restarts and updates until
you send `/new` (or until the storage limit removes its oldest history). Messages are processed
in order, including `/new`. Calls have separate conversations. Only private
messages from the configured owner are accepted; group messages are ignored.

Every chat and call turn includes the current time in `TIMEZONE` and UTC. Set `TIMEZONE` to
`Europe/Istanbul` for Istanbul time; the assistant does not assume the server OS uses that zone.
Replies use the existing Claude Code login and voice notes use the existing transcription key.
Claude login or usage-limit problems produce a retry message. No new keys are required.

## Saved facts and reminders

Only direct private messages from your configured owner account can change saved facts and
reminders. English phrases below and slash commands are recognized without a model. Other
languages still work for ordinary conversation; use slash commands for reliable scheduling.
Forwarded text and voice notes are treated as quoted content and do not run commands.

| Send | Result |
|---|---|
| `/remember I prefer tea` or `Remember that I prefer tea` | Saves a fact until you remove it |
| `/memories` | Lists saved facts and IDs |
| `/forget 2` | Deletes fact #2 and starts fresh to avoid reusing that fact from the active chat |
| `/remind in 20 minutes \| Take a break` | One-time reminder; hours and days also work |
| `Remind me tomorrow at 8am to study` | One-time reminder in `TIMEZONE` |
| `/remind 2027-12-10 09:00 \| Bring my passport` | Reminder at an explicit date and time |
| `/remind daily 08:00 \| Study` | Every day at 08:00 |
| `/remind weekdays 08:00 \| Study` | Monday through Friday |
| `Remind me every Wednesday at 8am to exercise` | Weekly on Wednesday |
| `/remind weekly 08:00 \| Review tasks` | Weekly on today's weekday |
| `/reminders` or `/tasks` | Lists unfinished tasks, dates and IDs |
| `/snooze 2 20` or `snooze #2 for 20 minutes` | Postpones reminder #2 by 20 minutes |
| `/done 2` or `done #2` | Removes reminder #2, including its repeating series |
| `/help` | Shows the command examples |

The confirmation always shows the date, time zone and reminder ID. Times without am/pm use
the 24-hour clock. An unclear time produces help instead of a guessed reminder. There can be
100 saved facts and 100 unfinished reminders per owner, with 2,000 characters per item.
Delivered one-time reminders remain in `/tasks` until you mark them done or snooze them.
Snoozing accepts 1 minute through 7 days and does not change a repeating reminder's usual time.

The worker checks every 15 seconds, independently of Claude and the current conversation.
After downtime, a missed reminder sends once; recurring reminders then advance to their next
future occurrence without sending a backlog. Failed sends stay pending. A crash immediately
after Telegram accepts a message can cause a duplicate on retry; delivery is not exactly-once.
The assistant must be running and connected to Telegram. Voice commands additionally need
transcription; text commands and reminder delivery do not spend Claude usage.

Repeats keep their local clock time in the zone recorded when created. A later `TIMEZONE`
change applies to new reminders; recreate existing ones to change their zone. During a
daylight-saving gap, an existing repeat moves forward through the gap. During a repeated hour,
it sends on the first occurrence. Creating a reminder at an ambiguous or skipped time asks
you to choose another time.

Facts are supplied to future chat and call turns as reference text, capped at 12 KB per turn;
`/memories` still lists all saved facts. Deleting facts starts a new text session; adding them preserves it.
An active call starts fresh on its next turn if its supplied facts changed. `/forget` removes
the fact from the active database but does not erase old Telegram messages, Claude transcript
files, provider history or backups. `/done` removes the reminder text from the active database.

State is in `data/assistant.sqlite3` beside `account.json`, separate from Claude's existing
`data/chat.json` session pointer and conversation history. `STATE_FILE` can override this
location. New database and chat files are created with private permissions on Linux. The
database is not encrypted. To back it up consistently, stop `telegram-caller`, copy its whole
`data` directory to a private backup, then start it again. Do not copy a live SQLite file on
its own while its WAL file is in use. Updates preserve this state; no new API key or manual
database setup is needed. Run only one assistant process against a given state file.

## Everyday commands

| What | Command |
|---|---|
| Is it running? | `systemctl status telegram-caller` |
| What happened on calls | `journalctl -u telegram-caller -n 50 --no-pager` |
| Restart (after changing `.env`) | `systemctl restart telegram-caller` |
| Stop / start | `systemctl stop telegram-caller` / `systemctl start telegram-caller` |
| Update now | send `/update` to the assistant, or on the server `bash ../update.sh` |

## Settings (`.env` in this folder, then restart)

| Setting | What it does |
|---|---|
| `OWNER_NAME` | What the assistant calls you |
| `PERSONA` | `jarvis` for the British butler style, empty for a plain assistant |
| `JARVIS_ADDRESS` | How Jarvis addresses you (`sir`, `ma'am`, your name, ...) |
| `TTS_EN` | English voice engine: `kokoro` (runs on your server) or `edge` (Microsoft, online) |
| `KOKORO_VOICE` | Kokoro voice: `bm_lewis`, `bm_george`, `bm_daniel`, `bm_fable` (British), `am_michael`, `af_heart`, ... |
| `KOKORO_MODEL` | `fp32` (existing voice, default) or smaller `int8`; listen before switching |
| `KOKORO_THREADS` | Local voice CPU threads, clamped to 1–2 (default 2) |
| `CLAUDE_CALL_PROCESS` | `stream` (default, one Claude process per call), or `per-turn` for older/incompatible wrappers |
| `BARGE_IN` | `1` lets you interrupt speech (default); `0` restores strict turn-taking |
| `VAD_MODE` | `webrtc` (default speech detector), or `energy` for the older detector |
| `VOICE_FA` | Persian voice: `fa-IR-DilaraNeural` (female) or `fa-IR-FaridNeural` (male) |
| `VOICE_EN` | English voice when `TTS_EN=edge`, e.g. `en-US-AvaMultilingualNeural` |
| `CLAUDE_MODEL` | `haiku` (fast, the default here), or empty for your account's default model |
| `TIMEZONE` | Your time zone (for "good morning" and times) |
| `STATE_FILE` | Optional path for saved facts and reminders; defaults beside `ACCOUNT_FILE` |
| `END_OF_TURN_MS` | How long a pause ends your turn, in milliseconds (default 1200). Lower answers sooner but may cut you off mid-sentence |
| `RING_SECONDS` | How long it rings you (default 45) |
| `QUIET_HANGUP_SECONDS` | Hangs up after this long without hearing you (default 90) |
| `MAX_CALL_MINUTES` | Longest call (default 30) |
| `STORAGE_LIMIT_MB` | Most disk space for saved call conversations; the oldest are deleted first (default 1024, `0` = no limit) |

## Lower memory and quicker calls

English Kokoro starts only when it is needed and stops with the call. Persian calls do
not load it. During a call, Claude stays open between turns, with the same permissions
and fresh time/fact context. Hanging up cancels outstanding work. Speaking continuously
for about 350 ms interrupts playback; brief clicks are ignored. Keep speaker volume low
enough to avoid microphone echo triggering an interruption. `BARGE_IN=0` is available
for devices without good echo cancellation. The pause before an answer remains 1.2 seconds.

To try the smaller voice, set `KOKORO_MODEL=int8`, run
`venv/bin/python models.py --env .env`, then restart the caller and listen to English.
Downloads are checked against pinned sizes and SHA-256 hashes before replacement.
After you are happy with the voice, `venv/bin/python models.py --env .env --prune-unused`
removes only the known unused model. The updater downloads missing selected files but
keeps the previous model for rollback until you choose to remove it. Returning to
`KOKORO_MODEL=fp32` and running the same downloader restores the original model.

The 2.1.286 stream protocol was checked with two turns through the exact released CLI
and a local fake API. The live server wrapper, subscription login and actual call latency
still need a server check. If the stream exits before producing any protocol output,
the call falls back to per-turn mode. Partial responses and timeouts are not replayed.
See [performance notes](../PERFORMANCE.md) for measured results and remaining checks.
