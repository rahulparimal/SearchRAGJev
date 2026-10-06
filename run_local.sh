#!/usr/bin/env bash
# NOTE: superseded by scripts/run_local.sh (port checks, process groups, constraints). Kept for reference; prefer the scripts/ version.
# run_local.sh - Run RAGSearch Jev locally (without Docker Compose)
# Usage: ./run_local.sh [command]
# Commands: setup, start, stop, test, eval, clean

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
cd "$PROJECT_ROOT"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# Check if command exists
command_exists() { command -v "$1" &> /dev/null; }

# Generate secure secret
generate_secret() { openssl rand -base64 32 | tr -d '\n'; }

# Setup environment
setup_env() {
    log_info "Setting up environment..."
    
    if [[ ! -f .env ]]; then
        cp .env.example .env
        log_info "Created .env from .env.example"
    fi
    
    # Generate secure secrets if using defaults
    if grep -q "replace-with-a-long-random-secret" .env; then
        SECRET=$(generate_secret)
        # Use | as delimiter to avoid issues with special chars in secret
        sed -i.bak "s|replace-with-a-long-random-secret|$SECRET|" .env
        log_success "Generated secure APP_SECRET_KEY"
    fi
    
    if grep -q "change-this-on-first-run" .env; then
        ADMIN_PASS=$(openssl rand -base64 16 | tr -d '\n')
        sed -i.bak "s|change-this-on-first-run|$ADMIN_PASS|" .env
        log_success "Generated secure ADMIN_PASSWORD: $ADMIN_PASS"
        log_warn "Save this password!"
    fi
    
    # Update DATABASE_URL for local PostgreSQL
    if grep -q "postgresql+psycopg://ragsearch:change-me@postgres:5432/ragsearch" .env; then
        sed -i.bak 's|postgresql+psycopg://ragsearch:change-me@postgres:5432/ragsearch|postgresql+psycopg://ragsearch:change-me@localhost:5432/ragsearch|' .env
        log_info "Updated DATABASE_URL for local PostgreSQL"
    fi
    
    # Update QDRANT_URL for local
    if grep -q "http://qdrant:6333" .env; then
        sed -i.bak 's|http://qdrant:6333|http://localhost:6333|' .env
        log_info "Updated QDRANT_URL for local Qdrant"
    fi
    
    # Update REDIS_URL for local
    if grep -q "redis://redis:6379/0" .env; then
        sed -i.bak 's|redis://redis:6379/0|redis://localhost:6379/0|' .env
        log_info "Updated REDIS_URL for local Redis"
    fi
    
    log_warn "Review .env and update any remaining defaults (especially POSTGRES_PASSWORD)"
}

# Start infrastructure services (PostgreSQL, Qdrant, Redis)
start_infra() {
    log_info "Starting infrastructure services..."
    
    # Check if Docker is available
    if ! command_exists docker; then
        log_error "Docker not found. Please install Docker to run PostgreSQL, Qdrant, and Redis."
        exit 1
    fi
    
    if ! docker info &> /dev/null; then
        log_error "Docker daemon not running. Please start Docker."
        exit 1
    fi
    
    # Start only infrastructure
    docker compose up -d postgres qdrant redis
    
    log_info "Waiting for services to be healthy..."
    local max_attempts=60
    local attempt=0
    
    while [[ $attempt -lt $max_attempts ]]; do
        if docker compose ps postgres | grep -q "healthy" && \
           docker compose ps qdrant | grep -q "Up" && \
           docker compose ps redis | grep -q "Up"; then
            log_success "Infrastructure services are ready"
            break
        fi
        sleep 2
        ((attempt++))
    done
    
    if [[ $attempt -eq $max_attempts ]]; then
        log_error "Services did not become ready in time"
        docker compose logs --tail=50 postgres qdrant redis
        exit 1
    fi
}

# Stop infrastructure
stop_infra() {
    log_info "Stopping infrastructure services..."
    docker compose down
    log_success "Infrastructure stopped"
}

# Run backend
run_backend() {
    log_info "Starting backend API..."
    
    if [[ ! -d venv ]]; then
        log_error "Virtual environment not found. Run './run_local.sh setup' first."
        exit 1
    fi
    
    source venv/bin/activate
    cd backend
    
    # Validate APP_SECRET_KEY
    python -c "from security import validate_app_secret; import os; validate_app_secret(os.getenv('APP_SECRET_KEY',''))" || {
        log_error "APP_SECRET_KEY validation failed. Check .env"
        exit 1
    }
    
    log_success "Starting FastAPI on http://localhost:8000"
    log_info "API docs: http://localhost:8000/docs"
    uvicorn app:app --host 0.0.0.0 --port 8000
}

# Run Celery worker
run_worker() {
    log_info "Starting Celery worker..."
    
    if [[ ! -d venv ]]; then
        log_error "Virtual environment not found. Run './run_local.sh setup' first."
        exit 1
    fi
    
    source venv/bin/activate
    cd backend
    
    log_success "Starting Celery worker"
    celery -A tasks.celery worker --loglevel=INFO --pool=solo
}

