#!/usr/bin/env bash
# Polyweat - Ubuntu VPS installer.
#
# Usage:
#   sudo bash deploy/install.sh
#
# This script:
#   1. installs Python 3 + venv + pip + sqlite3 + git
#   2. creates a dedicated `polyweat` system user (no shell)
#   3. clones / refreshes the repo into /opt/polyweat
#   4. creates a virtualenv and installs requirements
#   5. seeds /opt/polyweat/.env from .env.example if missing
#   6. installs and enables the systemd unit (commented at the bottom)
#
# It is safe to re-run.

set -euo pipefail

APP_USER="polyweat"
APP_DIR="/opt/polyweat"
REPO_URL="${POLYWEAT_REPO_URL:-https://github.com/bangjet12/Polyweat.git}"
BRANCH="${POLYWEAT_BRANCH:-main}"

if [[ "$EUID" -ne 0 ]]; then
  echo "ERROR: please run as root (sudo bash deploy/install.sh)" >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip sqlite3 git curl ca-certificates

echo "==> Creating user '$APP_USER' (if missing)"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

echo "==> Setting up $APP_DIR"
mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> Fetching code from $REPO_URL ($BRANCH)"
if [[ -d "$APP_DIR/.git" ]]; then
  sudo -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune
  sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only origin "$BRANCH"
else
  sudo -u "$APP_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> Creating Python virtualenv"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

echo "==> Seeding .env from .env.example (only if .env is missing)"
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  echo "    -> $APP_DIR/.env created. Edit it before running live."
fi

echo "==> Initialising database"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/polyweat" --env "$APP_DIR/.env" init-db

echo "==> Installing systemd unit (polyweat.service)"
install -m 644 "$APP_DIR/deploy/polyweat.service" /etc/systemd/system/polyweat.service
systemctl daemon-reload

cat <<'EOF'

==============================================================
Polyweat installed successfully.

Default mode is DRY_RUN (no real orders). To enable live trading
you must set BOTH in /opt/polyweat/.env:

    DRY_RUN=false
    LIVE_TRADING=true

and also fill in the POLYMARKET_* credentials.

Useful commands:
    sudo systemctl enable --now polyweat
    sudo systemctl status polyweat
    sudo journalctl -u polyweat -f
    sudo -u polyweat /opt/polyweat/.venv/bin/polyweat --env /opt/polyweat/.env status
    sudo -u polyweat /opt/polyweat/.venv/bin/polyweat --env /opt/polyweat/.env scan-once
==============================================================
EOF
