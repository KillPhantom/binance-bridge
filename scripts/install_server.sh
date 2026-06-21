#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/home/ubuntu/tv-binance-bridge}"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx ufw sqlite3

cd "$PROJECT_DIR"
if [[ ! -d venv ]]; then
  python3 -m venv venv
fi
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt

if [[ -f .env ]]; then
  echo "Existing $PROJECT_DIR/.env preserved."
else
  echo "Create $PROJECT_DIR/.env manually from .env.example, then chmod 600 .env."
fi
echo "Next: install the systemd and Nginx examples as described in README.md."
