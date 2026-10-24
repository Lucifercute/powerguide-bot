#!/usr/bin/env bash
# Send prompts/ to the server. No restart needed: the bot reads prompts on every call.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/server.sh

rsync -az --delete -e "$SSH" prompts/ "$SERVER:$REMOTE_DIR/prompts/"
echo "Prompts synced."
