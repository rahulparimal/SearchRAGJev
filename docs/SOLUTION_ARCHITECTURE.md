# RAGSearch Jev: solution and technical architecture

Audience: engineers and operators. Status labels: **Implemented**, **Target** (designed, not built), **Estimate** (unmeasured).
Companion docs: `TOKEN_EFFICIENCY.md`, `SEARCH_REDESIGN.md`, `RUNBOOK.md`, `KNOWN_ISSUES.md`, `ADMIN_GUIDE.md`.

## 1. Solution overview

A private-by-default search and answer workspace over an organisation's documents, with three user-selectable modes:

| Mode | What it does | Providers touched | Typical latency |
|---|---|---|---|
| Quick answer (default in UI) | Direct streamed LLM chat, no retrieval | Quick-answer LLM | first token ~1-3 s |
| Search | Hybrid retrieval over local KB and/or web, results first, optional AI Overview | Jev (optional), web (opt-in), LLM for overview (opt-in) | local <1 s, web 2-4 s |
| Deep Research | Multi-step web (and optional KB) research producing a cited, structured report | Web providers, Deep Research LLM, Jev | 45 s - 3 min |

Cross-cutting guarantees: ACL applied before any reranker/Jev/LLM/UI; web search needs per-request consent AND deployment allow; a
"Local KB only" toggle and a deployment-wide `LOCAL_ONLY_ENFORCED` lock make zero external calls; every LLM claim is citation-checked.

## 2. Component architecture

```
Browser (React/Vite, ui.tsx, research.tsx)
   |  HTTPS, SSE for /answer /search/overview /research
Nginx (static UI, /api proxy, SSE locations: proxy_buffering off)
   |
FastAPI (app.py)  -- auth (JWT), ACL, routes, config, rate limits
   |-- search path: plan.py(agent.py) -> retrieval.py(BM25+FTS+dense, RRF) -> jev.py rerank / local cross-encoder
   |-- overview.py (grounded, citation-checked)   direct.py (quick answer)   deepresearch.py (+research_report.py, webfetch.py)
   |-- tokencache.py / cache.py  (Redis, LRU fallback)      execpool.py (isolated thread pools)
   |
Celery worker (tasks.py) -> ingestion.py -> pipeline.py (extract, chunk) -> embedder -> Qdrant + Postgres
   |
Postgres (users, ACLs, docs, chunks, FTS + unaccent GIN)   Qdrant (vectors)   Redis (broker, caches, quotas)
External: Jev API (typed decisions), OpenAI-compatible LLMs, web providers (ddgs/Brave/SearXNG)
```

Deployment shapes (**Implemented**): dev compose (`./rag up`), host-run API/worker/Vite with Docker infra (`scripts/run_local.sh`),
enterprise compose, and a single all-in-one image (`docker/all-in-one`: Postgres, Redis, Qdrant, API, worker, nginx under supervisord,
data under `/data`).

### Module map

| Module | Responsibility |
|---|---|
| `app.py` | Routes, auth, ACL, gating (consent, quota, local-only), `/health`, admin config |
| `pipeline.py`, `ingestion.py` | Extraction, sentence-aware chunking over the whole document stream, profiles, shared ingest for upload/Celery/scripts |
| `retrieval.py`, `bm25.py` | RRF fusion (lexical legs combined by max), BM25 with accent folding |
| `jev.py` | Pooled async Jev client, circuit breaker, batched Noul rerank, `ground_batch`, `route_complexity` |
| `providers.py` | Embedder, Qdrant, `OverviewGenerator`, `ChatGenerator`, local cross-encoder |
| `web_search.py`, `execpool.py` | Provider chain, hedged fetch, breakers; dedicated pools so web/overview/search never starve each other |
| `overview.py`, `direct.py` | AI Overview and Quick answer streams |
| `deepresearch.py`, `research_report.py`, `webfetch.py` | Deep Research orchestrator, deterministic report parser/validator, hardened page fetcher |
| `cache.py`, `tokencache.py` | TTL cache + counters; sealed LLM-material cache with single-flight |

## 3. Data architecture

- **Postgres**: users, documents (version, ACL), chunks (text, page, `search_text` with a GIN index on `immutable_unaccent`).
- **Qdrant**: one vector per chunk (MiniLM-L6, 384 d); payload carries document/page for ACL filtering inside the vector query.
- **Redis**: Celery broker; caches (`ov:`, `tc:`, web, `sid:` evidence handles); rate-limit counters (`INCR`+`EXPIRE`).
- **Secrets**: provider keys Fernet-encrypted in settings; derived from `APP_SECRET_KEY`. Cached LLM material is sealed the same way.

