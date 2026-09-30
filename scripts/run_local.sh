#!/usr/bin/env bash
# Local dev runner: infra in Docker, API + worker + Vite on the host.
# Usage: scripts/run_local.sh {start|stop|status|logs}
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
RUN="$ROOT/.run"; mkdir -p "$RUN" uploads

[[ -f .env ]] || { echo "Missing .env (copy .env.example and fill in secrets)"; exit 1; }
[[ -d venv ]] || { echo "Missing venv: python3.12 -m venv venv && venv/bin/pip install -r backend/requirements.txt 'numpy<2' 'transformers==4.46.3' 'scipy<1.14' 'scikit-learn<1.6'"; exit 1; }
set -a; . ./.env; set +a
export UPLOAD_DIR="${UPLOAD_DIR:-$ROOT/uploads}"

wait_http() { for _ in $(seq 1 60); do curl -sf -m3 "$1" >/dev/null && return 0; sleep 2; done; echo "Timed out waiting for $1"; return 1; }
start_bg() { # name, workdir, cmd...
  local name=$1 dir=$2; shift 2
  if [[ -f "$RUN/$name.pid" ]] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then echo "$name already running"; return; fi
  (cd "$dir" && nohup "$@" > "$RUN/$name.log" 2>&1 & echo $! > "$RUN/$name.pid")
}

case "${1:-}" in
start)
  docker info >/dev/null 2>&1 || { echo "Docker is not running"; exit 1; }
  docker compose up -d postgres qdrant redis
  source venv/bin/activate
  # Port conflicts with other local containers are the most common failure.
  curl -sf -m3 "$QDRANT_URL/" | grep -q '"version"' || { echo "Qdrant not reachable at $QDRANT_URL"; exit 1; }
  start_bg api backend uvicorn app:app --host 0.0.0.0 --port 8000
  start_bg worker backend celery -A tasks.celery worker --loglevel=INFO --pool=solo   # solo: prefork breaks on macOS
  [[ -d frontend/node_modules ]] || (cd frontend && npm install)
  start_bg web frontend npm run dev
  wait_http http://localhost:8000/health
  wait_http http://localhost:5173/
  echo "API  http://localhost:8000/docs"; echo "UI   http://localhost:5173  (login: $ADMIN_USERNAME)"; echo "Logs $RUN/*.log";;
stop)
  for n in web worker api; do
    [[ -f "$RUN/$n.pid" ]] && { pkill -P "$(cat "$RUN/$n.pid")" 2>/dev/null || true; kill "$(cat "$RUN/$n.pid")" 2>/dev/null || true; rm -f "$RUN/$n.pid"; }
  done
  docker compose stop postgres qdrant redis;;
status)
  curl -sf http://localhost:8000/health && echo; curl -sfI http://localhost:5173/ | head -1; docker compose ps;;
logs) tail -f "$RUN"/*.log;;
*) echo "Usage: $0 {start|stop|status|logs}"; exit 1;;
esac
