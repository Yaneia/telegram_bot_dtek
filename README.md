# DTEK Outage Tracker — Telegram Bot & Mini App

Telegram bot and Telegram Mini App (TMA) for tracking Ukrainian electricity outage schedules (DTEK) and comparing two outage groups on a single timeline.

Supported regions: **Kyiv**, **Kyiv Oblast**, **Dnipro**, **Odesa**.

## Features

- **Outbound-only bot** — Aiogram 3.15+, long polling, no open inbound ports.
- **Resilient 5-tier data source cascade** with a circuit breaker:
  1. Direct DTEK scrape (Chrome 120 TLS impersonation)
  2. Community GitHub raw mirror
  3. YASNO API
  4. SQLite cache
  5. Deterministic mock fixtures
- **Normalizer** — 7 DTEK statuses → 48 uniform 30-minute intervals (Europe/Kyiv), emergency override.
- **Dual-slot comparison** — 3×3 matrix of joint availability for two groups (e.g. "Home" and "Office") with daily summary metrics.
- **Two presentation modes in chat:**
  - 1200×640 Retina PNG timeline card (Pillow)
  - Monospace Unicode table (≤ 28 chars wide) with inline navigation
- **Notifications** — 30 / 15-minute advance warnings and schedule-change alerts (hash diff), deduplicated, rate-limited to 25 msg/s.
- **Mini App** — zero-build single-file SPA (`docs/index.html`) hosted on GitHub Pages; schedule data is passed in the URL fragment (`#p=<zlib_base64url>`), settings sync back to the bot via `sync_<b64>` deep links.

## Bot commands

| Command | Description |
|---|---|
| `/start` | Onboarding (also accepts `sync_<b64>` deep links from the Mini App) |
| `/status` | Current schedule card; toggle image / text mode |
| `/slots` | Configure two groups (region → group → subgroup) |
| `/settings` | Notifications and display settings |
| `/help` | Help |

## Project structure

```
bot/
  main.py            # entry point (long polling + background loops)
  config.py          # settings (.env)
  healthcheck.py     # container healthcheck CLI
  database/          # aiosqlite WAL schema, connection, repositories
  handlers/          # commands, callbacks, keyboards
  models/            # domain models and enums
  rendering/         # PNG card and text table renderers
  services/          # scraper, normalizer, comparison, notifier, rate limiter, TMA payload
  fixtures/          # deterministic mock schedules for 4 regions
docs/index.html      # Telegram Mini App (GitHub Pages)
tests/               # offline test suite
Containerfile, compose.yaml
```

## Quick start (local)

Requires Python 3.11+.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # set BOT_TOKEN
.venv/bin/python -m bot.main
```

## Configuration

All settings are read from environment variables or `.env` — see [`.env.example`](.env.example).

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | — | Bot token from @BotFather (required) |
| `BOT_USERNAME` | `dtek_tracker_bot` | Bot username for deep links |
| `WEBAPP_URL` | `https://yaneia.github.io/telegram_bot_dtek` | Mini App URL |
| `DATABASE_PATH` | `data/bot.db` | SQLite database path |
| `SCRAPE_INTERVAL_SECONDS` | `600` | Schedule refresh interval |
| `NOTIFICATION_CHECK_INTERVAL_SECONDS` | `60` | Notification loop interval |
| `MOCK_FALLBACK_ONLY` | `false` | Use only mock fixtures (offline demo) |
| `DEFAULT_RENDER_MODE` | `image` | `image` or `text` |
| `LOG_LEVEL` | `INFO` | Logging level |

## Deployment (Podman / Docker)

```bash
cp .env.example .env        # set BOT_TOKEN
podman compose up -d --build   # or: docker compose up -d --build
```

The container runs as a non-root user with a read-only root filesystem, tmpfs `/tmp`, a 256 MB memory limit, a persistent `./data` volume for SQLite, and **no published ports**. Multi-arch build (`linux/amd64`, `linux/arm64`) is supported via the `Containerfile`.

## Mini App (GitHub Pages)

The `.github/workflows/pages.yml` workflow deploys `docs/` to GitHub Pages on every push to `main` that touches `docs/`. Enable Pages in the repository settings (**Settings → Pages → Source: GitHub Actions**), then point `WEBAPP_URL` and the bot's menu button in @BotFather to the published URL.

## Tests

The test suite is fully offline and uses deterministic fixtures.

```bash
.venv/bin/python -m pytest
```

## Author

**Artem Yaneia** — [artem@yaneia.me](mailto:artem@yaneia.me)
