#!/usr/bin/env bash
# Deploy the committed code to the server: push main, pull it there, rebuild and restart the bot.
# Uncommitted changes stop the deploy, so a half-done edit never reaches the prod bot.
# Prompts are not in git: they go by rsync (sync_prompts.sh). The server's .env and data/ are never touched.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/server.sh

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Есть незакоммиченные изменения — закоммитьте их перед деплоем:" >&2
    git status --short >&2
    exit 1
fi
if [[ "$(git branch --show-current)" != "main" ]]; then
    echo "Деплой только из main." >&2
    exit 1
fi

git push origin main
scripts/sync_prompts.sh
$SSH "$SERVER" "cd $REMOTE_DIR && git pull --ff-only && docker compose up -d --build \
    && docker image prune -f >/dev/null && docker compose ps && git log -1 --format='Deployed: %h %s'"
