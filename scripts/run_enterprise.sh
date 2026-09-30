#!/usr/bin/env bash
# Enterprise/production-style runner (Docker Compose + hardened overlay).
# Usage: scripts/run_enterprise.sh {up|down|status|logs|backup}
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
DC=(docker compose -f docker-compose.yml -f docker-compose.enterprise.yml)

preflight() {
  [[ -f .env ]] || { echo "Missing .env"; exit 1; }
  set -a; . ./.env; set +a
  local bad=0
  chk() { [[ -n "${!1:-}" ]] && [[ ! "${!1}" =~ (change-me|change-this|replace-with|local-dev) ]] || { echo "FAIL: $1 is empty or a placeholder"; bad=1; }; }
  for v in POSTGRES_PASSWORD APP_SECRET_KEY ADMIN_PASSWORD; do chk "$v"; done
  [[ ${#APP_SECRET_KEY} -ge 32 ]] || { echo "FAIL: APP_SECRET_KEY must be >= 32 chars"; bad=1; }
  # Inside Compose the services are addressed by service name, not localhost.
  [[ "$DATABASE_URL" == *"@postgres:5432/"* ]] || { echo "FAIL: DATABASE_URL must use host postgres:5432"; bad=1; }
  [[ "$QDRANT_URL" == "http://qdrant:6333" ]]  || { echo "FAIL: QDRANT_URL must be http://qdrant:6333"; bad=1; }
  [[ "$REDIS_URL"  == redis://redis:6379/* ]]  || { echo "FAIL: REDIS_URL must use host redis:6379"; bad=1; }
  [[ "$CORS_ORIGINS" != "*" ]] || { echo "FAIL: CORS_ORIGINS must be explicit"; bad=1; }
  [[ -z "$(git ls-files --error-unmatch .env.example 2>/dev/null | xargs -r grep -lE 'apikey_|nvapi-' 2>/dev/null)" ]] || { echo "FAIL: real API keys found in tracked .env.example"; bad=1; }
  ((bad==0)) || exit 1
}

case "${1:-}" in
up)
  preflight
  "${DC[@]}" up -d --build
  echo "Waiting for API health..."
  for _ in $(seq 1 60); do [[ "$("${DC[@]}" ps api --format '{{.Health}}')" == healthy ]] && { echo "Stack healthy: http://localhost:${WEB_PORT:-80}"; exit 0; }; sleep 5; done
  "${DC[@]}" logs --tail=80 api; exit 1;;
down)   "${DC[@]}" down;;
status) "${DC[@]}" ps;;
logs)   "${DC[@]}" logs -f --tail=100 "${@:2}";;
backup)
  set -a; . ./.env; set +a; mkdir -p backups; ts=$(date +%Y%m%d-%H%M%S)
  "${DC[@]}" exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "backups/pg-$ts.sql.gz"
  "${DC[@]}" exec -T qdrant sh -c 'tar czf - -C /qdrant storage' > "backups/qdrant-$ts.tgz"
  echo "Backups in ./backups ($ts)";;
*) echo "Usage: $0 {up|down|status|logs [svc]|backup}"; exit 1;;
esac
