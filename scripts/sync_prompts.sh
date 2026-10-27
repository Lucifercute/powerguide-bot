#!/usr/bin/env bash
# Send prompts/ and presets/ to the server. No restart needed: the bot reads both on every use.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/server.sh

rsync -az --delete -e "$SSH" prompts/ "$SERVER:$REMOTE_DIR/prompts/"
rsync -az --delete -e "$SSH" presets/ "$SERVER:$REMOTE_DIR/presets/"
echo "Prompts and presets synced."