# Run frontend
run_frontend() {
    log_info "Starting frontend..."
    
    cd frontend
    
    if [[ ! -d node_modules ]]; then
        log_info "Installing frontend dependencies..."
        npm install
    fi
    
    log_success "Starting Vite dev server on http://localhost:5173"
    npm run dev
}

# Run tests
run_tests() {
    log_info "Running unit tests..."
    
    if [[ ! -d venv ]]; then
        log_error "Virtual environment not found. Run './run_local.sh setup' first."
        exit 1
    fi
    
    source venv/bin/activate
    python -m unittest discover -s tests -v
}

# Run evaluation
run_eval() {
    local dataset="${1:-scripts/evaluation_template.jsonl}"
    local token="${RAGSEARCH_TOKEN:-}"
    
    if [[ ! -f "$dataset" ]]; then
        log_error "Dataset not found: $dataset"
        exit 1
    fi
    
    if [[ -z "$token" ]]; then
        log_error "RAGSEARCH_TOKEN not set. Login at http://localhost:5173 and get token from browser devtools (localStorage)."
        exit 1
    fi
    
    if [[ ! -d venv ]]; then
        log_error "Virtual environment not found. Run './run_local.sh setup' first."
        exit 1
    fi
    
    source venv/bin/activate
    python scripts/evaluate.py --dataset "$dataset" --api http://localhost:8000 --token "$token"
}

# Clean up
clean_all() {
    log_warn "This will remove virtual environment, node_modules, and Docker volumes!"
    read -p "Are you sure? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf venv
        rm -rf frontend/node_modules
        docker compose down -v --remove-orphans
        log_success "Cleanup complete"
    else
        log_info "Cleanup cancelled"
    fi
}

# Show help
show_help() {
    echo "Usage: $0 {setup|start|start-backend|start-worker|start-frontend|stop|test|eval|clean}"
    echo ""
    echo "Commands:"
    echo "  setup           - Create .env with secure secrets, install Python deps"
    echo "  start           - Start infrastructure + backend + worker + frontend (all in background)"
    echo "  start-backend   - Start only backend API (foreground)"
    echo "  start-worker    - Start only Celery worker (foreground)"
    echo "  start-frontend  - Start only frontend dev server (foreground)"
    echo "  stop            - Stop infrastructure services"
    echo "  test            - Run unit tests"
    echo "  eval [dataset]  - Run evaluation (requires RAGSEARCH_TOKEN)"
    echo "  clean           - Remove venv, node_modules, Docker volumes"
    echo ""
    echo "Typical workflow:"
    echo "  1. ./run_local.sh setup"
    echo "  2. ./run_local.sh start        # Starts everything"
    echo "  3. Open http://localhost:5173"
    echo "  4. Login with admin / <password from .env>"
    echo "  5. Upload documents, search, enjoy!"
}

# Main dispatcher
case "${1:-help}" in
    setup)
        setup_env
        log_info "Installing Python dependencies..."
        /usr/local/bin/python3.12 -m venv venv
        source venv/bin/activate
        pip install -r backend/requirements.txt
        log_success "Setup complete!"
        ;;
    start)
        setup_env
        start_infra
        log_info "Starting all services in background..."
        
        # Backend
        source venv/bin/activate
        cd "$PROJECT_ROOT/backend"
        nohup uvicorn app:app --host 0.0.0.0 --port 8000 > ../backend.log 2>&1 &
        BACKEND_PID=$!
        echo $BACKEND_PID > ../backend.pid
        log_success "Backend started (PID: $BACKEND_PID)"
        
        # Worker
        nohup celery -A tasks.celery worker --loglevel=INFO --pool=solo > ../worker.log 2>&1 &
        WORKER_PID=$!
        echo $WORKER_PID > ../worker.pid
        log_success "Worker started (PID: $WORKER_PID)"
        
        # Frontend
        cd "$PROJECT_ROOT/frontend"
        nohup npm run dev > ../frontend.log 2>&1 &
        FRONTEND_PID=$!
        echo $FRONTEND_PID > ../frontend.pid
        log_success "Frontend started (PID: $FRONTEND_PID)"
        
        log_success "All services started!"
        log_info "Frontend: http://localhost:5173"
        log_info "API: http://localhost:8000"
        log_info "API Docs: http://localhost:8000/docs"
        log_info "Logs: backend.log, worker.log, frontend.log"
        ;;
    start-backend)
        start_infra
        run_backend
        ;;
    start-worker)
        start_infra
        run_worker
        ;;
    start-frontend)
        run_frontend
        ;;
    stop)
        # Kill background processes
        for pid_file in backend.pid worker.pid frontend.pid; do
            if [[ -f "$pid_file" ]]; then
                kill $(cat "$pid_file") 2>/dev/null || true
                rm "$pid_file"
            fi
        done
        stop_infra
        ;;
    test)
        run_tests
        ;;
    eval)
        run_eval "${2:-}"
        ;;
    clean)
        clean_all
        ;;
    help|*)
        show_help
        ;;
esac