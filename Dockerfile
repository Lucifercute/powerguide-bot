FROM python:3.12-slim

# LibreOffice turns Word/PowerPoint sources into PDF (bot/sources.py); fonts with Cyrillic for it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-impress-nogui fonts-dejavu fonts-liberation fonts-crosextra-carlito \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # LibreOffice needs a writable home; the container runs as an unprivileged user.
    HOME=/tmp

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY questions.yaml ./
COPY bot ./bot

# prompts/, presets/, data/ and .env come from the host (docker-compose.yml).
CMD ["/app/.venv/bin/python", "-m", "bot"]
