# syntax=docker/dockerfile:1

# WebTorrent bridge dependencies (native modules are built/downloaded here).
FROM node:22-bookworm AS bridge
RUN apt-get update \
    && apt-get install -y --no-install-recommends cmake \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build/torrent_bridge
COPY torrent_bridge/package.json torrent_bridge/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

# Runtime image: Node 22 (for the bridge) + Python 3.11 (for Flask).
FROM node:22-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app

COPY requirements.txt ./
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt gunicorn==23.0.0

COPY --from=bridge /build/torrent_bridge/node_modules torrent_bridge/node_modules
COPY torrent_bridge/package.json torrent_bridge/package-lock.json torrent_bridge/
COPY torrent_bridge/server.mjs torrent_bridge/

COPY . .

RUN mkdir -p /app/.app_cache /app/state /data \
    && chown -R app:app /app /data

USER app

EXPOSE 8080 51413/tcp 51413/udp

# TCP check only: the HTTP endpoints require basic auth when APP_USER is set.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python3 -c "import socket; s = socket.create_connection(('127.0.0.1', 8080), 4); s.close()"

# One worker is intentional: media tokens and server-side fallback Cast state live in memory.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "16", \
     "--timeout", "0", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
