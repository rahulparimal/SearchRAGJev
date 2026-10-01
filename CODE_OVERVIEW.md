# RAGSearch Jev - Complete Code Overview

## Project Structure

```
RAGSearchJev/
├── AGENTS.md                    # Agent delivery roles & engineering constraints
├── docker-compose.yml           # Multi-service orchestration
├── README.md                    # Project documentation
├── .env.example                 # Environment template
├── backend/                     # FastAPI application
│   ├── agent.py                 # Agent router (local vs web)
│   ├── bm25.py                  # Okapi BM25 + ACL-scoped LocalBm25
│   ├── ingestion.py             # Shared ingest pipeline, stale sweeper, file versioning
│   ├── web_search.py            # Opt-in DuckDuckGo search (URL/citation sanitising)
│   ├── constraints.txt          # Verified dependency pins
│   ├── app.py                   # Main API composition, auth, routes
│   ├── config_store.py          # Encrypted settings management
│   ├── database.py              # SQLAlchemy models & connection
│   ├── Dockerfile               # Backend container
│   ├── extensions.py            # Extension hook system
│   ├── jev.py                   # TypeSafe Jev adapters
│   ├── pipeline.py              # Document extraction & chunking
│   ├── providers.py             # Embedding, vector, LLM adapters
│   ├── retrieval.py             # Hybrid search & RRF fusion
│   ├── requirements.txt         # Python dependencies
│   ├── security.py              # Secret validation
│   └── tasks.py                 # Celery background indexing
├── docs/                        # Documentation
│   ├── ADMIN_GUIDE.md
│   ├── BUILD_PROMPT.md
│   └── TECHNICAL_ARCHITECTURE.md
├── frontend/                    # React + Vite application
│   ├── Dockerfile
│   ├── index.html
│   ├── nginx.conf
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── src/
│       ├── main.tsx
│       ├── style.css
│       └── ui.tsx
├── scripts/
│   ├── evaluate.py              # Evaluation runner
│   └── evaluation_template.jsonl
└── tests/
    └── test_pipeline.py         # Unit tests
```

---

## Backend Architecture

### Core Components

#### 1. **app.py** - API Composition & Orchestration
- **FastAPI** application with CORS, JWT auth, role-based access
- **Endpoints**:
  - `GET /health` - Service health check
  - `POST /auth/login` - Username/password → JWT token
  - `GET /auth/me` - Current user info
  - `POST /admin/users` - Create users (admin only)
  - `POST /admin/documents` - Upload documents (admin only)
  - `GET /documents` - List documents
  - `GET /documents/{id}/snapshot` - Download original
  - `POST /search` - Hybrid search with optional Jev rerank & LLM answer
  - `GET /admin/settings` / `PUT /admin/settings` - Encrypted config
  - `POST /admin/reindex` - Trigger reindexing
- **Startup**: Validates `APP_SECRET_KEY`, creates DB indexes, initializes Qdrant, bootstraps admin user

#### 2. **pipeline.py** - Deterministic Ingestion
- **DocumentExtractor**: PDF (pypdf), DOCX (python-docx), TXT/MD/HTML (regex cleanup)
- **StructureChunker**: Sentence/paragraph-aware chunking with controlled overlap
  - Configurable `max_words` (default 420) and `overlap` (default 65)
  - Handles oversized paragraphs by sliding window
- **estimate_tokens()**: Word-based proxy (~0.75 words/token)
- **pack_ranked_passages()**: Budgeted evidence packing with deduplication
  - Returns: packed passages, used tokens, naive baseline, reduction %

#### 3. **retrieval.py** - Hybrid Search
- **PostgresLexicalSearch**: PostgreSQL full-text search with ACL filtering
  - Uses `websearch_to_tsquery` + `ts_rank_cd`
  - ACL: `d.acl='*' OR :user=ANY(string_to_array(d.acl,',')) OR :admin`
- **reciprocal_rank_fusion()**: RRF with k=60
  - Merges dense (Qdrant) + lexical (PostgreSQL) results
  - Returns sorted chunk IDs with payloads

#### 4. **jev.py** - TypeSafe Jev Adapters
- **JevDecisionProvider**: Calls TypeSafe SystemOne API
  - Choice questions (e.g., chunk profile selection)
  - State limited to 6000 chars
- **JevScoreReranker**: Scores up to 8 candidates
  - Typed Score questions with 3-level criteria
  - Falls back to RRF on any error

