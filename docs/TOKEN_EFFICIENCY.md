# Token efficiency design: spend an LLM token only when nothing cheaper can answer

Status labels used throughout: **Implemented** (in the code and tested), **Target** (designed, not built), **Estimate** (not benchmarked).
Token counts in the product are chars/4 estimates, never provider billing data. Do not quote a percentage saving until it is measured
against real provider usage (see "Measuring").

## 1. Principle: a ladder, cheapest rung first

Every request walks down this ladder and stops at the first rung that can answer. LLM generation is the last rung, not the first.

| Rung | Mechanism | Cost per request | Status |
|---|---|---|---|
| 0 | Exact/normalised cache hit (report, plan, route) | 0 LLM tokens, ~1 Redis read | Implemented (`backend/tokencache.py`) |
| 1 | Single-flight: identical concurrent requests share one computation | 0 extra tokens for followers | Implemented |
| 2 | Jev typed decision (Choice/Score/Noul) instead of an LLM call | 1 small Jev call, no generated text | Implemented for planning-skip, mode/route, reranking, grounding |
| 3 | Deterministic code (BM25, FTS, RRF, markdown/citation parser, SSRF checks) | 0 tokens | Implemented |
| 4 | LLM with a minimal, budgeted prompt | Tokens spent | Implemented (context budget, source char caps) |

Jev only emits typed decisions (a choice from a list, a 0..1 score, a boolean-ish Noul). It cannot write prose. So the rule is:
**Jev decides, the LLM writes.** Anything that is a decision (route, complexity, relevance, grounding, chunk profile) belongs on
Jev; only the user-visible prose (overview, quick answer, report) needs an LLM.

## 2. Where Jev replaces an LLM call

| Decision | Old approach | Now | Tokens avoided |
|---|---|---|---|
| Local vs web route | LLM classifier | Jev `choice` (`plan.py`, one combined plan call) | one LLM call per search |
| Is a Deep Research question complex enough to decompose? | always run the LLM planner (~500 output tokens + prompt) | `jev.route_complexity`: Noul score < `DEEP_RESEARCH_ROUTE_SIMPLE_BELOW` (0.25) skips the planner | the whole planner call for easy questions (Estimate: ~700 tokens) |
| Result relevance | LLM rerank | Jev batched Noul (one request, up to 20 candidates) or local cross-encoder | all rerank tokens |
| Claim grounding / citation check | LLM judge | Jev batched Noul (`jev.ground_batch`, ~1 request per 20 claims) | all judge tokens |
| Chunk profile at ingest | heuristics + LLM | Jev `choice` | n/a (ingest-time) |
| Overview eligibility | LLM | Jev `overview_worthy` score in the plan call, else word-count heuristic | one call |

Fallback rule (must hold for every Jev use): a Jev outage is "unavailable", never a content verdict. `route_complexity` returns `None`
on failure and the caller falls back to the LLM planner, so Jev down costs tokens but never correctness.

## 3. The cache module: `backend/tokencache.py`

One module owns caching of LLM-derived material so no feature invents its own keying, TTL, encryption, or stampede handling.

**Keys.** `make_key(ns, *parts)` -> `tc:v1:<ns>:<sha256>`. Callers must include everything that changes the output: normalised query,
model, endpoint id, option flags, and (for ACL-dependent material) the user/scope. `norm_query` casefolds, collapses whitespace and
strips trailing `?!.` so trivial variants share an entry.

**Encryption at rest.** Values are Fernet-sealed with the app key before reaching Redis (cached material can be derived from restricted
documents). A wrong key or tampered value reads as a miss.

**TTL per namespace** (env-tunable): `plan` 24 h, `route` 24 h, `report` 30 min (`DEEP_RESEARCH_CACHE_TTL_S`; web evidence goes stale),
`answer` 15 min. A global kill switch: `TOKENCACHE_ENABLED=false`.

**Single-flight.** `get_or_compute(ns, key, compute)` runs `compute()` once for N concurrent identical requests; followers await the
leader. If the leader fails, followers compute for themselves (a failure is never cached or shared). `None` is never cached.

