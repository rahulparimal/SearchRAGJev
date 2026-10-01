# RAGSearch Jev — Runbook

How to run the solution locally and in an enterprise-style deployment, plus the issues hit while bringing it up on macOS (2026-09-30).

## 0. One command (Docker only)

```bash
./rag up        # .env with generated secrets (mode 0600, never printed), free ports, build, health-gated start, sample seed
./rag doctor    # pass/warn/fail per check with fix hints (Docker, compose v2, arch, disk, ports, .env, containers, /health,
                # Qdrant dimension, model cache, Redis, Jev, LLM, web providers + breaker state, smoke /search with timings)
./rag status | logs [svc] | seed [paths] | eval [args] | down | reset | dev
```

- `.env` is created from `.env.example` only when missing. An existing `.env` is never overwritten: missing keys are appended (names listed, values not shown) and only busy `*_PORT` lines are moved (`CORS_ORIGINS` follows `WEB_PORT`). Tracked files are never modified. Edits use exact line replacement in Python (no `sed -i`, so macOS and Linux behave the same).
- Ports: `WEB_PORT`, `API_PORT`, `PG_PORT`, `QDRANT_PORT`, `REDIS_PORT` (defaults 5173, 8000, 5433, 6335, 6380). Busy ports move to the next free one and the choice is saved in `.env`. Infra and API publish on 127.0.0.1 only. A second `./rag up` keeps the ports of the running stack.
- `./rag up --check` reports what would change and validates the compose file without starting anything. If only postgres/qdrant/redis are running (dev mode), `./rag up` refuses unless `--force`, because it would recreate them on new ports.
- The embedding model is baked into the api/worker image (`HF_HOME=/opt/models`), so the first query downloads nothing. The API warms the model at startup; `/health` reports `embedder: warming|ok` (informational).
- Optional SearXNG: `COMPOSE_PROFILES=websearch ./rag up`, then set `WEB_PROVIDERS=searxng,ddgs` and `SEARXNG_URL=http://searxng:8080` (config in `docker/searxng/settings.yml`).

## 1. Running locally (macOS/Linux dev)

Prerequisites: Docker, Python 3.12, Node 20+.

