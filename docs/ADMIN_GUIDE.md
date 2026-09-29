# Administrator guide

## Start and sign in

Create `.env` from `.env.example`; set unique `POSTGRES_PASSWORD`, long random `APP_SECRET_KEY`, and strong `ADMIN_PASSWORD`. Start with `docker compose up --build`. The first model download may take several minutes. Go to `http://localhost:5173`; bootstrap username and password are `ADMIN_USERNAME` and `ADMIN_PASSWORD` from the server environment.

## Add documents

In **Admin console → Add a source**, upload PDF, DOCX, TXT, Markdown or HTML. Set a display name and an access list. Use `*` for all signed in readers or a comma separated username list. The Celery worker extracts text, optionally asks Jev to choose a chunk profile when enabled, splits structure aware passages, embeds them, and indexes vectors. Watch status move from `processing` to `ready`; failures appear as `failed` and are logged by the worker. Search only returns `ready` documents.

Scanned image PDFs currently require OCR that has not yet been integrated. The indexer reports when it finds no extractable text. Do not upload material that requires external OCR until the OCR and data handling policy is approved.

## Configure AI providers in tabs

In **Admin console → AI configuration**, use the **TypeSafe Jev**, **Answer LLM**, and **Retrieval** tabs. The Jev tab accepts the TypeSafe API key, base URL, and model. Jev is optional for baseline hybrid search. The Answer LLM tab configures an OpenAI-compatible chat completions endpoint, model ID, and API key. Endpoint and model fields offer common suggestions and accept custom typed values. The API key is optional for local or managed identity endpoints; the URL and model are required to generate answers. Blank key fields preserve saved keys. Provider secrets and settings are encrypted in PostgreSQL and the UI never displays saved keys.

The Retrieval tab shows the active embedding model and vector dimension, lets an administrator set the answer context budget, and enables optional Jev chunk-profile selection or bounded Jev Score reranking. Changing the embedding model or dimension remains a deployment operation and requires a compatible Qdrant collection and reindex. Jev chunk-profile selection sends a bounded extracted sample to TypeSafe; Jev reranking sends query text and up to 8 ACL-authorized candidates. Get approval for that processing and test the selected model before enabling these for sensitive material. Without a Jev API key, the app uses its default chunk profile and RRF ranking.

## Runtime and security settings

In **Admin console → Runtime & security**, review whether required deployment values are present. The page reports configuration status only and never returns secrets. Set `APP_SECRET_KEY`, `DATABASE_URL`, `QDRANT_URL`, `REDIS_URL`, `EMBEDDING_MODEL`, and `EMBEDDING_DIMENSION` in the deployment environment. Set `ADMIN_USERNAME` and `ADMIN_PASSWORD` before the first deployment to create the bootstrap administrator; later password changes are not controlled by those environment values. Set `CORS_ORIGINS` to the production frontend origin.

Generate a unique signing secret with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'` and put the result in the server `.env` as `APP_SECRET_KEY`. The API refuses to start with an example, default, or short secret. Keep the same signing secret while encrypted provider settings need to be read. Rotating it invalidates active sessions and prevents decryption of provider settings written with the previous key. Never commit `.env` or real credentials to GitHub.

## Manage search users

The application creates the bootstrap administrator from `.env`. In **Admin console → Workspace accounts**, an administrator can create a reader or another administrator with a username and initial password. Passwords must have at least 12 characters and are stored as Argon2id hashes. User editing, password resets and deactivation are not yet available in the UI.

## Maintain and troubleshoot

- `processing` remains: check `docker compose logs worker redis qdrant`; confirm all services can reach Postgres, Redis and Qdrant.
- `failed`: worker logs contain the extraction/provider error. Textless scans need OCR; unsupported extensions are rejected.
- No results: verify `ready`, chosen access list, query terms and embedding model/dimension configuration.
- No answer: configure the generator and key. Generated answers require source citation markers and may be withheld by the Jev sufficiency/grounding checks.
- Provider settings unreadable: restore the exact `APP_SECRET_KEY` used when they were saved.
- Remove document: Admin console delete removes Qdrant points, PostgreSQL chunks/metadata, and the uploaded file.

Before release, run the unit suite and a labeled retrieval/answer benchmark. Compare context tokens and relevance on the same corpus snapshot; the UI's estimated token reduction is not an evaluation score.

For a repeatable retrieval run, prepare JSONL cases in the format shown by `scripts/evaluation_template.jsonl`, with human validated relevant document IDs. Set `RAGSEARCH_TOKEN` to an administrator bearer token and run `python3 scripts/evaluate.py --dataset your-gold-set.jsonl --api http://localhost:8000`. Each run writes a timestamped snapshot with Recall@20, MRR, nDCG@20, retrieved IDs/ranks, reranker setting and estimated token reduction. Preserve the same corpus and labels for comparisons; evaluate generated-answer citations separately.

## Current deployment boundary

This development MVP uses browser-stored bearer tokens, local file storage, and document ACLs. OCR, SSO/MFA, user lifecycle tools, signed downloads, and complete audit history are not implemented. Use TLS and private networking; add enterprise identity and audit controls before production.
