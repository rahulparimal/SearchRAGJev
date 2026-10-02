#!/usr/bin/env bash
# First-boot initialization for the all-in-one image, then hands off to supervisord (which manages postgres/redis/qdrant/
# api/worker/nginx as siblings). Runs as root (needed to initdb and to gosu into postgres/redis); the API/worker/nginx/qdrant
# processes themselves run as whatever supervisord.conf says (root, for simplicity in this bundled image -- see Dockerfile
# comment). All persistent state lives under /data so a single volume mount is all a caller needs.
set -euo pipefail

mkdir -p /data/postgres /data/qdrant /data/redis /data/uploads /data/secrets
chown postgres:postgres /data/postgres
chown redis:redis /data/redis
chmod 700 /data/postgres

# Secrets generated once and persisted to /data/secrets, so a plain `docker restart` (or image upgrade against the same
# volume) keeps existing sessions valid and existing encrypted admin settings decryptable -- regenerating APP_SECRET_KEY on
# every boot would silently break both. A caller-supplied env var always wins and is never overwritten on disk.
persisted_or_generated() {
  local file="$1" env_name="$2"
  if [[ -n "${!env_name:-}" ]]; then printf '%s' "${!env_name}"; return; fi
  if [[ -s "$file" ]]; then cat "$file"; return; fi
  python3 -c "import secrets;print(secrets.token_urlsafe(32))" > "$file"
  chmod 600 "$file"
  cat "$file"
}
admin_password_is_new=0; [[ -s /data/secrets/admin_password || -n "${ADMIN_PASSWORD:-}" ]] || admin_password_is_new=1
export APP_SECRET_KEY POSTGRES_PASSWORD ADMIN_PASSWORD
APP_SECRET_KEY="$(persisted_or_generated /data/secrets/app_secret_key APP_SECRET_KEY)"
POSTGRES_PASSWORD="$(persisted_or_generated /data/secrets/postgres_password POSTGRES_PASSWORD)"
ADMIN_PASSWORD="$(persisted_or_generated /data/secrets/admin_password ADMIN_PASSWORD)"

export POSTGRES_DB="${POSTGRES_DB:-ragsearch}"
export POSTGRES_USER="${POSTGRES_USER:-ragsearch}"
export ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}}"
export QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export UPLOAD_DIR="${UPLOAD_DIR:-/data/uploads}"
export CORS_ORIGINS="${CORS_ORIGINS:-}"

if [[ ! -s /data/postgres/PG_VERSION ]]; then
  echo "[entrypoint] first boot: initializing Postgres data directory"
  gosu postgres initdb -D /data/postgres --auth=trust --username=postgres >/data/initdb.log 2>&1   # log OUTSIDE the data dir: a log file written inside /data/postgres before initdb runs (shell redirection creates it first) makes initdb refuse with "directory exists but is not empty"
  gosu postgres pg_ctl -D /data/postgres -o "-c listen_addresses=''" -w start
  gosu postgres psql -v ON_ERROR_STOP=1 --username=postgres <<-SQL
    CREATE USER "${POSTGRES_USER}" WITH PASSWORD '${POSTGRES_PASSWORD}';
    CREATE DATABASE "${POSTGRES_DB}" OWNER "${POSTGRES_USER}";
SQL
  gosu postgres pg_ctl -D /data/postgres -m fast -w stop
  echo "[entrypoint] Postgres initialized"
fi

if [[ "$admin_password_is_new" == 1 ]]; then
  echo "[entrypoint] generated admin login: ${ADMIN_USERNAME} / ${ADMIN_PASSWORD}  (persisted to /data/secrets/admin_password; this is the only time it is printed)"
fi

exec "$@"
