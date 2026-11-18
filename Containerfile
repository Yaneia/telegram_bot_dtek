# Multi-arch Podman/Docker Containerfile for Ukrainian Outage Tracker Telegram Bot
# Supports linux/amd64 and linux/arm64 targets (edge routers, NAS, servers)

# -----------------------------------------------------------------------------
# Stage 1: Build & Dependencies
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2: Hardened Runtime Container
# -----------------------------------------------------------------------------
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Ukrainian Electricity Outage Tracker (DTEK) Bot"
LABEL org.opencontainers.image.description="Outbound-only Telegram bot for DTEK electricity outage schedules"
LABEL org.opencontainers.image.authors="Artem Yaneia <artem@yaneia.me>"
LABEL org.opencontainers.image.source="https://github.com/Yaneia/telegram_bot_dtek"

# Runtime environment settings
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Kyiv \
    DATABASE_PATH=/app/data/dtek_bot.sqlite3 \
    HEARTBEAT_PATH=/tmp/bot_heartbeat

# Install minimal runtime system libraries for Pillow, curl_cffi, TLS, fonts, and timezone
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-dejavu-core \
    libjpeg62-turbo \
    zlib1g \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user and group (UID/GID 1000)
RUN groupadd -g 1000 botuser && \
    useradd -u 1000 -g 1000 -d /app -s /bin/sh botuser

WORKDIR /app

# Pre-create data directory for SQLite persistence and chown to botuser
RUN mkdir -p /app/data /tmp && chown -R botuser:botuser /app /tmp

# Copy installed Python packages from builder stage
COPY --from=builder /install /usr/local

# Copy application source code
COPY --chown=botuser:botuser bot/ ./bot/

# Switch to unprivileged non-root user
USER botuser

# Healthcheck monitoring SQLite connectivity and polling loop heartbeat
HEALTHCHECK --interval=60s --timeout=5s --retries=3 --start-period=15s \
    CMD ["python", "-m", "bot.healthcheck"]

# Entrypoint / command (outbound-only polling, zero inbound open ports)
CMD ["python", "-m", "bot.main"]