**Counters.** `snapshot()` is exposed in `/health` as `token_cache`: per-namespace hit/miss/coalesced, overall hit rate, and
`est_tokens_saved` (estimate).

**What is cached today.**
- Deep Research **report** (web-only runs): second identical question returns in milliseconds with 0 LLM calls and 0 web queries.
  Not cached when `include_kb` is on (KB-derived content is ACL-dependent; sharing it across users would leak). Not cached when
  verification was unavailable (do not freeze a degraded result).
- Deep Research **plan** (only real LLM plans, never the fallback plan) and the Jev **complexity route**.
- Existing, separate: AI Overview results (`ov:v1:*`, sealed, keyed by evidence ids + scope + model), web search results
  (`WEB_CACHE_TTL_S`), embeddings/query caches in the search path.

**Target (not built).**
- Quick answer cache for history-free, query-only turns (`answer` namespace is reserved; needs a safety review: personalised or
  time-sensitive prompts must bypass it).
- Semantic (embedding-similarity) lookup so paraphrases hit: use the existing embedder, store query vectors, accept only above a
  high threshold (>= 0.92) and only for `plan`/`route`, never for reports.
- Corpus-generation keys for KB-derived entries (`cache.generation('corpus')`) so a re-ingest invalidates them instead of waiting for TTL.
- Provider prompt caching: keep the static system prompt first and byte-identical (`PLAN_SYSTEM`, `REPORT_SYSTEM` already are) so
  providers that discount cached prefixes apply it automatically. Put volatile text (query, sources) last.

## 4. Other token levers already in the design

- **Results first, prose later.** `/search` returns no LLM text; the overview is a separate opt-in request (off by default, only for
  queries longer than 3 words), and Local KB only / enforced local-only mode makes zero provider calls.
- **Context budget.** `LLM_CONTEXT_BUDGET_TOKENS` packs deduplicated evidence under a cap; sources are char-capped
  (`OVERVIEW` / `DEEP_RESEARCH_SOURCE_CHARS`, `DEEP_RESEARCH_PAGE_TEXT_CHARS`).
- **Bounded output.** `OVERVIEW_MAX_TOKENS`, `DIRECT_MAX_TOKENS`, `DEEP_RESEARCH_REPORT_MAX_TOKENS`.
- **One LLM call for the report.** Deep Research uses exactly two LLM calls at most (plan, report); verification is Jev, parsing is code.
  `stats.llm_calls` reports this; cache hits report 0 new calls.
- **Right-size the model per job.** Quick answer: fast non-reasoning model. Deep Research report: stronger model is acceptable because
  it runs once. Admin tabs set each independently; blank falls back down the tier chain.
- **Do not retry blindly.** Bounded retries (one on 429/5xx) with an attempt cap so a stuck call cannot double the spend.
- **Fail before spending.** Gates (consent, quota, local-only, concurrency, not-configured) run before any LLM or web call.

## 5. Measuring (required before claiming savings)

1. Log provider-reported `usage` (prompt/completion tokens) per call and per feature; Jev already records `input_tokens`/`output_tokens`.
2. Replay a fixed query set twice: cold cache, then warm. Report hit rate (`/health` -> `token_cache.hit_rate`) and real tokens, not the estimate.
3. A/B the planner skip: run the question set with `DEEP_RESEARCH_ROUTE_SIMPLE_BELOW=0` and at 0.25; compare report quality with the
   rubric set in `docs/eval/deep_research.jsonl` and total tokens. Keep the threshold only if quality is unchanged.
4. Quality gate stays primary: tokens saved never justify lower citation precision or more unsupported claims.

## 6. Operating guidance

- Keep `TOKENCACHE_ENABLED=true`; clear a namespace by bumping its key version (`tc:v1` -> `tc:v2`) in `make_key`.
- A user who needs fresh research should be offered a "refresh" control (bypass cache) -- **Target**; today wait out the 30 min TTL or restart Redis.
- Never put a user name in a key for shared material (reduces hit rate) and never omit it for ACL-dependent material (leaks data).
