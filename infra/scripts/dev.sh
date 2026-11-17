#!/usr/bin/env bash
# Adrobot: local Docker lifecycle management for Git Bash / Linux / macOS.

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

readonly CORE_SERVICES=(postgres redis fastapi django frontend)
readonly APP_IMAGES=(django fastapi frontend)

log() {
    printf '\n[adrobot] %s\n' "$*"
}

fail() {
    printf '\n[adrobot] ERROR: %s\n' "$*" >&2
    exit 1
}

on_error() {
    local exit_code=$?
    printf '\n[adrobot] Failed at line %s (exit %s).\n' \
        "$1" "$exit_code" >&2
    printf '[adrobot] Check logs: bash infra/scripts/dev.sh logs\n' >&2
    exit "$exit_code"
}

trap 'on_error "$LINENO"' ERR

need_docker() {
    command -v docker >/dev/null 2>&1 ||
        fail "Docker CLI not found. Install or start Docker Desktop."

    docker compose version >/dev/null 2>&1 ||
        fail "Docker Compose v2 is unavailable."

    docker info >/dev/null 2>&1 ||
        fail "Docker daemon is unavailable. Start Docker Desktop."
}

need_env() {
    [[ -f .env ]] ||
        fail "Missing .env. Copy .env.example to .env and configure local secrets."
}

validate() {
    need_docker
    need_env
    docker compose config --quiet
}

start_core() {
    local build_flag="${1:-}"

    if [[ "$build_flag" == "build" ]]; then
        log "Building and starting core services"

        docker compose up \
            -d \
            --build \
            --wait \
            --wait-timeout 180 \
            "${CORE_SERVICES[@]}"
    else
        log "Starting core services"

        docker compose up \
            -d \
            --wait \
            --wait-timeout 180 \
            "${CORE_SERVICES[@]}"
    fi
}

backup_database() {
    local backup_dir="var/backups"
    local backup_path

    mkdir -p "$backup_dir"

    backup_path="$backup_dir/adrobot_$(date -u +%Y%m%dT%H%M%SZ)_${RANDOM}.sql"

    log "Backing up PostgreSQL before migrations"

    if ! docker compose exec -T postgres sh -c \
        'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
        > "$backup_path"; then

        rm -f -- "$backup_path"

        fail "PostgreSQL backup failed; migrations were not started"
    fi

    log "Backup saved: $backup_path"
}

apply_migrations() {
    log "Checking Django model/migration drift"

    docker compose exec -T django \
        python manage.py makemigrations --check --dry-run

    backup_database

    log "Applying Django migrations"

    docker compose exec -T django \
        python manage.py migrate --noinput

    log "Applying SQLAlchemy / Alembic migrations"

    docker compose exec -T fastapi \
        alembic upgrade head

    log "Checking for unapplied Django migrations"

    docker compose exec -T django \
        python manage.py migrate --check --noinput

    log "Checking SQLAlchemy migration drift"

    docker compose exec -T fastapi \
        alembic check
}

check_project() {
    log "Validating Docker Compose"

    docker compose config --quiet

    log "Checking Django configuration"

    docker compose exec -T django \
        python manage.py check

    log "Checking Django model/migration drift"

    docker compose exec -T django \
        python manage.py makemigrations --check --dry-run

    log "Checking unapplied Django migrations"

    docker compose exec -T django \
        python manage.py migrate --check --noinput

    log "Checking Alembic migration drift (kt_* tables)"

    docker compose exec -T fastapi \
        alembic check

    log "All configuration and migration checks passed"
}

show_addresses() {
    printf '\n'
    printf 'UI:           http://localhost:5173\n'
    printf 'Django Admin: http://localhost:8001/admin/\n'
    printf 'Django API:   http://localhost:8001/api/v1/health/\n'
    printf 'PostgreSQL:   127.0.0.1:55432\n'
    printf 'Docker DB:    postgres:5432\n'
}

