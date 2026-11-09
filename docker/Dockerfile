FROM python:3.15-slim-bookworm

LABEL org.opencontainers.image.title="Moon Watcher" \
      org.opencontainers.image.description="Telegram bot for monitoring websites, domains, Docker containers and VPS resources" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user
RUN groupadd --gid 1000 botuser && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash --create-home botuser

# whois client is used by python-whois
RUN apt-get update && apt-get install -y --no-install-recommends \
        whois \
        ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

# App code
COPY --chown=botuser:botuser src ./src
COPY --chown=botuser:botuser docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x entrypoint.sh

USER botuser

# The bot touches this file every 30s from its event loop; a stale file means it is stuck or dead
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,time; s=os.stat('/tmp/bot_healthy'); exit(0 if time.time()-s.st_mtime<120 else 1)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
