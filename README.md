# HR-Adrobot-Keitaro

Adrobot — test assignment: Keitaro campaign creator and a draft-based offer editor.

The repository includes the service scaffold, SQLAlchemy models and migrations,
local offer drafts, and a server-side Keitaro Admin API client. Campaign creation
and stream-offer updates are available through Python calls in the FastAPI backend.

## Architecture (approved A-01 to A-04)

- `frontend/` — React 19 / TypeScript / SCSS + Vite.
- `backend/django/` — Django 5.2 / DRF: authentication, administrator settings, future Telegram/monitoring models. Django migrations own its tables only.
- `backend/fastapi/` — FastAPI / SQLAlchemy 2.x / Alembic: Keitaro integration, campaigns, streams, snapshots, drafts, offers and operations (to be added in later commits). Alembic owns `kt_*` tables only.
- `backend/bot/` — aiogram skeleton. No bot polling until a valid token is configured.
- `postgres` — one PostgreSQL DB; `redis` — Celery broker/result store.
- Django communicates with FastAPI over an authenticated internal HTTP API. Keitaro credentials are server-only.
- Keitaro is the source of truth for published data; drafts and removed-offer history remain local.

## Requirements

Docker Desktop with Compose v2 and Git Bash. For local non-Docker development:
Python 3.12+, Node.js 22+, PostgreSQL 17, Redis 7.4.

## First-time setup (Git Bash)

```bash
git clone https://github.com/alexanderanuchin/HR-Adrobot-Keitaro.git
cd HR-Adrobot-Keitaro
# Extract the provided scaffold ZIP into THIS directory, preserving paths.
# .env is local and ignored. If absent: cp .env.example .env
# Review .env and set local passwords/secrets; add real Keitaro API key only locally later.
docker compose config --quiet
docker compose up -d --build postgres redis fastapi django frontend
docker compose exec django python manage.py migrate --noinput
docker compose exec fastapi alembic upgrade head
# Only if needed, create a local Django administrator:
docker compose exec django python manage.py createsuperuser
```

Services:

- UI: http://localhost:5173
- Django admin: http://localhost:8001/admin/
- Django health: http://localhost:8001/api/v1/health/
- FastAPI lives **only on the internal Compose network** (`fastapi:8000`).

To run worker and DB-backed scheduler (skeleton only; no monitoring schedule is configured):

```bash
docker compose --profile automation up -d celery_worker celery_beat
```

To enable Telegram later, populate `TELEGRAM_BOT_TOKEN` in `.env` and run:

```bash
docker compose --profile telegram up -d telegram_bot
```

**Do not launch the Telegram bot without an authorized token.** The bot currently implements only `/start` and `/status` informational messages, no campaign mutations or real monitoring.

## Migration ownership

**Django** owns `accounts_user`, auth/session/admin tables and future monitoring/settings tables:

```bash
docker compose exec django python manage.py makemigrations --check --dry-run
docker compose exec django python manage.py migrate --noinput
```

**SQLAlchemy** owns future `kt_*` tables. The baseline revision is intentionally empty:

```bash
docker compose exec fastapi alembic current
docker compose exec fastapi alembic upgrade head
```

Future schema updates must use migrations from the *owning* ORM. Alembic autogenerate will be scoped to the `kt_*` family when models are added. Never run two independent migrations against the same table.

## Checks

```bash
# Requires Docker containers:
docker compose exec django python manage.py check
docker compose exec django python manage.py test accounts --noinput
docker compose exec fastapi python -m pytest -q
# Frontend dependencies installed in container during build:
docker compose exec frontend npm run typecheck
docker compose exec frontend npm run build
# CI will run all static checks/builds and migration smoke tests.
```

Run `docker compose logs --tail=100 django fastapi frontend` for troubleshooting.
Run `docker compose down` to stop services; **do not use `down -v`** unless you deliberately want to destroy the local database volume.

## Credentials and safety

`.env` is ignored and never committed. `.env.example` only contains placeholders. Local demo secrets are generated per ZIP. Never put employer credentials, an API token, Telegram token or database password into Git, screenshots, logs or Actions YAML. Rotate any previously exposed real passwords.

`app.services.campaign_creation.create_campaign` uses `KeitaroAdminClient` to create
a disabled campaign, verify the two streams, and enable it. On failure, inspect
the reported stage and campaign ID in Keitaro before retrying. A timeout can occur
after the tracker applies a write.

`KeitaroAdminClient.replace_stream_offers` sends the desired list of offer IDs,
shares and states to the flow update endpoint. Draft positions marked as removed
are omitted when building the list for publication.

The tests use simulated Admin API responses. Live verification should cover the
campaign's domain binding, GEO redirect and fallback routing, and the behavior of
partial flow updates when adding or removing offer associations.

## Planned next small commit

Implement Django administrative settings/Telegram binding database models and migrations **or** first SQLAlchemy Keitaro models, after reviewing the first pushed commit and CI. We will choose one contained scope together.
