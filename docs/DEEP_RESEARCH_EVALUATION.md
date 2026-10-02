# Deep Research: performance and quality evaluation (2026-10-02)

Honest scope: one machine, one LLM (nvidia/nemotron-3-ultra reasoning model via an OpenAI-compatible endpoint), ddgs scraping for web search,
Jev verification, snippets only (page fetch off), 8 questions (`docs/eval/deep_research.jsonl`), one run per question per arm. Raw rows:
`docs/eval/results_baseline.jsonl`, `docs/eval/results_v3.jsonl`. The numbers are small-sample and run against flaky external providers.
Treat them as diagnosis, not a benchmark. "Structurally clean" (script checks) is NOT factual quality: nobody has hand-graded these reports.

## 1. Measured results

| | Baseline | After improvements (v3) |
|---|---|---|
| Runs producing a report | 6 / 8 | 6 / 8 (different failures) |
| Mean time, successful runs | 107 s | 106 s |
| Mean sources per run | 6.2 | 11.0 |
| Claims judged supported by Jev | 52 / 101 (51%) | 75 / 136 (55%) |

An intermediate run (v2) produced reports for only 3 / 8 because the search provider and LLM were failing (12 search failures, 503s and timeouts
in the log); it is not used for comparison.

**Where time goes (baseline, successful runs):** planning 9-30 s, web search 1-2 s, report writing 33-146 s (55-85% of the run), Jev
verification ~0.3 s. Latency is the report LLM, not search or verification. A faster non-reasoning model for the report is the biggest latency lever
and is a configuration change (Admin > Deep Research).

**Failures seen:** report LLM errors (503/timeouts), search provider throttling (`DDGSException`, 41 failures in the v3 log), one report rejected for
uncited claims. These are provider reliability problems, partly mitigated below, not removed.

**Per-question like-for-like (supported/checked):** dr1 11/19 -> 8/36, dr2 10/18 -> 14/23, dr3 8/18 -> 30/36, dr8 1/3 -> 16/27. Mixed, and the number of
claims checked changed (table cells are now checked, more sources), so the ratios are not directly comparable. No claim of a quality improvement is made.

## 2. What was found and changed (Opus reviewed, Sonnet implemented, 269 tests pass)

| Finding | Evidence | Change |
|---|---|---|
| Planner silently degraded to one query | 3 of 6 baseline planner calls hit the 30 s limit; those runs had 4 sources | Plan timeout 60 s, token budget 1500 (reasoning models spend tokens thinking), wider fallback plan (2 queries, 2x sources) |
| Only 'yahoo' ever queried | `bing` is not an available ddgs engine | Default engines `yahoo,duckduckgo` (tested working at the time) |
| Grounding saw ~150 chars of each source | `ground_batch` gave 20 claims 4,500 chars | Claims checked against the best-matching passage (600 chars) of the cited source; 6 claims per Jev request |
| Fetched pages kept only the intro | first 2,500 chars | When page fetch is on: 20k extracted, best passages chosen per sub-question (`webfetch.relevant_excerpt`), MAX_PAGES 20 |
| `stats.llm_calls` hard-coded to 2 | code | Counts real calls (1 when planner skipped/cached, 3 with repair/retry) |
| Report rejected -> nothing | observed | One repair call (only adopted if better; same gate applies); one retry on transient report-LLM failure |
| Reasoning-model `<think>` text could break parsing | review | Stripped before parsing |
| Table cells without own citation were unchecked | review | Cells inherit row citations, marked `rowcited`, and are verified |
| One site could fill the source list | review | Max 3 sources per domain per run |
| Breaker-paused sub-questions never retried | review | Retried in the reflection step when breakers recover |

## 3. Comparison with other platforms (documented features only; no output quality was measured)

We could not run the other products, so this compares published design, not results. Sources reviewed by Opus: OpenAI deep research guide, Gemini
Deep Research docs, Anthropic multi-agent research write-up, Perplexity Sonar Deep Research, GPT Researcher, LangChain open_deep_research, STORM.

| Capability | Us | Others (documented) | Position |
|---|---|---|---|
| Planning | one LLM plan, Jev skips it for simple questions | OpenAI clarify step; Gemini lets the user review the plan | behind (no plan editing) |
| Iterative search | one bounded retry for empty sub-questions | agents reflect after each tool result | behind, by design (bounded cost) |
| Pages read | snippets by default; optional fetch with passage selection | OpenAI open_page / find_in_page; Gemini URL context | behind by default |
| Citation enforcement | every sentence and cell cited, uncited dropped, reject gate, Jev check, honest labels | inline annotations; none documents a reject gate | ahead on enforcement |
| Tables | validated restricted markdown | Gemini adds charts; OpenAI code interpreter | equal (no charts) |
| Conflicting sources | prompt-level only | Co-STORM moderator | weak everywhere |
| Private data | ACL-checked KB, consent, KB reports never cached | file search / connectors | ahead on ACL and privacy |
| Cost / latency control | max 3 LLM calls, caches, quotas, deadline | tool-call caps, effort levels | ahead |

Not copied on purpose: open-ended multi-agent loops (about 15x tokens, hard to reproduce), server-retained background runs, code-interpreter sandboxes,
streaming unvalidated drafts, competing on source count.

## 4. Still open (ranked)
1. Use a faster non-reasoning model for the report call, or a second provider for failover; measure again. 2. Give search a reliable provider
(Brave API key or SearXNG): ddgs throttles under bursts. 3. Hand-grade the reports against the rubric and build a small judged set. 4. Turn
page fetch on after an egress decision and A/B it with `scripts/research_eval.py --arm`. 5. User-editable plan before searching. 6. Repeat the eval
3+ times per arm to see run-to-run variance before trusting any difference.
