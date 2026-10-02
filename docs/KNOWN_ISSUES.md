# Known issues (original adversarial QA from 2026-10-01, status updated same day after a follow-up fix pass)

From an independent adversarial QA of the T1-T9 search redesign. Status below is per-item: **fixed** (with the evidence), **open**, or **cleanup only** (fixed, but nothing re-benchmarks it). This file was found to be stale during the follow-up pass -- most High items were already fixed in code before this update; verify status against the code, not just this file, before relying on it.

## High
1. **Jev client wedges. FIXED.** `AsyncJev.decide`/`_attempts` (backend/jev.py) use `httpx.Timeout` only, never `asyncio.wait_for`/cancellation, around the request itself. `_recycle()` rebuilds the pooled client after `RECYCLE_AFTER=5` consecutive timeouts; a per-base `CircuitBreaker` opens after 5 failures in 10s; pool is `JEV_POOL_MAX=128`. `/health` now reports `jev.recycles`/`jev.inflight`/breaker state.
2. **Slow overviews starve `/search`. FIXED, and completed in the follow-up pass.** `OverviewGenerator.agenerate` uses its own async httpx pool (`providers._ov_client`), never a worker thread. `execpool.py` has a dedicated `run_ov` pool for overview/quick-answer settings/cache/evidence I/O (16 overview streams no longer compete with search's `run_io`/`run_search` pools for that bookkeeping) -- `run_ov` existed but was unused in the first pass of this fix; `search_overview` and the new `/answer` endpoint were wired to it in the follow-up. `OVERVIEW_MAX_CONCURRENT=8` returns reason `busy` past the cap.
3. **Overview success about 55%. PARTIALLY FIXED.** Grounding threshold lowered to `OVERVIEW_GROUNDED_MIN=0.6`; a failing bullet is dropped individually rather than voiding the whole overview; grounding is given query+lead context. Not done: retry-once on LLM failure, and no default fast non-reasoning model is chosen for `OVERVIEW_LLM_MODEL` (an admin must still pick one). Not re-benchmarked since the 0.6 threshold change.

## Medium
4. **Jev per-candidate rerank collapses under concurrency. FIXED.** The per-candidate (`noul_parallel`) and sync (`score_batch`) rerank styles were removed from the request path entirely (not just defaulted off) -- only the batched `noul_scores_batch` runs for live searches now. `JEV_BATCH_SIZE` (default 20) tracks `JEV_RERANK_TOP_N` (default 20) so a full rerank costs one Jev request, not several. `JevScoreReranker`/`JevDecisionProvider` remain for ingestion/scripts only.
5. **Bing ads as organic results. FIXED.** `web_search.py`'s `ENGINE_HOSTS` filter excludes bing.com and other search-engine hosts from organic results.
6. **Local fusion loses semantic matches. PARTIALLY FIXED, root cause found in the follow-up pass.** `DENSE_MIN_SCORE`/`DENSE_ALONE_MIN` floors and a no-lexical-weak-dense-only zeroing rule were already in place; `JEV_RERANK_TOP_N` raised 12->20 (not yet benchmarked -- see `.env.example`). The follow-up pass found and fixed a likely bigger cause: ingestion chunk sizes (420/260/320/500 words) tokenized to ~450+ wordpieces against the embedding model's 256-wordpiece `max_seq_length`, so roughly the back half of every chunk was silently dropped from the vector dense search actually uses. Chunk-size profiles were cut to 200/130/160/210 words (measured: 200 words -> ~219 wordpieces, safely under budget) -- verified by ingesting a 479-page real-world PDF and confirming all 1,010 resulting chunks stayed within budget (avg 169.5, max 200 words) with good retrieval on content queries and a clean negative control. This only affects newly-ingested/re-ingested documents; nothing in this change re-ingests existing ones.
7. **Web Jev rerank lowers nDCG. UNCHANGED.** `WEB_RERANK_ENABLED=false` by default, as before; not revisited.
8. **Local rerank gain below target. OPEN, not re-measured.** The `JEV_RERANK_TOP_N` 12->20 change and the chunking fix (item 6) are both plausible levers but neither has been benchmarked against this target; run `scripts/evaluate.py` before claiming a number.
9. **`mode=local` leaking a speculative web fetch. FIXED (unrelated to this pass).** `web_on` is unconditionally `False` for `mode=='local'` in `app.py`; the new server-enforced `local_only` toggle added in this pass reuses and extends this same guarantee (it also disables Jev/plan/overview, not just the web fetch).
10. **Empty web results tripping the circuit breaker. FIXED.** `hedged_fetch` only calls `.fail()` on real exceptions/timeouts; an empty answer calls `.ok()`. The follow-up pass found and fixed a related case introduced in this session's own changes: a local thread-pool-saturation error (`web_busy`, from the new dedicated web-fetch pool) was briefly mis-handled as a provider failure and could trip a provider's breaker and misreport the notice; both are now excluded from breaker/notice accounting.
11. **Jev outage shown as a content judgement. FIXED.** `jev.down` / partial-answer handling (`jev_rerank_partial`, `jev_partial`) distinguish an outage from a real verdict; `ground_sentences` returning `None` is never treated as 0.0.
12. **Outages invisible to doctor. FIXED.** `scripts/doctor.py` checks the reranker fallback, `degraded_stages`, and Jev breaker/failure state on the smoke search; `/health` now also reports Jev pool recycle count and in-flight requests.
13. **Vector search still runs for local routes in agent+web. BY DESIGN, unchanged.**

## Low
14-22: not revisited in this pass except where noted above (6, 10, 12). Still open as originally described: cache-hit latency target (14), word-count gate edge cases (15), overview heading-line validation (16), one shared SQLAlchemy session (17, partially addressed by this pass's io-pool split but not the session sharing itself), `evaluate.py` test-user cleanup (18), overview cache key / plaintext Redis caching (19), admin-settable provider URL validation (20), tests faking Jev/LLM with no real-pool/concurrency tests (22). Item 21 (setup rough edges) is now fixed -- see below.

## This session's follow-up pass also added and fixed
- **Local KB only toggle**: `local_only` on `/search`, server-enforced (not just a UI convention) -- disables web/Jev/plan/overview outright, plus a deployment-wide `LOCAL_ONLY_ENFORCED` setting that also 403s `/search/overview` and `/answer`.
- **Quick answer**: a new default search mode (`POST /answer`, SSE) that bypasses all retrieval/Jev/web for a fast direct LLM chat. Explicitly NOT private by default (labeled as such in the UI); disabled entirely by Local KB only.
- A real correctness bug in `jev.py`: the batch reranker discarded ALL partial results (not just the slow ones) when the outer deadline fired; fixed to keep whatever completed.
- Setup (item 21): `./rag up --check` on a fresh clone, `DATABASE_URL` derived from the real password, pinned searxng tag, and a `--web` opt-in for doctor's web smoke test are all in place and covered by `tests/test_setup.py`. Hardcoded dev ports in `scripts/run_local.sh`/`vite.config.ts` were fixed in the follow-up pass.
- A second-pass critic review of this session's own changes found and fixed several new issues before they shipped: an `/answer` history limit that would 422 a long Quick-answer chat instead of trimming it; an embedder warm-up failure that could permanently disable dense search; a `tune_rerank.py` crash from the same change; an inflight-counter race in the new web-fetch thread pool; and a Quick-answer read-timeout that could kill a healthy slow stream early. See git history for details.
- A real, measured eval: `docs/eval/siva_purana.jsonl` + `scripts/chunk_eval.py`, run live against the ingested PDF -- 8/8 page-hit queries passed, 235-392ms (Jev rerank) / 544-850ms (local cross-encoder fallback, see below).
- Admin UI for Quick-answer LLM configuration and the Local-only-enforced lock (previously only settable by editing `.env`).

## This session's SECOND follow-up pass (Opus-triaged "best in class search" items)
Opus (acting as reviewer) was asked to triage the remaining not-started items for actual search-quality impact and cut what didn't matter. It cut the UI component split, setup polish, a separate frozen offline A/B harness, and the broader visual UI pass as low/no value given everything already shipped, and instead found two higher-leverage issues by reading the current code:
- **FIXED: accent/diacritic stripping broke lexical matching.** `bm25.py tokenize()` stripped all non-ASCII characters (`[^a-z0-9\s]`), so real Unicode diacritics (IAST "Viṣṇu"/"Śiva") would have tokenized to garbage ("vi u"); Postgres FTS had the same problem (no `unaccent`). Fixed with NFKD-based accent folding in `bm25.py` (`fold_accents`, plus a length-preserving `_fold_same_length` for `highlight_spans` so offsets stay valid) and a Postgres `unaccent` extension + immutable wrapper function, applied on both the GIN index and the query in `retrieval.py`. On the actual Siva Purana PDF, the real OCR artifact turned out to be different (see next item) rather than true Unicode diacritics, so this fix's impact on THIS corpus was not large -- but it is a correct, general fix verified against real Unicode input, and matters for any future document with genuine accented/transliterated text.
- **FIXED: a real OCR corruption bug found while investigating the above.** The actual Siva Purana PDF's OCR misrecognizes Sanskrit diacritic letters as a literal `$` or `?` (measured: 509 mid-word occurrences, e.g. "Vi$nu" for "Visnu", "$iva" for "Siva"). Fixed in `pipeline.py`'s `_clean_pdf_page` with a safe, high-confidence regex (`[$?]` immediately followed by a letter -- a genuine `$`/`?` is never followed directly by a letter). Re-ingested the PDF and confirmed 0 remaining occurrences; re-ran the eval (still 8/8, best_rank improved on at least one query).
- **ADDED: a local cross-encoder fallback reranker** (`providers.LocalCrossEncoder`, `cross-encoder/ms-marco-MiniLM-L-6-v2`, on-box, no external call). Previously, any search with Jev off (local-only mode, no Jev key, or an admin override) got ZERO reranking -- plain RRF order only. Now falls back to this local reranker by default (`LOCAL_RERANKER_ENABLED=true`), warmed at startup like the embedder, gated by a `ready` check so it degrades gracefully (`cross_encoder_warming`) rather than blocking a request on cold load. Verified live: local-only mode + this fallback hit best_rank=1 on all 7 content queries in the eval set (544-850ms, higher latency than Jev's network-parallel batching but on a CPU-bound, single-box dev machine under load from this session's own processes -- re-measure on a quieter box before trusting the latency number).
- `JEV_RERANK_TOP_N=20` validation against `evaluate.py` (item 6 from the first pass) was NOT done in this second pass either -- still an unmeasured target.

## Not done in this pass (flagged, not fixed)
- Full verse/chapter-aware chunking (page-spanning chunks, section/chapter metadata, a `page_end`/`section`/`seq` schema change) -- only the chunk-size truncation bug and the OCR/accent fixes above were done. Opus's triage explicitly cut this as low-value relative to its cost.
- The frontend's single `ui.tsx` was not split into components (Opus: cut, pure refactor, zero search-quality impact).
- First-run setup polish (`./rag config`, auto-bootstrap) (Opus: cut, does nothing for search).
- `RRF_WEIGHTS` (bm25=1, dense=1, fts=0.6) and `RERANK_BLEND` have not been retuned against a real baseline -- Opus flagged the two keyword legs (1.6 combined) as possibly outvoting the semantic leg, but this needs `evaluate.py`/`tune_rerank.py` numbers before changing, not a guess.
- An embedding-model upgrade (e.g. `BAAI/bge-small-en-v1.5`, same 384 dims, 512-wordpiece limit) was flagged by Opus as a stretch goal requiring recalibrated `DENSE_MIN_SCORE`/`DENSE_ALONE_MIN` cutoffs and a full re-ingest -- not attempted.

## Deep Research and token cache: status checklist
Done and tested: planner/search/report/validate/verify pipeline; admin tab; full-page fetch (opt-in, SSRF-hardened); batched Jev grounding
(`ground_batch`); sealed report/plan/route cache with single-flight (`tokencache.py`); Jev complexity routing that skips the LLM planner;
gap-driven reflection retry; markdown download; cache `refresh` flag (API; no UI control yet); `/health` fields (`research_active`,
`token_cache`); route-table smoke tests (`tests/test_routes.py`); structural eval runner (`scripts/research_eval.py`).

Added after Opus feasibility review: Jev verification of table cells (with row/column context, after prose under `GROUND_MAX`); honest report
footer (`stats.pages_fetched`: snippets vs full pages); UI Refresh button (shown only on cached reports; costs a run of quota);
print / save-as-PDF (browser print stylesheet, no server work); opt-in Quick-answer cache (`DIRECT_CACHE_ENABLED`, history-free,
time-sensitive/personal queries bypass; first tests for `direct.py`); A/B instrumentation in `scripts/research_eval.py` (`--arm/--out`).

NOT done, with Opus's reasoning:
- Detached/resumable runs: DEFERRED. Needs sealed owner-scoped Redis event streams (events can hold KB text), a detached task lifecycle that
  survives disconnects but not restarts, and orphan cleanup. Needs its own design session.
- Map-reduce summaries: DEFERRED. Page text is capped at `DEEP_RESEARCH_PAGE_TEXT_CHARS`; map-reduce adds up to ~10 LLM calls with no quality evidence.
- Streamed unverified draft: DROPPED. It would show uncited or injected text before `parse_report` and the drop-rate gate, defeating validation.
- Semantic cache lookup: DEFERRED (needs the embedder in the request path, threshold unmeasured, a plan call is only ~500 tokens).
- Corpus-generation cache keys: DROPPED until a cache holds document-derived material (KB-derived reports are not cached at all).
- Planner-skip threshold (0.25): instrumentation exists but NO A/B has been run; the web is nondeterministic so results would be proxies.
  Run each arm with `TOKENCACHE_ENABLED=false` (the plan cache otherwise bypasses routing for repeated questions).
- The research eval checks structure only; factual quality needs manual rubric review. Table-cell verification depends on Jev judging short cells.
- Page fetch has a residual DNS-rebinding window (documented in webfetch.py).
- The Quick-answer cache is untested against a real provider and off by default.

## Cleanup needed (unchanged from the original QA)
Test users: qa-r1, qa-r2, qa-reader1, qa-reader2, rv_reader, evalr-* (no delete endpoint). Test document "QA Restricted Bonus Matrix". Rotate the TypeSafe and LLM keys, Postgres password and APP_SECRET_KEY (they were in an old working-tree `.env.example`; `.env.bak` holds copies).

## Measured, not met
Original QA numbers (local p95 0.7s, overview p95 8s, local rerank +0.05 nDCG) were not re-measured in this pass. Do not assume they still hold -- re-run `scripts/evaluate.py` and `scripts/overview_bench.py` before citing a number.
