#!/usr/bin/env bash
# One-command setup for the Telegram call assistant. Run it in this folder as root:
#     bash setup.sh
# You can run it again any time; it skips steps that are already done.
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
CONNECTOR="${CONNECTOR:-$(cd .. && pwd)/telegram-connector}"   # set up first, next to this folder
setting() { grep "^$1=" "$2" 2>/dev/null | cut -d= -f2- || true; }

echo "== 1/5 Installing programs =="
apt-get update -qq
apt-get install -y -qq python3-venv ffmpeg curl >/dev/null
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
echo "Programs ready."

echo "== 2/5 Settings =="
CLAUDE_BIN="$(command -v claude || true)"
# Some server panels (like xWriter) install their own copy of Claude Code.
PANEL_CLAUDE="$(ls -d /var/lib/xwriter-clis/tools/claude/*/claude 2>/dev/null | sort -V | tail -1)"
for c in "$HOME/.local/bin/claude" /usr/local/bin/claude /usr/bin/claude $PANEL_CLAUDE; do
  [ -z "$CLAUDE_BIN" ] && [ -x "$c" ] && CLAUDE_BIN="$c"
done
if [ -z "$CLAUDE_BIN" ]; then
  echo "Claude Code isn't installed. Install it and log in, then run this again:"
  echo "    curl -fsSL https://claude.ai/install.sh | bash"
  echo "    claude        (then type /login and follow the link)"
  exit 1
fi
echo "Claude Code found: $CLAUDE_BIN"
if [ ! -f .env ]; then
  read -rp "Your MAIN Telegram @username (the only account the assistant will talk to): " OWNER
  read -rp "Your first name, as the assistant should call you: " OWNER_NAME
  TZ_DEFAULT="$(setting TIMEZONE "$CONNECTOR/.env")"
  read -rp "Your time zone [${TZ_DEFAULT:-UTC}]: " TIMEZONE
  KEY="$(setting TRANSCRIBE_API_KEY "$CONNECTOR/.env")"
  if [ -z "$KEY" ]; then
    read -rsp "Paste your OpenAI API key (for understanding speech; it stays hidden): " KEY; echo
  else
    echo "Using the OpenAI key from the Telegram connector."
  fi
  read -rp "Use the Jarvis style (a British butler voice for English)? (Y/n): " JARVIS
  if [ "$JARVIS" = "n" ] || [ "$JARVIS" = "N" ]; then
    STYLE="TTS_EN=edge
PERSONA="
  else
    read -rp "How should Jarvis address you, e.g. sir or ma'am [sir]: " ADDRESS
    STYLE="TTS_EN=kokoro
KOKORO_VOICE=bm_lewis
PERSONA=jarvis
JARVIS_ADDRESS=\"${ADDRESS:-sir}\""
  fi
  umask 077
  cat > .env <<EOF
OWNER=$OWNER
OWNER_NAME="$OWNER_NAME"
TIMEZONE=${TIMEZONE:-${TZ_DEFAULT:-UTC}}
TRANSCRIBE_API_KEY=$KEY
CLAUDE_BIN=$CLAUDE_BIN
CLAUDE_MODEL=haiku
CLAUDE_MCP_CONFIG=$DIR/mcp.json
CLAUDE_ALLOWED_TOOLS=WebSearch,WebFetch,mcp__telegram__whoami,mcp__telegram__list_chats,mcp__telegram__find_chats,mcp__telegram__read_messages,mcp__telegram__search_messages,mcp__telegram__read_attachment,mcp__telegram__get_profile,mcp__telegram__list_scheduled
VOICE_FA=fa-IR-DilaraNeural
VOICE_EN=en-US-AvaMultilingualNeural
$STYLE
EOF
  echo "Settings saved (private file .env)."
else
  echo "Settings already exist, keeping them."
  if ! grep -q '^OWNER_NAME=' .env; then
    read -rp "Your first name, as the assistant should call you: " OWNER_NAME
    echo "OWNER_NAME=\"$OWNER_NAME\"" >> .env
  fi
  grep -q '^CLAUDE_MODEL=' .env || echo "CLAUDE_MODEL=haiku" >> .env
fi
if grep -q '^TTS_EN=kokoro' .env; then
  mkdir -p models
  for f in kokoro-v1.0.onnx voices-v1.0.bin; do
    if [ ! -s "models/$f" ]; then
      echo "Downloading Kokoro's $f (once; the model is about 350 MB)..."
      curl -L --fail --progress-bar -o "models/$f.part" \
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f"
      mv "models/$f.part" "models/$f"
    fi
  done
fi
# Lets Claude read (never send) your Telegram during calls, through your connector.
SECRET="$(setting MCP_SECRET "$CONNECTOR/.env")"
DOMAIN="$(setting DOMAIN "$CONNECTOR/.env")"
if [ -n "$SECRET" ] && [ -n "$DOMAIN" ]; then
  umask 077
  printf '{"mcpServers":{"telegram":{"type":"http","url":"https://%s/%s/mcp"}}}\n' "$DOMAIN" "$SECRET" > mcp.json
elif [ ! -f mcp.json ]; then
  echo "No connector address (MCP_SECRET and DOMAIN) in $CONNECTOR/.env: calls won't read your Telegram."
fi

echo "== 3/5 Logging in the assistant's own Telegram account =="
if [ -f data/account.json ]; then
  echo "Already logged in."
else
  venv/bin/python login.py
fi

echo "== 4/5 Starting =="
cat > /etc/systemd/system/telegram-caller.service <<EOF
[Unit]
Description=Telegram call assistant
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
Environment=HOME=$HOME
Environment=PATH=$(dirname "$CLAUDE_BIN"):/usr/local/bin:/usr/bin:/bin
ExecStart=$DIR/venv/bin/python caller.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable telegram-caller >/dev/null 2>&1
systemctl restart telegram-caller
echo "Started."

echo "== 5/5 Connecting it to your Telegram connector =="
CALLER="$(venv/bin/python -c "import json; a=json.load(open('data/account.json')); print('@'+a['username'] if a.get('username') else a['id'])")"
if [ -f "$CONNECTOR/.env" ]; then
  sed -i '/^CALLER=/d' "$CONNECTOR/.env"
  echo "CALLER=$CALLER" >> "$CONNECTOR/.env"
  (cd "$CONNECTOR" && docker compose up -d --build >/dev/null 2>&1) && echo "Connector updated: Claude can now call you."
else
  echo "No connector found in $CONNECTOR. Calling you from Claude won't work, but you can still call $CALLER."
fi

sleep 8
echo
journalctl -u telegram-caller -n 5 --no-pager -o cat || true
echo
echo "All set! The assistant is $CALLER."
echo " - Call it on Telegram from your main account, or"
echo " - send it a message:  /call   (or ask Claude: \"call me\")"