usage() {
    cat <<'HELP'
ADROBOT — Docker project manager

Usage:
    bash infra/scripts/dev.sh COMMAND

Commands:
    up                  Build/start project, migrate and check
    rebuild             Rebuild application images, migrate and check
    migrate             Back up DB and apply Django/Alembic migrations
    check               Check configuration and migrations without changes
    test                Run backend tests, React typecheck and build
    backup              Back up PostgreSQL to var/backups/
    status              Show running containers
    logs [service]      Follow last 100 log lines
    admin               Create Django superuser
    automation-up       Start Celery worker and scheduler
    automation-down     Stop Celery worker and scheduler
    bot-up              Start Telegram bot (requires token)
    bot-down            Stop Telegram bot
    down                Stop services without deleting database volumes
    doctor              Verify Docker, Compose and .env
    help                Show this message

Safety:
    - No command deletes PostgreSQL volumes.
    - Migrations are checked before application.
    - Database backup is created before migrations.
    - .env is never committed by this script.
HELP
}

command_name="${1:-help}"

case "$command_name" in

    up|start)
        validate
        start_core build
        apply_migrations
        check_project

        log "Core services are ready"
        docker compose ps
        show_addresses
        ;;

    rebuild)
        validate

        log "Rebuilding application images without Docker build cache"

        docker compose build \
            --no-cache \
            "${APP_IMAGES[@]}"

        start_core
        apply_migrations
        check_project

        log "Rebuild finished"
        docker compose ps
        show_addresses
        ;;

    migrate)
        validate

        log "Starting dependencies for migrations"

        docker compose up \
            -d \
            --wait \
            --wait-timeout 180 \
            postgres redis fastapi django

        apply_migrations
        ;;

    check)
        validate
        check_project
        ;;

    test)
        validate
        check_project

        log "Running Django tests"

        docker compose exec -T django \
            python manage.py test --noinput

        log "Running FastAPI tests"

        docker compose exec -T fastapi \
            python -m pytest -q

        log "Checking React types"

        docker compose exec -T frontend \
            npm run typecheck

        log "Building React"

        docker compose exec -T frontend \
            npm run build

        log "All tests and builds passed"
        ;;

    backup)
        validate

        docker compose up \
            -d \
            --wait \
            --wait-timeout 180 \
            postgres

        backup_database
        ;;

    status|ps)
        need_docker

        docker compose \
            --profile automation \
            --profile telegram \
            ps
        ;;

    logs)
        need_docker
        shift

        docker compose \
            --profile automation \
            --profile telegram \
            logs \
            --follow \
            --tail=100 \
            "$@"
        ;;

    admin)
        validate

        docker compose exec django \
            python manage.py createsuperuser
        ;;

    automation-up)
        validate

        docker compose \
            --profile automation \
            up -d --build \
            celery_worker celery_beat
        ;;

    automation-down)
        need_docker

        docker compose \
            --profile automation \
            stop celery_worker celery_beat
        ;;

    bot-up)
        validate

        if ! grep -qE '^TELEGRAM_BOT_TOKEN=.+$' .env; then
            fail "TELEGRAM_BOT_TOKEN is empty in .env"
        fi

        docker compose \
            --profile telegram \
            up -d --build telegram_bot
        ;;

    bot-down)
        need_docker

        docker compose \
            --profile telegram \
            stop telegram_bot
        ;;

    down|stop)
        need_docker

        log "Stopping containers without removing PostgreSQL volumes"

        docker compose \
            --profile automation \
            --profile telegram \
            down
        ;;

    doctor)
        validate

        log "Docker, Compose and .env configuration are available"

        docker compose version

        docker compose \
            --profile automation \
            --profile telegram \
            ps
        ;;

    help|-h|--help)
        usage
        ;;

    *)
        usage >&2
        fail "Unknown command: $command_name"
        ;;

esac
