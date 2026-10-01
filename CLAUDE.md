# RAGSearch Jev: working reference for Claude

Read AGENTS.md first (roles and engineering constraints). This file records how to run the project and what we learned.

## Run it
- One command: `./rag up` (generates `.env` with secrets, picks free ports, starts Docker stack, seeds samples). `./rag doctor` diagnoses; `./rag down` stops. `make` targets wrap these.
- Local dev (infra in Docker, API/worker/Vite on host): `scripts/run_local.sh {start|stop|status|logs}`.
- Enterprise-style: `scripts/run_enterprise.sh {up|down|status|logs|backup}` with `docker-compose.enterprise.yml`.
- Manual API start: `set -a; . ./.env; set +a; cd backend && uvicorn app:app --port 8000` (app.py has no server entry point; `.env` is NOT auto-loaded). Worker: `celery -A tasks.celery worker --pool=solo` (prefork breaks on macOS).
- Tests: `source venv/bin/activate && python -m unittest discover -s tests`; frontend: `cd frontend && npx tsc --noEmit`.

## Environment gotchas (hit during bring-up)
- Dependencies must be pinned (backend/constraints.txt): numpy<2, transformers==4.46.3, scipy<1.14, scikit-learn<1.6 with torch 2.2.2 (last Intel-mac wheel). Unpinned installs fail with NumPy ABI / GenerationMixin errors.
- Other local projects may hold ports 6333 (Qdrant) and 6379 (Redis). This project uses 6335 / 6380 locally; `./rag up` auto-picks free ports.
- Vite dev proxy must strip `/api` (nginx does in Docker).
- macOS BSD sed has no `sed -i` without a suffix: edit with python.
- Never put real keys in `.env.example` (it happened once; keys in the old working tree must be rotated). `.env`, `.env.*`, venv, logs and evaluation-runs are git-ignored.

## Architecture in brief
- FastAPI API + Celery worker + Postgres (FTS, chunks) + Qdrant (vectors) + Redis (broker, caches) + React/Vite UI.
- Search: agent router (Jev choice + local term-coverage override) -> local hybrid (Qdrant + Postgres FTS + BM25, weighted RRF, optional Jev rerank) or web (provider chain, BM25 + domain prior, no vector search). ACL is applied before any reranker/Jev/LLM/UI.
- Web search is private by default: per-request toggle AND deployment allow (`WEB_SEARCH_ENABLED` or admin setting); 403 otherwise; no admin bypass. Only the query text leaves.
- AI Overview is separate from `/search` (`POST /search/overview`, SSE), off by default, eligible only for queries longer than 3 words, grounded and citation-checked.
- Ingestion has one shared pipeline (backend/ingestion.py) for admin upload, Celery and `scripts/ingest_samples.py`.
- Design docs: docs/SEARCH_REDESIGN.md/.docx, docs/ARCHITECTURE_AND_CHANGES.docx, docs/RUNBOOK.md.

## Learnings
- Slowness root causes (measured): LLM answer inside `/search` (7-12 s local, 16-20 s web, nemotron-550b reasoning model); unreliable/slow ddgs scraping (2-3 s, 19% empty); serial Jev calls; ~7 s cold model load. Fix pattern: results first, overview separately, parallel stages, caches, pinned engines.
- Jev calls: merge into one request where possible; never wrap httpx in `asyncio.wait_for` (cancelled connects can wedge the pool); log every failure; a Jev outage must show as unavailable, not as a content judgement.
- Per-candidate Jev reranking costs ~11 requests per query and collapses under concurrency; prefer one batch call and partial-score fallback.
- Benchmarks can hide failures: a corpus where every query returns every document gives a flattering nDCG. Use an ACL-aware corpus with graded relevance (docs/eval).
- Keep claims honest: token reduction is an estimate until benchmarked; label measured versus target.
- Process: Opus reviews/designs, Sonnet implements, a separate critic agent tests, then fixes and a re-test. Do not report tests that did not run.

## Open work
See docs/KNOWN_ISSUES.md for the unfixed QA defects (priority: Jev client wedge, overview thread starvation, overview success rate).