```bash
cp .env.example .env            # fill in secrets; keep URLs pointing at localhost
python3.12 -m venv venv
venv/bin/pip install -r backend/requirements.txt -c backend/constraints.txt
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
| 11 | **Security** | Live API keys, DB password and app secret were written into the working-tree `.env.example` (never committed) | Real values written into the template | **Fixed in the tree**: placeholders restored, `.env` and related files ignored. **User action required: rotate the TypeSafe/LLM keys, DB password and APP_SECRET_KEY**, because the values sat in a world-readable template and may have been copied or backed up (`.env.bak`) |
| 12 | Low | `run_local.sh start` mutates `.env` via `setup_env` and assumes default ports | Script design | New `scripts/run_local.sh` doesn't touch `.env` |
| 13 | Low | `requirements.txt` lacks the pins from #3–5 | Unpinned transitive deps | **Fixed**: `backend/constraints.txt` (use `-c constraints.txt`; the Dockerfile does) |

## 4. Verified

Local stack: `/health` ok; UI served on 5173; login via `/api/auth/login`; upload → Celery indexing (`ready`) → search returned the test chunk. The new `scripts/run_local.sh` and `scripts/run_enterprise.sh` were only syntax-checked (and the overlay validated with `docker compose config`); they have **not** been run end to end.

## 5. Agent mode, BM25 and ingestion

- **Modes** (`POST /search`, field `mode`; UI chips under the search box): `agent` (default, auto-route), `local`, `web`. The response includes `route` and `route_reason`.
- **Routing** ([backend/agent.py](../backend/agent.py)): Jev Choice decides local vs web. A "web" vote is overridden to local when the best permitted local chunk contains at least 60% of the query's content terms. Without a Jev key, the same coverage rule decides on its own.
- **BM25** ([backend/bm25.py](../backend/bm25.py), ported from Dharma `bm25.js`): a third leg in local RRF fusion (beside Qdrant and Postgres FTS) and the ranker for web results. Local BM25 is filtered by ACL before fusion, rerank, Jev or LLM.
- **Web search** ([backend/web_search.py](../backend/web_search.py)): an ordered provider chain (`WEB_PROVIDERS`; optional Brave Search API via `BRAVE_API_KEY`, optional SearXNG via `SEARXNG_URL`, then `ddgs` with **pinned engines**, default `yahoo,bing`, never ddgs `auto`). Only the query text leaves the system, to the providers named in the UI, the admin console and each response's `notice`. Web hits are never embedded or vector-searched: BM25 over title and snippet, then a Jev rerank. Hedged requests (next provider after 700 ms), a circuit breaker (3 failures in 60 s open it for 120 s), a 3 s hard deadline and a 15 minute cache keyed by a hash of the query. Results are untrusted text. Set `WEB_SEARCH_ENABLED=false` to force local-only.
- **Ingestion** ([backend/ingestion.py](../backend/ingestion.py)): one pipeline (extract, Jev chunk profile, chunk, embed, Qdrant, Postgres), idempotent on re-run. Used by the Celery task, the admin upload (Admin tab, PDF/DOCX/TXT/MD/HTML) and the CLI.
- **Samples:** `python scripts/ingest_samples.py [paths]` loads `docs/samples/` with no Celery needed (run with `.env` exported).

## 5b. AI Overview and search timings

- `POST /search` never produces prose and returns `timings`, `degraded_stages`, `search_id`, `overview: {eligible, reason}` and a `Server-Timing` header. The UI shows results first, then calls `POST /search/overview` (SSE) only when the **AI Overview** toggle (off on every page load) is on, the query has more than 3 words, and Jev judges it a question or specific lookup (without a Jev key: a wh-word/`?` heuristic; web results then always need a Jev key).
- The overview reads only the evidence stored under `(search_id, username)` for 5 minutes, re-validates local document ACLs right before the LLM call, wraps sources in per-request nonce delimiters with look-alike markers stripped, validates citations, and releases text only after every sentence passes a Jev grounding check. Failures arrive as a `withheld` event (`insufficient_evidence`, `unsupported_claim`, `provider_unavailable`, `ineligible`, `expired`); results are never affected.
- Model: `OVERVIEW_LLM_*` (falls back to `LLM_*`). Use a fast, non-reasoning model; `OVERVIEW_EXTRA_BODY` carries provider-specific "disable reasoning" parameters. Pick by measurement (`scripts/evaluate.py --overview`).
- The UI "Timings" toggle shows per-stage milliseconds (`plan`, `bm25`, `fts`, `dense`, `web_fetch`/`web_cache`, `rerank`, `total`). nginx must not buffer `/api/search/overview` (configured in `frontend/nginx.conf`).

## 6. Web search toggle

Searches are local by default. The search screen has a **Web search** toggle (off on every page load). With it off, every query stays on your documents. With it on, agent mode may route generic questions to the web (query text only). The toggle only works when the deployment allows web search (`WEB_SEARCH_ENABLED=true` or the admin setting); otherwise `/search` returns 403 for `web:true`. API: `POST /search {"query":"...","web":true}`; there is no admin bypass.

## 7. Operations notes (QA fix round, 2026-10-01)

- **Logs are structured** (JSON lines on stderr, `LOG_LEVEL`, `LOG_FORMAT=text` for plain text). Every swallowed failure is logged without query text, document text or secrets: `rag.jev` (`jev call failed reason=timeout|http_500|... breaker=open`), `rag.search` (`search leg bm25 failed`), `rag.web`, `rag.llm`, `rag.overview`. An outage that used to be invisible now shows up as repeated `jev call failed` lines, `degraded_stages` containing `jev_unavailable`/`jev_rerank_unavailable`, and a failing `./rag doctor`.
- **Jev client**: one pooled keep-alive client (`JEV_POOL_MAX`, default 128; pre-warmed at start). Deadlines come from httpx timeouts; an in-flight request is never cancelled (cancelling connecting requests wedged the old 32-slot pool). After 5 consecutive timeouts the client is rebuilt; 5 failures in 10 s open a circuit breaker for 10 s (`/admin/config` shows `jev_breaker`; `./rag doctor` fails while it is open).
- **Rerank**: batched Noul (up to 12 candidates per Jev request (compact ~3.5k-char state), partial answers accepted, per-(model, query, chunk, ACL scope) score cache, failed attempts negatively cached for 30 s). `JEV_RERANK_STYLE=noul_parallel` restores one request per candidate, `score_batch` the legacy single sync request.
- **Overview**: runs on its own async httpx pool (no worker threads), at most `OVERVIEW_MAX_CONCURRENT` (8) per API process (more get `withheld: busy`), one retry on timeout/429/5xx (Retry-After honoured) inside `OVERVIEW_TIMEOUT_S`. `withheld` reasons: `insufficient_evidence`, `unsupported_claim` (the lead sentence failed grounding), `provider_unavailable` (the LLM), `jev_unavailable` (the verification service), `busy`, `ineligible`, `expired`. A bullet that fails grounding is dropped; the rest is shown. Grounding threshold `OVERVIEW_GROUNDED_MIN` (0.6, calibrated, see SEARCH_REDESIGN section 13). Evidence is stored under `search_id` as ids only (text re-read from the database with an ACL re-check), sealed with Fernet, TTL 120 s; cached overviews are sealed too (TTL 300 s) and keyed by provider endpoint and model.
- **Provider URLs** (`JEV_BASE_URL`, `LLM_BASE_URL`, `OVERVIEW_LLM_BASE_URL`, `SEARXNG_URL`): http/https only, no credentials, no link-local/metadata addresses; optional `PROVIDER_URL_ALLOWLIST` (hosts or `.suffix`, comma-separated). Changing a URL's host in the admin console requires entering its API key again. The LLM key is never sent to a different overview host.
- **Admin API**: `GET /admin/users`, `DELETE /admin/users/{username}` (admin only; not yourself, not the last admin). `POST /search` accepts `rerank` and `embed_cache` overrides for administrators only (used by `scripts/evaluate.py`; no global setting is flipped).
- **Doctor**: `./rag doctor` now FAILS when the smoke search is degraded (reranker fallback, `degraded_stages`, failed Jev requests) even though the direct Jev probe is fine, and when the Jev breaker is open. The smoke web query is opt-in: `./rag doctor --web` (it sends one real query to the web providers).
- **Docker**: `scripts/` is mounted read-only into the api container (the build context is `./backend`), SearXNG is pinned to a tag, `frontend/.dockerignore` keeps `node_modules` out of the build.
- **Tools**: `scripts/overview_bench.py` (overview success/latency per model + grounding calibration), `scripts/tune_rerank.py` (dev/holdout blend tuning), `scripts/evaluate.py --delete-users 'evalr-*'`.
