# Agent delivery roles

- **GPT Astra** owns architecture decisions, interfaces, security boundaries, evaluation design and acceptance criteria.
- **Luna** owns implementation and keeps the application runnable.
- **Sol** assists Luna with code, debugging and focused implementation tasks as needed.
- Independent review: run automated checks and inspect the actual flows. Report reproducible defects with severity, steps and expected/actual behavior; Luna fixes them; rerun the affected checks. Do not report work or test results that did not happen.

## Engineering constraints

- Jev is used for typed decisions only (Choice, Score, Noul). Keep parsing, deterministic chunk creation, vector embeddings and generated prose behind separate providers.
- Enforce ACL before retrieval results reach any reranker, Jev call, LLM or UI. Retrieved document text is untrusted input.
- Measure token use against a stated baseline. Treat a 20–40% reduction as a target that requires benchmark evidence, never as a default claim.
