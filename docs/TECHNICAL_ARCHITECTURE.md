# Technical architecture

## Overview

React/Vite delivers search, evidence snapshots, and administration. FastAPI handles JWT authentication, upload metadata, query orchestration, provider configuration, and response formatting. Celery indexes uploads asynchronously through Redis. PostgreSQL stores users, ACLs, source metadata, versions, and extracted chunks. Qdrant stores dense vectors. A Sentence Transformers adapter embeds chunks and questions. PostgreSQL English full text search provides a lexical leg. Results combine with reciprocal rank fusion (RRF). The optional Jev reranker scores up to twelve authorized candidates in one SystemOne call. An OpenAI compatible generation adapter writes a cited answer only when evidence checks pass.

## Ingestion

1. Admin upload validates suffix and byte limit, hashes and writes source bytes, creates a document row, and enqueues `index_document`.
2. `DocumentExtractor` extracts PDF/DOCX/text/Markdown/HTML. `StructureChunker` respects paragraph/sentence boundaries, caps chunk size, and uses a bounded overlap.
3. Optional Jev Choice selects one deterministic chunk profile from narrative, table-heavy, code, policy or other. It does not write chunks.
4. `LocalEmbedder` creates normalized vectors; model dimension is checked against configured Qdrant dimension. `QdrantVectorStore` upserts deterministic UUID points with document/page payload. PostgreSQL stores chunk text and its lexical index.
5. Only after both stores commit does document status become ready. Failed jobs are marked failed and remain excluded from search.

## Search

1. Validate query and classify intent with Jev Choice. If unavailable or timed out, use broad retrieval.
2. Derive permitted document IDs and apply that filter inside Qdrant. PostgreSQL lexical SQL independently applies the ACL predicate before returning passages.
3. Fuse dense and lexical ranks by RRF. Optionally score the top 12 with Jev Score (3-level rubric) when admin-enabled; fall back to RRF on errors or malformed results.
4. Deduplicate overlapping passages and pack a bounded evidence set under `LLM_CONTEXT_BUDGET_TOKENS`. Source version, document, page and chunk IDs travel with every result.
5. If answer mode is requested and a generator is configured, a Jev Noul sufficiency gate can abstain before generation. Require citation markers that map to returned evidence; when Jev is configured, a second Noul checks cited support. Otherwise return retrieved source passages.

## Token efficiency

Context packing is designed to target 20–40% fewer input tokens compared with a naive top-result context, through duplicate overlap removal, bounded candidate count, and a configurable context budget. `estimate_tokens` is a word/punctuation proxy, not a tokenizer. The API returns estimated packed tokens and an explicit baseline. Do not state a measured production reduction until evaluation compares actual provider usage with a labeled corpus, and checks Recall@k/nDCG, citation precision, answer faithfulness, latency and cost. Tune the budget and profile by query type; preserve evidence quality as the release gate.

## Interfaces and extension points

`database.py` owns SQLAlchemy models and connection pooling. `config_store.py` handles encrypted provider settings. `providers.py` implements embedding, Qdrant and OpenAI compatible model adapters. `pipeline.py` owns parsing/chunking/packing. `retrieval.py` owns lexical retrieval and rank fusion. `jev.py` implements typed Jev calls and Score reranking. `app.py` composes auth/config/routes. `tasks.py` owns background indexing. `extensions.py` defines `pre_ingest`, `post_extract`, `pre_index`, `pre_query`, `post_answer` callbacks with bounded timeouts and isolated failures. API secrets are not passed to extensions. New parsers, embedding providers, rerankers and generators should implement the existing small interfaces instead of changing search routes.

## Data and deployment

Compose starts Postgres, Qdrant, Redis, API, worker and Nginx-served React. `.env` supplies endpoints and bootstrap secrets. Encrypted provider settings use a Fernet key derived from `APP_SECRET_KEY`; key rotation requires a deliberate settings re-encryption. Use persistent encrypted volumes and backups in deployment. Add SSO/MFA, formal audit records, OCR, tenant isolation, signed source-file preview, retention workflows, observability and end-to-end security tests before production.
