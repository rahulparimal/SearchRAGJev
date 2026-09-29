# Build prompt for a Jev assisted RAG search application

You are a coordinated software delivery team. **GPT Astra** is the architecture lead and writes the design, contracts, acceptance criteria and tradeoff decisions. **Luna** is the implementation agent and owns working code, migrations, Compose, documentation and fixes. **Sol** assists Luna with coding, debugging and focused implementation tasks as needed. An independent critic exercises the system, reports reproducible defects with severity, exact steps, expected versus actual behavior, and test output to Luna; Luna fixes them; the critic retests each fix. Continue this loop until all acceptance criteria pass. Do not claim to have executed a role or test unless it actually ran.

## Goal and key constraint

Build a production oriented, locally runnable document search and grounded RAG application with a polished React interface, admin ingestion, Qdrant, PostgreSQL and open source defaults. Jev from TypeSafe AI is a **typed decision service** (Choice, Score, Noul). Use Jev to classify documents, select chunk profiles, route queries, prioritize candidates when justified by benchmarks, and assess answerability. Do **not** pretend Jev directly parses files, generates chunks, emits embedding vectors, or writes answers unless an official endpoint for that capability is documented and verified. Implement those functions with swappable adapters. Distinguish measured improvements from hypotheses.

## Product experience

Make a responsive React/TypeScript search UI with accessible keyboard navigation, quick search, document and answer tabs, facets, saved searches, query suggestions, highlights, ranked result cards, page aware snapshot preview, citation links, and an evidence side panel. Evaluate whether an existing open source search UI or Open WebUI extension actually supports this workflow; reuse it only if admin ingestion, ACL, result snapshots and citations can be met cleanly. Otherwise implement the React UI. Do not present a generic chat box as the search interface.

Admin UI: username/password login, first login rotation, role separation, upload and bulk upload PDF/DOCX/PPTX/TXT/MD/HTML, collection management, ACL assignment, ingest job monitoring, extraction and chunk preview, reprocess, version replacement, delete, provider health and masked config, evaluation runs and audit view. Use Argon2id, secure HttpOnly sessions, CSRF defenses, login throttling; offer SSO/MFA extension. Secrets remain server side.

## Stack and contracts

React/Vite frontend; FastAPI backend and worker; PostgreSQL for users, documents, versions, jobs, saved searches, eval runs and audit; Qdrant for dense and sparse hybrid retrieval; Redis for Celery queue; MinIO for originals and immutable citation snapshots. Use Docling or Unstructured for parsing, configurable OCR for scans, Sentence Transformers as the local embedding default, and a cross encoder as default reranker. Use Docker Compose, migration tooling, OpenAPI, structured logs and OpenTelemetry hooks.

Define versioned interfaces: `DocumentParser`, `ChunkPolicy`, `EmbeddingProvider`, `Retriever`, `Reranker`, `DecisionProvider`, `GenerationProvider`, `OCRProvider`, `ExtensionHook`. Provide local and configurable remote embedding/reranking adapters and an OpenAI compatible custom LLM plus Ollama generation adapter. Configuration must support model name, endpoint, timeout, API key environment variable, dimension, collection and fallbacks. Support `TYPESAFE_API_KEY` and discover available Jev models from the official model endpoint. Validate config and provider capabilities on startup; never send secrets to the browser. Expose extension hooks `pre_ingest`, `post_extract`, `pre_index`, `pre_query`, `post_answer`, each with bounded execution and audit.

## Ingestion and retrieval

Preserve headings, tables, source page, offsets, version, extraction confidence and ACL. Start with structure aware chunks of 350–650 tokens and 60–100 overlap, tune with labeled evaluation. Derive stable chunk IDs, content hashes and versioned snapshots. Index new versions in isolation and switch active pointer only after successful completion; remove stale chunks under retention policy. Pin embedding model and vector dimensions per collection; migrate through new collection plus alias swap.

At query time enforce tenant and document ACL in retrieval and preview. Use Jev Choice for intent and collection routing with an explicit unknown option and confidence fallback. Combine dense and sparse retrieval, fuse ranks, rerank bounded top candidates, and deduplicate adjacent spans. Answer mode uses only retrieved permitted evidence, cites document version and page, rejects invalid citations, and abstains when evidence is insufficient. Jev Score relevance ranking is optional behind a feature flag; compare against a cross encoder for nDCG, latency, stability and cost before adoption.

## Astra architecture review: make Jev a measurable decision layer

Implement the following as individually switchable policies. Every Jev request supplies a small, explicitly selected state, typed questions, a pinned model version, a timeout and an audit correlation ID. Store decision type, selected option or score, confidence or probability where provided, policy version, latency and fallback outcome. Do not log raw confidential text by default. Use one clear judgment per question; combine several judgments with deterministic code. Never ask Jev to compute arithmetic, enforce ACL, create vectors or generate prose.

