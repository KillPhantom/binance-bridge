#!/usr/bin/env bash
set -euo pipefail

: "${SERVER_USER:?Set SERVER_USER, for example ubuntu}"
: "${SERVER_HOST:?Set SERVER_HOST to the Vultr IP or hostname}"
: "${SERVER_PATH:?Set SERVER_PATH, for example /home/ubuntu/tv-binance-bridge}"

cd "$(dirname "$0")/.."

rsync -avz --delete \
  --exclude '.env' \
  --exclude 'venv/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude 'bridge.db' \
  --exclude 'bridge.db-shm' \
  --exclude 'bridge.db-wal' \
  --exclude '.git/' \
  ./ "${SERVER_USER}@${SERVER_HOST}:${SERVER_PATH}/"
