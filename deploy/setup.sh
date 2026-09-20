#!/usr/bin/env bash
# Sets up the call server on a fresh Ubuntu VM (Azure, Oracle, any VPS).
#
#   bash deploy/setup.sh yourhost.centralindia.cloudapp.azure.com
#
# Afterwards the server runs on every boot, behind HTTPS, and you paste
# the URL it prints into the app. Safe to re-run.
set -euo pipefail

HOST="${1:-}"
if [ -z "$HOST" ]; then
    echo "Usage: bash deploy/setup.sh <your-public-hostname>"
    exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$REPO_DIR/phase1"
USER_NAME="$(id -un)"

echo "==> System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https

# Scheduled calls and quiet hours use the server's local time, so a VM
# left on UTC would call you at the wrong hours.
echo "==> Time zone: Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata

echo "==> Python environment"
cd "$APP_DIR"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements-server.txt

if [ ! -f "$APP_DIR/.env" ]; then
    cat > "$APP_DIR/.env" <<'EOF'
GROQ_API_KEY=
FIREBASE_SERVICE_ACCOUNT_JSON=firebase-adminsdk.json
EOF
    echo "    created phase1/.env -- put your Groq key in it"
fi

echo "==> Caddy (HTTPS, free certificate, renews itself)"
if ! command -v caddy >/dev/null; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq caddy
fi

# Caddy terminates HTTPS and passes requests (including the call's
# WebSocket, which it handles automatically) to the server on localhost.
sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$HOST {
    reverse_proxy 127.0.0.1:8765
}
EOF
sudo systemctl restart caddy

echo "==> Service (starts on boot, restarts if it crashes)"
sudo tee /etc/systemd/system/heywaifu.service >/dev/null <<EOF
[Unit]
Description=HeyWaifu call server
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
Environment=CALL_TLS=0
Environment=PUBLIC_URL=https://$HOST
ExecStart=$APP_DIR/venv/bin/python -m call
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q heywaifu

# Starting without a key would just restart-loop, so stop here and say so.
if ! grep -q '^GROQ_API_KEY=.\+' "$APP_DIR/.env"; then
    echo
    echo "Almost done -- the server needs your keys before it can start:"
    echo "  1. nano $APP_DIR/.env          (paste your GROQ_API_KEY)"
    echo "  2. copy firebase-adminsdk.json into $APP_DIR/"
    echo "  3. bash deploy/setup.sh $HOST  (re-run this script)"
    exit 0
fi
sudo systemctl restart heywaifu

sleep 4
echo
echo "===================================================================="
if [ -f "$APP_DIR/.call_token" ]; then
    echo "Paste this into the app (Settings -> Connection -> Server address):"
    echo "    https://$HOST/s/$(cat "$APP_DIR/.call_token")"
else
    echo "Service didn't start cleanly. Check: journalctl -u heywaifu -n 40"
fi
echo "===================================================================="
echo
systemctl --no-pager --lines=5 status heywaifu || true
