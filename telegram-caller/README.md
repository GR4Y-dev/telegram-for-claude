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
| Any text message | Replies as text, with a typing indicator and conversation memory |
| A voice note | Sends a transcript, then a text answer (up to 5 minutes / 20 MB) |
| `/new` | Starts a fresh text conversation; does not reset a call |

Text and voice notes share one conversation while the service is running. A service restart
starts a new chat; previous Claude session files remain subject to the storage limit. Messages
are processed in order, including `/new`. Calls have separate conversations. Only private
messages from the configured owner are accepted; group messages are ignored.

Every chat and call turn includes the current time in `TIMEZONE` and UTC. Set `TIMEZONE` to
`Europe/Istanbul` for Istanbul time; the assistant does not assume the server OS uses that zone.
Replies use the existing Claude Code login and voice notes use the existing transcription key.
Claude login or usage-limit problems produce a retry message. No new keys are required.

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
| `VOICE_FA` | Persian voice: `fa-IR-DilaraNeural` (female) or `fa-IR-FaridNeural` (male) |
| `VOICE_EN` | English voice when `TTS_EN=edge`, e.g. `en-US-AvaMultilingualNeural` |
| `CLAUDE_MODEL` | `haiku` (fast, the default here), or empty for your account's default model |
| `TIMEZONE` | Your time zone (for "good morning" and times) |
| `END_OF_TURN_MS` | How long a pause ends your turn, in milliseconds (default 1200). Lower answers sooner but may cut you off mid-sentence |
| `RING_SECONDS` | How long it rings you (default 45) |
| `QUIET_HANGUP_SECONDS` | Hangs up after this long without hearing you (default 90) |
| `MAX_CALL_MINUTES` | Longest call (default 30) |
| `STORAGE_LIMIT_MB` | Most disk space for saved call conversations; the oldest are deleted first (default 1024, `0` = no limit) |
