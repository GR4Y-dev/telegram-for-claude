# Telegram connector: reference

Setting it up for the first time? Follow `../README.md` (the main guide). This page is for later.

Lets Claude use Telegram **as your own account**: read chats (also far back, by date),
open attachments (photos, PDFs, Word, Excel, text files), understand voice messages
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

## Settings (`.env`)

| Setting | What it does |
|---|---|
| `TIMEZONE` | Your time zone, used for scheduled messages and dates |
| `MAX_SENDS_PER_HOUR` | Safety limit for sends, files, forwards and edits together (default 30) |
| `MAX_ATTACHMENT_MB` | Bigger attachments aren't opened (default 20) |
| `MAX_UPLOAD_MB` | Biggest file Claude may send from a web link (default 50) |

## Safety limits built in
- At most 30 sends, files, forwards and edits per hour together, so the account never looks like spam.
- Reading chats does **not** mark them as read. Only `mark_read` does, when you ask for it.
- Claude can only edit your own messages. Deleting is at most 50 messages at once.
- Links to private or local addresses are refused.
- Nothing piles up on disk: attachments are opened in memory and never saved, and logs are capped
  at 3 × 10 MB per container.
- No joining groups, changing your profile, or touching your contacts or settings.
