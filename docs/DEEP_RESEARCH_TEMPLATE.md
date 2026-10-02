# Deep Research report: template, what is computed and what is written

The report follows the research-findings template (brief, executive summary, findings, evidence assessment, implications,
recommendations, method, source register, appendix). The rule that makes it trustworthy: **anything that can be computed from the run is
computed in code; the model only writes what needs reading the sources, and every factual sentence must cite them.**

| Section | Who produces it | Honesty guard |
|---|---|---|
| Research brief (question, date, evidence base, source dates) | Code | From the run itself |
| Scope and context (decision supported, scope, definitions) | Model | Analysis section: may be uncited, no new numbers/names |
| Executive summary: short answer, key findings | Model | Every sentence cited; uncited sentences removed |
| Executive summary: recommendation | Model | Analysis; or the fixed line "The gathered sources do not support a recommendation." |
| Confidence (High/Medium/Low + reasons) | Code | Rule-based on verified-claim rate, multi-source agreement, number of sources and sites; always Low for an evidence-only page |
| Findings (statement, evidence, supporting bullets, interpretation, limitations) | Model | Evidence/bullets cited; interpretation and limitations are analysis |
| Conflicting evidence, what is not established | Model | Cited, or the fixed "do not report conflicting evidence" line |
| Implications, recommendations table, further research | Model | Analysis; uncited allowed ONLY if it adds no number, date, percentage or name absent from the sources/question (`research_report.new_facts_ok`) |
| Evidence assessment table | Code | Agreement, source types, recency (provider dates), claim-check rate; "Not assessed" where it cannot be measured (e.g. applicability) |
| Research method (providers, search terms, criteria, analysis method, limitations) | Code | Lists the actual queries, counts, removed-statement counts and real limitations (snippets only, gaps, failover used) |
| Source register (ID, source, organisation, year, type, how it supports, reliability notes) | Code | Type and note come from the website address only (`research_sections.classify_source`) and say so |
| Appendix (coverage gaps, run details) | Code | |

## Failure behaviour
A run that found sources never ends as a bare error: an evidence-only report (best-matching quoted sentences per sub-question, labelled "not
synthesised", confidence Low) is sent early as a `partial` event and shown if the writer fails, times out, or its draft has too few cited
statements. If the writer's draft is mostly uncited but enough cited statements survive, it is shown with a banner stating how many statements
were removed.

## Small / local models
A local primary model (loopback or LAN address) gets a compact prompt (executive summary, findings, not established), at most
`DEEP_RESEARCH_LOCAL_MAX_SOURCES` sources, and a smaller token budget. A CPU-only machine running a 7B model usually cannot finish even the
compact report inside the research time limit; the evidence-only fallback is the realistic guarantee there. A hosted non-reasoning model as the
report writer is the recommended configuration.

## Not done
Section-by-section generation for small models, measured-latency timeouts and a pre-flight model probe, a user-editable plan, and charts.
