#!/usr/bin/env bash
# Local dev runner: infra in Docker, API + worker + Vite on the host.
# Usage: scripts/run_local.sh {start|stop|status|logs}
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
RUN="$ROOT/.run"; mkdir -p "$RUN" uploads

[[ -f .env ]] || { echo "Missing .env (copy .env.example and fill in secrets)"; exit 1; }
[[ -d venv ]] || { echo "Missing venv: python3.12 -m venv venv && venv/bin/pip install -r backend/requirements.txt -c backend/constraints.txt"; exit 1; }
set -a; . ./.env; set +a
export UPLOAD_DIR="${UPLOAD_DIR:-$ROOT/uploads}"
export API_PORT="${API_PORT:-8000}" WEB_PORT="${WEB_PORT:-5173}"  # read by vite.config.ts too, so a non-default port never needs editing frontend files

wait_http() { for _ in $(seq 1 60); do curl -sf -m3 "$1" >/dev/null && return 0; sleep 2; done; echo "Timed out waiting for $1"; return 1; }
pid_alive() { [[ -f "$RUN/$1.pid" ]] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
check_port() { # name, port: fail if a process outside our recorded process groups holds it
  local name=$1 port=$2 p g mine
  for p in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null); do
    g=$(ps -o pgid= -p "$p" | tr -d ' '); mine=0
    for f in "$RUN"/*.pid; do [[ -f "$f" && "$(cat "$f")" == "$g" ]] && mine=1; done
    ((mine)) || { echo "Port $port needed by $name is held by PID $p not started by this script (stop it first, or see: lsof -nP -iTCP:$port)"; exit 1; }
  done
}
start_bg() { # name, workdir, cmd...  (own process group so stop can kill the whole tree, e.g. npm -> node)
  local name=$1 dir=$2; shift 2
  if pid_alive "$name"; then echo "$name already running"; return; fi
  (cd "$dir" && nohup python3 -c 'import os,sys;os.setsid();os.execvp(sys.argv[1],sys.argv[1:])' "$@" > "$RUN/$name.log" 2>&1 & echo $! > "$RUN/$name.pid")
}

case "${1:-}" in
start)
  docker info >/dev/null 2>&1 || { echo "Docker is not running"; exit 1; }
  docker compose up -d postgres qdrant redis
  source venv/bin/activate
  # Port conflicts with other local containers are the most common failure.
  for _ in $(seq 1 30); do curl -sf -m3 "$QDRANT_URL/" | grep -q '"version"' && break; sleep 1; done
  curl -sf -m3 "$QDRANT_URL/" | grep -q '"version"' || { echo "Qdrant not reachable at $QDRANT_URL"; exit 1; }
  pid_alive api || check_port api "$API_PORT"; pid_alive web || check_port web "$WEB_PORT"
  start_bg api backend uvicorn app:app --host 0.0.0.0 --port "$API_PORT"
  start_bg worker backend celery -A tasks.celery worker --loglevel=INFO --pool=solo   # solo: prefork breaks on macOS
  [[ -d frontend/node_modules ]] || (cd frontend && npm install)
  start_bg web frontend npm run dev
  wait_http "http://localhost:$API_PORT/health"
  wait_http "http://localhost:$WEB_PORT/"
  echo "API  http://localhost:$API_PORT/docs"; echo "UI   http://localhost:$WEB_PORT  (login: $ADMIN_USERNAME)"; echo "Logs $RUN/*.log";;
stop)
  for n in web worker api; do
    [[ -f "$RUN/$n.pid" ]] && { kill -TERM -- "-$(cat "$RUN/$n.pid")" 2>/dev/null || kill "$(cat "$RUN/$n.pid")" 2>/dev/null || true; rm -f "$RUN/$n.pid"; }
  done
  docker compose stop postgres qdrant redis;;
status)
  curl -sf "http://localhost:$API_PORT/health" && echo; curl -sfI "http://localhost:$WEB_PORT/" | head -1; docker compose ps;;
logs) tail -f "$RUN"/*.log;;
*) echo "Usage: $0 {start|stop|status|logs}"; exit 1;;
esac
