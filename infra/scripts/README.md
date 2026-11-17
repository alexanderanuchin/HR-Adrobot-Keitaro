# Adrobot — Infrastructure Scripts

Scripts for local development and Docker infrastructure management.

Requirements:
- Git Bash (Windows) or Bash (Linux/macOS)
- Docker Desktop / Docker Engine with Compose v2
- Configured local .env in the repository root

## Main commands

Run from the repository root:

    bash infra/scripts/dev.sh up
    bash infra/scripts/dev.sh rebuild
    bash infra/scripts/dev.sh migrate
    bash infra/scripts/dev.sh check
    bash infra/scripts/dev.sh test

The script resolves the project root automatically.

## Startup

`up`:
1. Validates Docker, Compose and .env.
2. Builds and starts PostgreSQL, Redis, FastAPI, Django and React.
3. Checks Django migration drift.
4. Backs up PostgreSQL.
5. Applies Django and Alembic migrations.
6. Checks schema drift and pending migrations.
7. Prints service addresses.

## Rebuilding

`rebuild` rebuilds the three core application images without build
cache, recreates changed application containers, runs migrations,
and performs configuration checks.

PostgreSQL data is retained in its Docker volume.

Optional Celery and Telegram services can be refreshed separately
using automation-up and bot-up.

## Migrations

Django migrations own Django administrative tables.

Alembic owns only SQLAlchemy kt_* tables.

The `migrate` command never automatically generates migration files.

If model/migration drift is detected, it stops for developer review.

## Backups

Before applying migrations, the script writes a PostgreSQL SQL dump to:

    var/backups/

The existing repository .gitignore excludes var/.

Do not commit database dumps or publish them as CI artifacts.

## Tests

`test` runs:
- Django system check and migration checks
- All Django tests
- FastAPI pytest tests
- React TypeScript typecheck
- React production build

## Optional services

    bash infra/scripts/dev.sh automation-up
    bash infra/scripts/dev.sh automation-down
    bash infra/scripts/dev.sh bot-up
    bash infra/scripts/dev.sh bot-down

The Telegram bot requires TELEGRAM_BOT_TOKEN in the local .env.

The Celery and Telegram services remain infrastructure skeletons
until the monitoring features are implemented.

## Diagnostics

    bash infra/scripts/dev.sh doctor
    bash infra/scripts/dev.sh status
    bash infra/scripts/dev.sh logs django
    bash infra/scripts/dev.sh logs fastapi

## Shutdown

    bash infra/scripts/dev.sh down

This stops services without removing PostgreSQL volumes.

The script intentionally does not provide a destructive
database reset command.
