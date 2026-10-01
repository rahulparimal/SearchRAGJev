# Known issues (open QA findings, 2026-10-01)

From an independent adversarial QA of the T1-T9 search redesign. Status: **not fixed** unless noted. Evidence and repro scripts were produced in a session scratchpad; the findings and fixes are stated here so they can be reproduced.

## High
1. **Jev client wedges.** `AsyncJev.decide` wraps the httpx call in `asyncio.wait_for`; cancelled connects leave pool slots in CONNECTING until all 32 are stuck, then every Jev call fails (observed: every search +0.9 s, `rrf_fallback`, overviews withheld). Fix: use `httpx.Timeout` only, recreate the client after repeated timeouts, add a circuit breaker, log failures, pool >= 128. Restart the API to recover.
2. **Slow overviews starve `/search`.** `stream_overview` uses the default thread executor shared with search legs; 25 open overview streams made `/search` take 10.9 s. Fix: async LLM client or a dedicated bounded executor, plus a concurrency cap returning reason `busy`.
3. **Overview success about 55%.** Causes: slow LLM (1 to 10.6 s, 12 s timeout, no retry), grounding threshold 0.75 too strict, bullet fragments graded without context, one poisoned document suppresses a topic. Fix: ground with query context or the whole overview, drop failing bullets, calibrate threshold (about 0.6), retry once, pick a faster non-reasoning model.

## Medium
4. Jev per-candidate rerank collapses under concurrency (16 concurrent: 15/16 fall back). Use the batch reranker, a bigger pool, partial scores, cached scores.
5. Bing ads (`/aclick`) appear as organic web results; filter them and any search-engine hosts.
6. Local fusion loses semantic matches (BM25 counts stopwords; dense leg returns every document). Use content terms, a dense similarity floor, rerank 20 to 30, and a no-relevant-results floor.
7. Web Jev rerank lowers nDCG on the recorded fixtures (0.926 to about 0.84); default `WEB_RERANK_ENABLED=false`.
8. Local rerank gain is +0.028 nDCG against a +0.05 target and is worse on 6 of 54 queries.
9. `mode=local` with `web=true` still triggers the speculative web fetch (privacy).
10. Empty web results trip the circuit breaker; the notice can claim a query was sent when it was not.
11. A Jev outage is shown as `insufficient_evidence` / `unsupported_claim`.
12. Outages were invisible to logs and `./rag doctor`; doctor should fail on a degraded smoke search.
13. Vector search still runs for local routes in agent mode with web on (by design for relevance; skipped for web routes).

## Low
14. Web cache hits miss the 0.15 s target when rerank is cold or failed. 15. Word-count gate miscounts hyphens and apostrophes, and no-space scripts (CJK, Thai). 16. Overview validation withholds on an uncited heading line. 17. Blocking sync Redis/settings work on the event loop; one shared SQLAlchemy session. 18. `evaluate.py` leaves `evalr-*` users (no delete-user endpoint) and flips a global rerank setting. 19. Overview cache key lacks provider URL; restricted evidence is cached as plaintext in Redis. 20. Admin-settable provider URLs can exfiltrate keys; validate and never fall back to another provider's key. 21. Setup rough edges (`./rag up --check` on a fresh clone, example DATABASE_URL appended, unpinned searxng tag, doctor smoke web query unasked). 22. Tests fake Jev and the LLM; no real-pool, starvation or concurrency tests.

## Cleanup needed
Test users: qa-r1, qa-r2, qa-reader1, qa-reader2, rv_reader, evalr-* (no delete endpoint). Test document "QA Restricted Bonus Matrix". Rotate the TypeSafe and LLM keys, Postgres password and APP_SECRET_KEY (they were in an old working-tree `.env.example`; `.env.bak` holds copies).

## Measured, not met
Local p95 0.7 s (met on a fresh process, not on the wedged live API or at 16 concurrent), overview p95 8 s (12.4 s), local rerank +0.05 nDCG (+0.028). Local no-Jev p50 23 ms and web cold p50 about 0.7 s were measured on a healthy process.