#### 5. **providers.py** - Pluggable Adapters
- **LocalEmbedder**: SentenceTransformers (all-MiniLM-L6-v2, 384-dim)
  - Lazy model loading, dimension validation
- **QdrantVectors**: Vector store operations
  - Collection auto-creation, payload indexing on `doc_id`
  - Filtered search by allowed document IDs (ACL)
- **OpenAICompatibleGenerator**: LLM answer generation
  - System prompt: cite evidence with [S#], treat as untrusted

#### 6. **database.py** - SQLAlchemy Models
- **User**: username, password_hash (Argon2), role (reader/admin)
- **Doc**: id, title, filename, checksum, version, status, owner, acl, snapshot_path
- **Chunk**: id, doc_id, page, text, token_estimate
- **Setting**: key, encrypted_value (Fernet)

#### 7. **config_store.py** - Encrypted Settings
- **key_fernet()**: Derives Fernet key from `APP_SECRET_KEY` (SHA-256)
- **read_setting()**: Decrypts from DB, falls back to env var

#### 8. **tasks.py** - Celery Background Indexing
- **index_document**: Full ingestion pipeline
  1. Extract pages → 2. Jev chunk profile (optional) → 3. Chunk → 4. Embed → 5. Upsert Qdrant → 6. Store chunks in PostgreSQL
  - Retries on connection errors (3x with backoff)
  - Updates document status: processing → ready/failed

#### 9. **extensions.py** - Hook System
- **Hooks**: pre_ingest, post_extract, pre_index, pre_query, post_answer
- **ThreadPoolExecutor** with 250ms timeout per hook
- Returns (value, events) for audit trail

#### 10. **security.py** - Secret Validation
- Rejects placeholder/short `APP_SECRET_KEY` (< 32 bytes)

---

## Frontend Architecture

### React + Vite + TypeScript

#### **src/ui.tsx** - Main Application
- **State**: auth token, user, documents, search results, answer, settings
- **Components**:
  - `LoginForm` - Username/password → stores JWT in localStorage
  - `SearchBar` - Query input, limit, answer toggle
  - `ResultsList` - Ranked passages with scores, page, snippet
  - `AnswerPanel` - LLM answer with citations
  - `AdminConsole` - User management, document upload, settings
  - `SettingsPanel` - Jev/LLM configuration (encrypted storage)
- **API calls**: Bearer token auth, error handling

#### **src/style.css** - Styling
- CSS variables for theming
- Responsive grid layout
- Component-scoped styles

---

## Data Flow

### Document Ingestion
```
Upload (POST /admin/documents)
    → Save file to /app/uploads
    → Create Doc record (status=processing)
    → Celery: index_document.delay(doc_id)
        → DocumentExtractor.extract()
        → Jev chunk profile (if enabled)
        → StructureChunker.split()
        → LocalEmbedder.encode()
        → QdrantVectors.upsert()
        → Store Chunk records in PostgreSQL
        → Doc.status = ready
```

### Search Query
```
POST /search {query, limit, answer}
    → current_user (JWT validation)
    → ACL: allowed_doc_ids from PostgreSQL
    → Dense: QdrantVectors.search(vector, allowed_doc_ids)
    → Lexical: PostgresLexicalSearch.search(query, user, admin)
    → RRF: reciprocal_rank_fusion(dense, lexical)
    → JevScoreReranker.rerank() (if enabled & key configured)
    → pack_ranked_passages() with token budget
    → OpenAICompatibleGenerator.answer() (if answer=true & LLM configured)
    → Return {results, answer, usage, intent, reranker}
```

---

## Configuration (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_DB` | ragsearch | Database name |
| `POSTGRES_USER` | ragsearch | Database user |
| `POSTGRES_PASSWORD` | change-me | **Must change** |
| `DATABASE_URL` | postgresql+psycopg://... | SQLAlchemy URL |
| `QDRANT_URL` | http://qdrant:6333 | Vector DB |
| `REDIS_URL` | redis://redis:6379/0 | Celery broker |
| `APP_SECRET_KEY` | replace-with-a-long-random-secret | **Must change** (32+ bytes) |
| `ADMIN_USERNAME` | admin | Bootstrap admin |
| `ADMIN_PASSWORD` | change-this-on-first-run | **Must change** |
| `TYPESAFE_API_KEY` | | Jev API key (optional) |
| `JEV_MODEL` | jev-latest | Jev model |
| `JEV_BASE_URL` | https://api.typesafe.ai | Jev endpoint |
| `JEV_RERANK_ENABLED` | false | Enable Score reranking |
| `JEV_CHUNK_PROFILE_ENABLED` | false | Enable chunk profile choice |
| `EMBEDDING_MODEL` | sentence-transformers/all-MiniLM-L6-v2 | Embedding model |
| `EMBEDDING_DIMENSION` | 384 | Must match model |
| `LLM_BASE_URL` | | OpenAI-compatible endpoint |
| `LLM_API_KEY` | | LLM API key |
| `LLM_MODEL` | | LLM model name |
| `LLM_CONTEXT_BUDGET_TOKENS` | 2800 | Evidence token budget |
| `MAX_UPLOAD_MB` | 50 | Upload size limit |
| `CORS_ORIGINS` | http://localhost:5173 | CORS origins |

---

## Security Model

1. **Authentication**: JWT (HS256) with 8-hour expiry, signed by `APP_SECRET_KEY`
2. **Authorization**: Role-based (reader/admin), document-level ACLs
3. **Encryption at Rest**: Fernet (AES-128) for API keys, key derived from `APP_SECRET_KEY`
4. **ACL Enforcement**: Both Qdrant (payload filter) and PostgreSQL (SQL WHERE)
5. **Input Validation**: Pydantic models, file type/size limits, username regex

---

## Testing

```bash
# Unit tests (no infrastructure needed)
python -m unittest discover -s tests -v

# Evaluation (requires running API + auth token)
export RAGSEARCH_TOKEN="your-jwt-token"
python scripts/evaluate.py --dataset scripts/evaluation_template.jsonl --api http://localhost:8000
```

**Test Coverage**:
- Chunking bounds & overlap
- Token estimation monotonicity
- Budget packing < baseline
- Deduplication & boundary handling
- Evaluation metrics (Recall@k, MRR, nDCG@k)
- Secret validation

---

## Deployment

### Docker Compose (Development)
```bash
docker compose up --build
# Frontend: http://localhost:5173
# API: http://localhost:8000
# API Docs: http://localhost:8000/docs
```

### Production Considerations
- Replace Docker Compose with Kubernetes manifests
- Add TLS termination (nginx/cert-manager)
- Configure private networking for PostgreSQL/Qdrant/Redis
- Add structured logging, metrics (Prometheus), tracing
- Implement rate limiting, audit logging
- Add backup/restore for PostgreSQL & Qdrant
- Use secret manager for `APP_SECRET_KEY`, passwords
- Enable CSRF protection for cookie sessions
- Add SSO/MFA, password reset, account deactivation

---

## Search modes, BM25, web search and ingestion (added on feature/agent-mode-bm25-ingestion)

- **POST /search** takes `mode` in `agent|local|web` (422 otherwise). `local` = Qdrant dense + Postgres full-text + in-process BM25, fused with RRF; ACL filtering happens before any rerank, Jev call, LLM or UI.
- **bm25.py**: `LocalBm25` caches ready-chunk rows under a content key (count + md5 of chunk ids, so same-count replacement invalidates) and builds one index per permitted-document set (LRU of 8), so idf/length statistics never include unreadable documents.
- **agent mode** (`agent.py`): Jev Choice decides local vs web; a strong local BM25 coverage (>=60% of query terms) overrides a web vote. Agent only routes to web when web search is enabled; otherwise it returns local with `route_reason='web_disabled'`.
- **web search** is private by default: disabled unless `WEB_SEARCH_ENABLED=true` (env) or the admin toggle. `mode=web` needs an admin or the setting (reader gets 403 when disabled). Only the query leaves the system. Results keep only http/https URLs, have `[S#]` markers stripped, are wrapped in `<<<SOURCE>>>` delimiters, and a generated web answer requires a Jev key and grounding check (otherwise results only). Provider failures return `degraded:true` with a notice. The UI labels these "Web answer, unverified".
- **ingestion.py**: one `ingest_document` used by the Celery task, `scripts/ingest_samples.py` and requeue. A ready document stays searchable during re-ingest (vectors upserted, chunk rows swapped in one transaction, stale vectors removed). `sweep_stale` marks documents stuck in `processing` (> `STALE_PROCESSING_MINUTES`, default 30) as failed at API start or via `POST /admin/documents/sweep-stale`; `POST /admin/documents/{id}/requeue` re-queues one. `register_file` dedups by filename+checksum and versions changed files (old version retired after the new one is ready).
- **/health** checks Postgres, Qdrant and Redis and returns 503 when degraded.
