#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${1:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Project directory does not exist: $PROJECT_DIR" >&2
  echo "Use an absolute path; do not use /~/..." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx ufw sqlite3

cd -- "$PROJECT_DIR"
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
