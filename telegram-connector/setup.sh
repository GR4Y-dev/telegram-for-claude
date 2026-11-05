#!/usr/bin/env bash
# One-command setup for the Telegram connector. Run it in this folder:
#     bash setup.sh
# You can run it again any time; it skips steps that are already done.
set -e
cd "$(dirname "$0")"

echo "== 1/4 Checking Docker =="
if ! command -v docker >/dev/null 2>&1; then
  read -rp "Docker isn't installed. Install it now? (y/N): " ok
  if [ "$ok" = "y" ] || [ "$ok" = "Y" ]; then
    curl -fsSL https://get.docker.com | sh
  else
    echo "Docker is needed. Stopping."; exit 1
  fi
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker is installed but 'docker compose' is missing. Send Claude this message."; exit 1
fi
echo "Docker is ready."

echo "== 2/4 Settings =="
if [ ! -f .env ]; then
  SECRET=$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
  read -rsp "Paste your Cloudflare tunnel token (it stays hidden), then press Enter: " TOKEN; echo
  if [ -z "$TOKEN" ]; then echo "No token pasted. Run bash setup.sh again."; exit 1; fi
  read -rp "Your time zone, e.g. Europe/Istanbul or Asia/Tehran [UTC]: " TIMEZONE
  umask 077
  cat > .env <<EOF
MCP_SECRET=$SECRET
TUNNEL_TOKEN=$TOKEN
TIMEZONE=${TIMEZONE:-UTC}
MAX_SENDS_PER_HOUR=30
EOF
  echo "Settings saved (private file .env)."
else
  echo "Settings already exist, keeping them."
fi
if ! grep -q '^DOMAIN=' .env; then
  read -rp "The address you gave the tunnel in Cloudflare, e.g. telegram.example.com: " DOMAIN
  echo "DOMAIN=$DOMAIN" >> .env
fi
DOMAIN="$(grep '^DOMAIN=' .env | cut -d= -f2)"

echo "== 3/4 Building and logging in to Telegram =="
# Compose reads COMPOSE_FILE from .env, including the optional WhatsApp store override.
docker compose build
if docker compose run --rm --no-deps telegram test -f /data/account.json; then
  echo "Already logged in to Telegram."
else
  docker compose run --rm --no-deps telegram python login.py
fi

echo "== 4/4 Starting =="
docker compose up -d
MCP_SECRET=$(grep '^MCP_SECRET=' .env | cut -d= -f2)
echo
echo "All set! Your connector address is:"
echo
echo "    https://$DOMAIN/$MCP_SECRET/mcp"
echo
echo "Treat it like a password: anyone with it can use your Telegram."
echo "Paste it ONLY into claude.ai > Settings > Connectors > Add custom connector."
if [ -f ../update.sh ] && [ ! -f "/etc/systemd/system/$(basename "$(cd .. && pwd)")-update.timer" ]; then
  echo
  read -rp "Update automatically every night (only versions whose tests passed)? (Y/n): " ok
  if [ "$ok" != "n" ] && [ "$ok" != "N" ]; then bash ../update.sh --install; fi
fi