| Point in workflow | Jev primitive and decision | Action and safe fallback |
| --- | --- | --- |
| Upload classification | Choice among `manual`, `specification`, `policy`, `report`, `drawing`, `other` | Suggest parser and chunk profile; unknown or weak result uses the default parser and sends a review flag. |
| Extraction quality | Noul: “Is this extracted passage coherent enough for indexing?” | Route suspicious OCR to an admin review queue; keep a deterministic empty text and OCR confidence check. Do not discard source content solely on Jev output. |
| Section treatment | Choice among `narrative`, `table`, `code`, `metadata`, `other` | Select tested section chunk policy; split actual text with deterministic code while retaining heading, page and offsets. |
| Query intent | Choice among `known_item`, `fact`, `comparison`, `exploratory`, `navigation`, `other` | Choose search profile and whether to offer answer mode; low confidence falls back to broad hybrid search. |
| Query reformulation gate | Noul: “Would a second retrieval using a rewritten query materially help?” | Invoke a configured LLM rewriter only when budget and policy allow; compare recall gain against extra latency. Jev does not write the reformulation. |
| Candidate fit | Score using a task specific ordered rubric such as `irrelevant`, `partially_relevant`, `directly_answers` | Optionally rerank only a bounded shortlist after ACL filtering; retain cross encoder baseline and do not infer that a typed Score is a calibrated retrieval probability. |
| Evidence sufficiency | Noul: “Do these permitted passages contain enough evidence to answer the user’s question?” | Generate only above a threshold calibrated on answerable and unanswerable queries; otherwise show cited search results and say evidence is insufficient. |
| Post answer review | Separate Noul judgments for “Does every material claim have supporting evidence?” and “Is the answer responsive?” | Reject or revise unsupported answers; validate citation IDs and quoted spans deterministically. Do not treat Jev as a complete factual verifier. |
| Feedback triage | Choice among `retrieval_miss`, `ranking_issue`, `bad_extraction`, `unsupported_answer`, `other` | Send user feedback to the right evaluation bucket; an administrator verifies labels before they alter a gold set. |

Prioritize query intent, evidence sufficiency and feedback triage for the first Jev release. Add upload and section decisions after the deterministic ingestion pipeline is stable. Keep candidate scoring and answer review in **shadow mode** until an A/B evaluation proves value against the existing reranker and groundedness checks. Batch related typed questions when supported, but avoid presenting an entire document or an unbounded candidate list as state. Set explicit budgets for Jev calls per upload and per query.

Build a `JevDecisionPolicy` module with typed request/response validation, retry only for transient errors, circuit breaker, concurrency and rate limits, version pinning, and per-action confidence thresholds. For low confidence, timeout, 429 or invalid response, choose the documented fallback for that decision. Add a `none_of_these` option where a Choice set may be incomplete. Make every policy independent of authorization: ACL checks happen in code before candidate text is sent to Jev and again before display. Treat retrieved text as untrusted data so an embedded instruction cannot change the decision policy.

Evaluation must include a decision specific labeled set: intent accuracy and calibration, OCR review precision/recall, relevance nDCG lift, answerability false positive rate, and feedback agreement with human reviewers. Measure cost and p95 latency at the query level, compare Jev on/off and shadow/active runs using the same corpus snapshot, and tune thresholds separately for each action. Keep an experiment log of decision rubrics and model versions; a model upgrade requires rerunning the regression set before promotion.

## Evaluation and delivery

Create a representative labeled query set and evaluation harness measuring Recall@20, nDCG@10, MRR, zero results, citation precision, faithfulness, answerability and abstention, p50/p95 latency, ingest success and ACL leakage. Save each run's config, corpus/index snapshot, model versions, scores, and failure examples. Include unit tests for version switching and ACL, integration tests with real services, end to end upload to search to citation, and adversarial tests for prompt injection in documents. Use fixtures free of private customer data.

Deliver a runnable repository with Compose, `.env.example`, migrations, seed/bootstrap admin, provider config samples, architecture diagram, API contracts, a technical architecture Word document, an admin guide Word document, screenshots of actual UI, and a README with exact setup and verification commands. Use environment based `TYPESAFE_API_KEY` and `CUSTOM_LLM_API_KEY` placeholders. Never include live secrets. State all assumptions, known gaps and untested behaviors clearly.

**Acceptance:** clean Compose startup; admin can log in and rotate password; upload each supported type and inspect chunks; document becomes searchable only when indexed; reader ACL enforced across search, answer and preview; citations open the exact retained snapshot; a custom OpenAI compatible LLM can be configured without code edits; wrong provider credentials fail clearly; evaluation run is reproducible; critic's reported defects are fixed and retested with evidence.

Official API reference to verify during implementation: https://api.typesafe.ai/docs
