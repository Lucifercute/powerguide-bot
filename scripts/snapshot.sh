#!/usr/bin/env bash
# Copy the server's database and session files to snapshots/<date-time>/ for debugging.
# Only server → Mac: the local data/ (dev database) is not touched.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/server.sh

target="snapshots/$(date +%Y-%m-%d_%H-%M)"
mkdir -p "$target"
# A consistent copy of the database even while the bot is writing to it.
$SSH "$SERVER" "docker exec powerguide-bot /app/.venv/bin/python -c \
\"import sqlite3; s = sqlite3.connect('data/bot.db'); d = sqlite3.connect('data/snapshot.db'); s.backup(d); d.close()\""
rsync -az -e "$SSH" "$SERVER:$REMOTE_DIR/data/snapshot.db" "$target/bot.db"
rsync -az -e "$SSH" "$SERVER:$REMOTE_DIR/data/sessions/" "$target/sessions/"
$SSH "$SERVER" "rm -f $REMOTE_DIR/data/snapshot.db"
echo "Snapshot: $target"
