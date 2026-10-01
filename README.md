# Atlas Search Jev

Atlas Search is a local first, modular document search and grounded answer application. It uses Jev for typed decisions, Sentence Transformers for embeddings, Qdrant for dense retrieval, PostgreSQL full text search for lexical retrieval, and a configurable OpenAI compatible endpoint for answer generation. Redis and Celery keep indexing work out of the interactive API path.

## Architecture and efficiency target

The request path retrieves BM25, dense and Postgres full-text candidates in parallel with ACL filters, merges rankings with weighted reciprocal rank fusion, reranks the top candidates with one typed Jev Noul judgment each (when a Jev key is set), removes duplicate overlapping passages, and packs evidence under a configurable token budget. The 20-40% input-token reduction is a **target** against a defined top-result baseline, not a claim: the app reports packed versus unpacked token estimates per query and makes no savings claim until measured with actual provider token usage and a labeled evaluation set. `POST /search` never calls the LLM; the optional AI Overview is a second request (`POST /search/overview`, Server-Sent Events) over evidence the server already ACL-filtered and stored under a `search_id`.

The token estimate is deliberately labeled as a proxy. Before comparing providers, create a benchmark with representative queries and relevant document/page labels. Record retrieval recall/nDCG, answer citation support, actual API input tokens, p95 latency and spend for baseline and optimized modes on the same corpus snapshot. Accept a lower token budget only when quality gates pass.

## Quick start (Docker is the only prerequisite)

```bash
./rag up        # creates .env (generated secrets, never printed), picks free ports, builds, starts, waits healthy, seeds sample docs
./rag doctor    # one line per check with a fix hint; exit 1 if anything fails
./rag down      # stop (data kept);  ./rag reset deletes volumes;  ./rag status | logs [svc] | seed | eval | dev
```

`./rag up` prints the URLs it chose (ports move up when 5173/8000/5433/6335/6380 are busy). The admin password is in `.env` (`ADMIN_PASSWORD`). A second `./rag up` is idempotent and never rewrites existing secrets. `./rag up --check` shows what would happen without changing anything. See [docs/RUNBOOK.md](docs/RUNBOOK.md) for host (dev) mode and troubleshooting.

Search shows ranked results first (BM25, vectors and Postgres full text, optional Jev rerank). The **AI Overview** is off by default and is written only after the results are on screen, only for questions of more than 3 words, and only when it passes a per-sentence grounding check; **Web search** is off by default and sends only the query text to the providers shown in the UI. Add a TypeSafe key and an optional OpenAI compatible LLM endpoint in Admin Console (keys are encrypted in PostgreSQL using a key derived from `APP_SECRET_KEY`). Jev and the LLM are optional; retrieval works without them.

Manual start: copy `.env.example` to `.env`, set unique secrets, run `docker compose up --build`, open http://localhost:5173.

The first administrator is created only when the database has no user with `ADMIN_USERNAME`. Administrators can create reader/admin accounts in the Admin Console. Change the bootstrap password after first login (the current MVP requires updating the environment and replacing the user record to rotate it). For production use, add password reset, account deactivation, SSO/MFA, CSRF protection for cookie sessions, TLS termination, encrypted backups and a secret manager.

## Components

- `backend/app.py`: API composition, auth, admin routes and search orchestration.
- `backend/pipeline.py`: document extraction, structure aware chunking, token estimation and evidence packing.
- `backend/retrieval.py`: PostgreSQL lexical retrieval and RRF fusion.
- `backend/jev.py`: TypeSafe SystemOne adapter and optional bounded Score reranker.
- `backend/extensions.py`: explicit bounded extension hooks.
- `backend/tasks.py`: Celery background indexing.
- `frontend/src`: React search and admin interfaces.
- PostgreSQL: account/document metadata, ACLs, version and chunk text.
- Qdrant: normalized dense vectors with document/page metadata.
- Redis: queue broker.

## Jev policies

Jev returns typed decisions (Choice, Score or Noul), not document vectors or answer prose. Query mode classification and answerability gates run when a key is configured. Admin can optionally enable Jev to choose a tested chunk profile and score up to 12 search candidates. The default keeps both optional actions disabled until the administrator has reviewed data handling and evaluated accuracy, latency and cost. The app includes Jev only with a bounded sample of extracted text for the chunk-profile decision; do not enable this for sensitive corpora unless external processing is approved.

## Supported documents and limitations

PDF (text based), DOCX, TXT, Markdown and HTML are supported. Scanned PDFs currently fail with a clear ingestion error because OCR is not wired in. PPTX, document replacement/version history, tenant management, citation-faithfulness evaluation, and recovery-grade object storage are not yet implemented. Uploaded originals are stored in the shared Docker volume; PostgreSQL currently stores chunks and full text for PostgreSQL FTS.

## Security notes

API key settings are Fernet encrypted at rest with a key derived from `APP_SECRET_KEY`; losing that secret makes encrypted settings unreadable. Use a strong random secret, rotate using a planned re-encryption migration, and never commit `.env`. The default development values are unsafe for network exposure. Restrict CORS, put TLS/auth gateway in front, configure Qdrant/Postgres/Redis private networking, and add audit log retention before production. ACLs are document level and enforced in Qdrant filters and PostgreSQL lexical SQL before passage contents are used for model calls. Admins may read all documents.

## Verify

Run `python3 -m unittest discover -s tests -v` to verify chunk bounds, overlap and context budget packing. The API and end to end stack should also be checked with a configured Docker engine and representative evaluation corpus before production.

Benchmark: `./rag seed docs/samples/eval` loads a 38-document synthetic corpus (near duplicates, a version pair, shared-vocabulary distractors, 2 ACL-restricted documents), then `python scripts/evaluate.py --label baseline --repeat 3` runs `docs/eval/search_benchmark.jsonl` (graded relevance) as a non-admin reader and reports nDCG@5, MRR@5, per-stage p50/p95 latency, ACL leaks, and with `--overview` the overview eligibility and withheld rates. `--web-fixtures docs/eval/web_fixtures` ranks recorded web results offline. Token figures are word-based estimates; no savings claim is made without a measured baseline. Legacy datasets with `relevant_document_ids` still work (`scripts/evaluation_template.jsonl`).
