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
  # DATABASE_URL / QDRANT_URL / REDIS_URL are set by docker-compose.yml (service hostnames) and override .env, so they are not checked here.
  [[ "${CORS_ORIGINS:-}" != "*" && -n "${CORS_ORIGINS:-}" ]] || { echo "FAIL: CORS_ORIGINS must be set explicitly (not * or empty)"; bad=1; }
  if git ls-files --error-unmatch .env.example >/dev/null 2>&1 && grep -qE 'apikey_|nvapi-|sk-|AKIA' .env.example; then echo "FAIL: API-key-like value found in tracked .env.example"; bad=1; fi
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
  # Qdrant snapshot API gives a consistent copy (tar of live storage can be torn). The qdrant image has no curl/wget, so use the api container's python.
  snap=$("${DC[@]}" exec -T api python -c "import urllib.request as u,json;print(json.load(u.urlopen(u.Request('http://qdrant:6333/collections/rag_chunks_v1/snapshots',method='POST'),timeout=300))['result']['name'])") || { echo "Qdrant snapshot failed"; exit 1; }
  "${DC[@]}" exec -T api python -c "import sys,urllib.request as u;sys.stdout.buffer.write(u.urlopen('http://qdrant:6333/collections/rag_chunks_v1/snapshots/$snap',timeout=300).read())" > "backups/qdrant-$ts-$snap"
  echo "Backups in ./backups ($ts)";;
*) echo "Usage: $0 {up|down|status|logs [svc]|backup}"; exit 1;;
esac