## 4. Ingestion pipeline (**Implemented**)

Upload -> validate/hash -> document row -> Celery `index_document` -> extract (PDF/DOCX/text/MD/HTML, OCR-diacritic and
hyphenation repair) -> optional Jev chunk-profile choice -> `StructureChunker.split_document` (whole-document sentence stream,
whole-sentence overlap, word budgets kept under the embedder's 256-wordpiece limit) -> embed -> Qdrant upsert + Postgres rows ->
status `ready` only after both commit. Failed jobs stay excluded; a sweeper fails documents stuck in `processing`.

## 5. Search architecture (**Implemented**)

1. Agent router: Jev Choice (local/web/both) plus a local term-coverage override.
2. Local: three legs (dense, Postgres FTS, BM25) over ACL-permitted documents; weighted RRF with `lexical='max'`; dense leg guarded when the
   embedder is warming. Rerank: Jev batched Noul (top 20, one request) else local cross-encoder, else RRF order.
3. Web: provider chain, BM25 plus a domain prior, no vector search; only the query text leaves.
4. Results return first. AI Overview is a separate SSE request, grounded sentence by sentence and dropped when ungrounded.

Failure semantics: a Jev or provider outage surfaces as "unavailable/degraded", never as a content judgement; partial rerank scores
are used rather than discarded.

## 6. Security and privacy model

- Auth: JWT sessions; admin vs reader roles; ACL lists per document.
- Egress control: web off by default; needs deployment allow + per-request toggle; no admin bypass. Local-only and enforced lock make
  `/answer`, `/search/overview`, `/research` return 403.
- Prompt-injection: untrusted text (web, KB) is nonce-delimited (`<<<SRC nonce Sn>>>`), look-alike markers stripped, model output is
  never trusted as markup (server-side parse, allowed block types only).
- SSRF: provider URLs validated; Deep Research page fetch uses a stricter boundary (section 8.5).
- Cache safety: sealed values, ACL-dependent material keyed per user/scope or not cached.
- Rate limits: per-user hourly Deep Research quota (Redis), process-wide concurrency caps.

## 7. Performance and resilience design

Thread-pool isolation (`run_search`, `run_io`, `run_ov`, `run_web`); circuit breakers for Jev and each web provider; never cancel an
httpx request mid-connect (cancelled connects wedged the pool); results-first so slow LLMs never block search; hedged web fetch;
bounded retries. Open defects are tracked honestly in `KNOWN_ISSUES.md`.

---

## 8. Deep Research module

### 8.1 Purpose and non-goals

Answer complex, multi-part questions by decomposing them, gathering public web evidence (optionally the user's own KB), and writing a
structured report (summary, per-sub-question sections, comparison tables, disagreements) where every factual sentence carries a
checkable citation. Non-goals: private/offline research (it is explicitly NOT private), real-time latency, unattended autonomous
browsing, or answering from model memory.

### 8.2 Pipeline

```
POST /research (SSE)
 gates: local-only/enforced -> 403 | web disabled -> 403 | feature off -> 403 | no consent -> 403 | quota -> 429 | Jev required but absent -> 503
 0. report cache lookup (web-only runs)  --hit--> stream cached report, 0 LLM, 0 web
 1. plan:   plan cache -> Jev complexity route (simple => skip planner) -> else LLM planner (JSON) -> fallback single sub-question
 2. search: per sub-question, <=2 queries, shared semaphore (WEB_PARALLEL), yields to normal /search via web_headroom(), stops when all breakers open
 3. reflect: sub-questions that found nothing get one alternative query (max REFLECT_MAX); recovered ones leave the gap list
 4. read:   optional full-page fetch (webfetch.py) replaces provider snippets; failures fall back to snippets
 5. kb:     optional ACL-checked local search per sub-question (include_kb)
 6. write:  ONE LLM call over nonce-delimited numbered sources, restricted-markdown contract
 7. validate: research_report.parse_report - drop uncited claims, reject report if drop rate > DROP_RATE_MAX
 8. verify: Jev ground_batch over up to GROUND_MAX claims (prose first, then cited table cells with row/column context; batched), flag claims below GROUNDED, status verified|partial|unavailable|skipped
 9. emit:   report event (blocks, citations, gaps, verification, markdown, stats), then done; seal into cache if healthy
```

SSE events: `meta`, `status` (planning/searching/reflecting/reading/writing/verifying), `plan`, `subq`, `report`, `error`, `done`.
Every failure is an `error` event with a reason (`busy`, `not_configured`, `no_sources` + gaps, `provider_unavailable` + partial
sources, `unsupported_claim`); the generator never raises.

### 8.3 Design decisions and why

| Decision | Rationale |
|---|---|
| Plan -> search -> write is a fixed pipeline, not a free-running agent loop | Bounded cost and latency; reproducible; each stage independently testable. Reflection is one bounded retry, not open-ended. |
| Jev decides, LLM writes | Routing, relevance and grounding are decisions Jev can make cheaply; at most 2 LLM calls per run (`stats.llm_calls`). |
| Title from the plan, body parsed deterministically | The model cannot inject structure; the parser only emits h2/h3/p/ul/table blocks. |
| Uncited claims dropped, high drop rate rejects the report | An honest "could not support this" beats a fluent unsupported report. Coverage gaps are stated, not papered over. |
| Honest verification labels | `verified` / `partial` / `unavailable` / `skipped`; an outage is never shown as success. In snippet mode many claims are flagged because snippets are short; full-page mode (Phase B) addresses this. |
| KB reports not cached, per-user quota, consent per request | Privacy and cost control. |

### 8.4 Cost and latency controls

Planner skipped for simple questions (Jev) and cached; report cached 30 min; one report call; batched grounding (about 2 Jev
requests for 40 claims instead of 40); source and output caps; total deadline `DEEP_RESEARCH_TOTAL_S`; process concurrency cap;
per-user hourly quota. See `TOKEN_EFFICIENCY.md`.

### 8.5 Page fetcher trust boundary (`webfetch.py`)

Off by default (`DEEP_RESEARCH_FETCH_PAGES`). Stricter than provider-URL validation because URLs come from the open web: http(s)
only; optional host allowlist; DNS resolved and EVERY address must be globally routable (rejects loopback, private, link-local,
metadata, multicast, unspecified, IPv4-mapped IPv6); redirects followed manually (max 3) with the check repeated per hop; content
types limited to html/plain/xhtml; byte cap; text extraction strips script/style/nav/footer/forms. Residual risk: DNS-rebinding TOCTOU
between check and connect; mitigated by running the API without access to internal networks. Documented, not eliminated.

### 8.6 Configuration

`DEEP_RESEARCH_*` env vars (see `.env.example`) and the admin tab "Deep Research" (enable switch, LLM base URL/model/key with fallback
to Overview then base LLM). Key knobs: `MAX_SUBQ`, `QUERIES_PER_SUBQ`, `MAX_SOURCES`, `TOTAL_S`, `REPORT_MAX_TOKENS`,
`ROUTE_SIMPLE_BELOW`, `REFLECT_MAX`, `DEEP_RESEARCH_CACHE_TTL_S`, `FETCH_PAGES`, `REQUIRE_JEV`.

### 8.7 Observability

`/health` reports `research_active` (runs in flight) and `token_cache` (hit rate, estimated tokens saved) next to Jev breaker state.
Logs carry stage failures with reason class only. Per-run `stats` ship in the report event.

### 8.8 Testing

Unit: planner fallback, happy path, no-sources, breakers open, uncited rejection, report/LLM failure, Jev outage vs disabled, injection
resistance, counter release, KB gating, page-fetch wiring and failure, report/plan caching, KB never cached, Jev-routed planner skip,
reflection recovery, 23 SSRF/fetch tests, 15 report-parser tests. Endpoint gates in `test_search_redesign.py`. Live: curl and browser
runs against real providers. Quality: `docs/eval/deep_research.jsonl` (8 questions with rubrics) - **rubric scoring is manual today;
an automated runner is Target**. Known gap: unit tests call functions directly and bypass FastAPI routing; a decorator misplacement
once broke `/auth/me` unseen - keep ASGI-level route smoke tests.

### 8.9 Roadmap and decisions

Built after review: table-cell verification, honest footer, Refresh, print/save as PDF (browser print stylesheet), Quick-answer cache (opt-in),
eval A/B instrumentation. Deferred: detached/resumable runs (owner-scoped sealed event streams; needs a design), map-reduce summaries,
semantic cache lookup, automated quality scoring. Dropped: streaming an unverified draft (it would bypass citation validation).

## 9. Operations

`./rag doctor` diagnoses; `./rag up/down`; `/health` for liveness and Jev/token-cache state; back up Postgres, Qdrant and `/data`;
rotating `APP_SECRET_KEY` invalidates sessions, encrypted settings and sealed caches (all read as misses). Pin dependencies per
`backend/constraints.txt`. Never commit real keys.
