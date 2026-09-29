# Atlas Search Jev

Atlas Search is a local first, modular document search and grounded answer application. It uses Jev for typed decisions, Sentence Transformers for embeddings, Qdrant for dense retrieval, PostgreSQL full text search for lexical retrieval, and a configurable OpenAI compatible endpoint for answer generation. Redis and Celery keep indexing work out of the interactive API path.

## Architecture and efficiency target

The request path retrieves dense and lexical candidates with ACL filters, merges rankings with reciprocal rank fusion, optionally reranks a bounded shortlist with Jev Score, removes duplicate overlapping passages, and packs evidence under a configurable token budget. This is intended to use **20–40% fewer input tokens than a defined top-result baseline** while preserving retrieval and answer quality. The app reports an estimated reduction for each query; it does not claim a production gain until measured with actual provider token usage and a labeled evaluation set.

The token estimate is deliberately labeled as a proxy. Before comparing providers, create a benchmark with representative queries and relevant document/page labels. Record retrieval recall/nDCG, answer citation support, actual API input tokens, p95 latency and spend for baseline and optimized modes on the same corpus snapshot. Accept a lower token budget only when quality gates pass.

## Start locally

1. Copy `.env.example` to `.env`. Set unique PostgreSQL and `APP_SECRET_KEY` values and a strong `ADMIN_PASSWORD`. Treat the `.env` file as secret.
2. Run `docker compose up --build` from this directory. The first startup downloads the embedding model and can take several minutes.
3. Open http://localhost:5173 and sign in with the admin username and password from `.env`.
4. In Admin Console upload a PDF, DOCX, TXT, Markdown or HTML file. Wait for the Celery worker to report `ready`; then search it.
5. Add a TypeSafe key and an optional OpenAI compatible LLM endpoint in Admin Console. Keys are encrypted in PostgreSQL using a key derived from `APP_SECRET_KEY`. Jev and answer generation are optional; retrieval works without them.

The first administrator is created only when the database has no user with `ADMIN_USERNAME`. Change the bootstrap password after first login (the current MVP requires updating the environment and replacing the user record to rotate it). For production use, implement full user lifecycle, SSO/MFA, CSRF protection for cookie sessions, TLS termination, encrypted backups and a secret manager.

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

PDF (text based), DOCX, TXT, Markdown and HTML are supported. Scanned PDFs currently fail with a clear ingestion error because OCR is not wired in. PPTX, document replacement/version history, tenant management, admin user provisioning, citation-faithfulness evaluation, and recovery-grade object storage are not yet implemented. Uploaded originals are stored in the shared Docker volume; PostgreSQL currently stores chunks and full text for PostgreSQL FTS.

## Security notes

API key settings are Fernet encrypted at rest with a key derived from `APP_SECRET_KEY`; losing that secret makes encrypted settings unreadable. Use a strong random secret, rotate using a planned re-encryption migration, and never commit `.env`. The default development values are unsafe for network exposure. Restrict CORS, put TLS/auth gateway in front, configure Qdrant/Postgres/Redis private networking, and add audit log retention before production. ACLs are document level and enforced in Qdrant filters and PostgreSQL lexical SQL before passage contents are used for model calls. Admins may read all documents.

## Verify

Run `python3 -m unittest discover -s tests -v` to verify chunk bounds, overlap and context budget packing. The API and end to end stack should also be checked with a configured Docker engine and representative evaluation corpus before production.
