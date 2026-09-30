# RAGSearch Jev — Runbook

How to run the solution locally and in an enterprise-style deployment, plus the issues hit while bringing it up on macOS (2026-09-30).

## 1. Running locally (macOS/Linux dev)

Prerequisites: Docker, Python 3.12, Node 20+.

```bash
cp .env.example .env            # fill in secrets; keep URLs pointing at localhost
python3.12 -m venv venv
venv/bin/pip install -r backend/requirements.txt 'numpy<2' 'transformers==4.46.3' 'scipy<1.14' 'scikit-learn<1.6'
scripts/run_local.sh start      # infra (Docker) + API + Celery worker + Vite
scripts/run_local.sh status | logs | stop
```

| Service | URL |
|---|---|
| UI | http://localhost:5173 |
| API / docs | http://localhost:8000 / `/docs` |
| Postgres / Qdrant / Redis | host ports 5433 / 6335 / 6380 |

Local `.env` values: `DATABASE_URL=...@localhost:5433/...`, `QDRANT_URL=http://localhost:6335`, `REDIS_URL=redis://localhost:6380/0`, `UPLOAD_DIR=<repo>/uploads`.

## 2. Running at enterprise level

Everything runs in Docker Compose with the hardened overlay [docker-compose.enterprise.yml](../docker-compose.enterprise.yml): infra not published to the host, restart policies, API healthcheck, 2 workers, web tier on `WEB_PORT` (default 80).

```bash
scripts/run_enterprise.sh up        # preflight checks, build, start, wait for healthy
scripts/run_enterprise.sh status | logs [svc] | backup | down
```

Inside Compose, `.env` must use service names: `DATABASE_URL=...@postgres:5432/...`, `QDRANT_URL=http://qdrant:6333`, `REDIS_URL=redis://redis:6379/0`. The script refuses to start on placeholder secrets, short `APP_SECRET_KEY`, wildcard CORS, or real API keys in the tracked `.env.example`.

**Not covered by the scripts (needed for a real enterprise rollout):** TLS termination (put a reverse proxy/LB in front of `web`), a secrets manager instead of `.env`, managed/HA Postgres and Qdrant, scheduled backups with restore tests, log/metrics shipping, SSO. `backup` is a manual dump only and has not been restore-tested.

## 3. Issues encountered

| # | Severity | Symptom | Root cause | Fix |
|---|---|---|---|---|
| 1 | High | Backend "started" but nothing listened on 8000 | `app.py` has no server entry point, so `python app.py` exits | Run `uvicorn app:app` (updated `run_local.sh`) |
| 2 | High | `OSError: Read-only file system: '/app'` | `.env` isn't auto-loaded; `UPLOAD_DIR` defaulted to `/app/uploads` | Export `.env` before launch (scripts do) |
| 3 | High | `_ARRAY_API not found` warnings, then import failures | numpy 2.x installed with torch 2.2.2 | Pin `numpy<2` |
| 4 | High | `cannot import name 'GenerationMixin'` | transformers 4.57 too new for torch 2.2.2 | Pin `transformers==4.46.3` |
| 5 | High | `numpy has no attribute 'long'` | scipy 1.18 / scikit-learn 1.9 need numpy 2 | Pin `scipy<1.14`, `scikit-learn<1.6` |
| 6 | High | Startup failed: `Qdrant ... 404` | Another project's Qdrant v1.7.4 already owned host port 6333 | Map this project's Qdrant to 6335 |
| 7 | Medium | Redis port 6379 "already allocated" | Another Redis container on 6379 | Map this project's Redis to 6380 |
| 8 | High | UI login got `404 Not Found` | API routes have no `/api` prefix; nginx strips it, Vite dev proxy didn't | Added rewrite in `vite.config.ts` |
| 9 | High | Indexing task failed: `not enough values to unpack` | Celery prefork pool breaks with spawn on macOS | `--pool=solo` locally (Linux containers keep prefork) |
| 10 | Medium | `sed -i` edits failed | BSD `sed` needs `-i ''` | Use Python/portable edits |
| 11 | **Security** | Live-looking API keys, DB password and app secret in tracked `.env.example` | Real values written into the template | **Not yet fixed**: move to `.env`, restore placeholders, rotate keys if pushed |
| 12 | Low | `run_local.sh start` mutates `.env` via `setup_env` and assumes default ports | Script design | New `scripts/run_local.sh` doesn't touch `.env` |
| 13 | Low | `requirements.txt` lacks the pins from #3–5 | Unpinned transitive deps | **Not yet fixed**: add pins or a constraints file |

## 4. Verified

Local stack: `/health` ok; UI served on 5173; login via `/api/auth/login`; upload → Celery indexing (`ready`) → search returned the test chunk. The new `scripts/run_local.sh` and `scripts/run_enterprise.sh` were only syntax-checked (and the overlay validated with `docker compose config`); they have **not** been run end to end.

## 5. Agent mode, BM25 and ingestion

- **Modes** (`POST /search`, field `mode`; UI chips under the search box): `agent` (default, auto-route), `local`, `web`. The response includes `route` and `route_reason`.
- **Routing** ([backend/agent.py](../backend/agent.py)): Jev Choice decides local vs web. A "web" vote is overridden to local when the best permitted local chunk contains at least 60% of the query's content terms. Without a Jev key, the same coverage rule decides on its own.
- **BM25** ([backend/bm25.py](../backend/bm25.py), ported from Dharma `bm25.js`): a third leg in local RRF fusion (beside Qdrant and Postgres FTS) and the ranker for web results. Local BM25 is filtered by ACL before fusion, rerank, Jev or LLM.
- **Web search** ([backend/web_search.py](../backend/web_search.py)): DuckDuckGo via `ddgs`. Only the query text leaves the system. Results are untrusted and cited as `[S#]` with the same grounding checks. Set `WEB_SEARCH_ENABLED=false` to force local-only.
- **Ingestion** ([backend/ingestion.py](../backend/ingestion.py)): one pipeline (extract, Jev chunk profile, chunk, embed, Qdrant, Postgres), idempotent on re-run. Used by the Celery task, the admin upload (Admin tab, PDF/DOCX/TXT/MD/HTML) and the CLI.
- **Samples:** `python scripts/ingest_samples.py [paths]` loads `docs/samples/` with no Celery needed (run with `.env` exported).
