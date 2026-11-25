#!/usr/bin/env bash
# Run as root on the server, from a clone of this repository:  sudo bash deploy_bot.sh
# Installs the bot as a systemd service that restarts on crash and at boot. Re-run it to update (it pulls first) or to change .env.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
git pull -q --ff-only origin Main 2>/dev/null || true
command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }
python3 -c "import venv, ensurepip" 2>/dev/null || { apt-get update -qq && apt-get install -y -qq python3-venv; }
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

ask() { # name, prompt, secret?
  local cur; cur="$(grep -s "^$1=" .env | head -1 | cut -d= -f2- || true)"
  [ -n "$cur" ] && return 0
  local v=""; read -r -p "$2: " v </dev/tty
  [ -n "$v" ] || { echo "$1 is required"; exit 1; }
  printf '%s=%s\n' "$1" "$v" >> .env
}
touch .env && chmod 600 .env
ask TELEGRAM_BOT_TOKEN "Telegram bot token"
ask ADMIN_CHAT_ID "Your Telegram chat id (the bot will message you 'Bot is online')"
for kv in "PIPELINE_REPO=sonyeyas-beep/eyas-finetune" "PIPELINE_REF=Main" "DRY_RUN=1" "ORDERS_FILE=$PWD/orders.json"; do
  grep -q "^${kv%%=*}=" .env || echo "$kv" >> .env
done

cat > /etc/systemd/system/order-bot.service <<UNIT
[Unit]
Description=order bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$PWD
EnvironmentFile=$PWD/.env
ExecStart=$PWD/venv/bin/python $PWD/bot.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now order-bot
systemctl restart order-bot
sleep 4
systemctl is-active order-bot
journalctl -u order-bot -n 8 --no-pager | sed 's/bot[0-9]*:[A-Za-z0-9_-]*/bot<token>/g'
echo "logs: journalctl -u order-bot -f"
