# Administrator guide

## Start and sign in

Create `.env` from `.env.example`; set unique `POSTGRES_PASSWORD`, long random `APP_SECRET_KEY`, and strong `ADMIN_PASSWORD`. Start with `docker compose up --build`. The first model download may take several minutes. Go to `http://localhost:5173`; bootstrap username and password are `ADMIN_USERNAME` and `ADMIN_PASSWORD` from the server environment.

## Add documents

In **Admin console → Add a source**, upload PDF, DOCX, TXT, Markdown or HTML. Set a display name and an access list. Use `*` for all signed in readers or a comma separated username list. The Celery worker extracts text, optionally asks Jev to choose a chunk profile when enabled, splits structure aware passages, embeds them, and indexes vectors. Watch status move from `processing` to `ready`; failures appear as `failed` and are logged by the worker. Search only returns `ready` documents.

Scanned image PDFs currently require OCR that has not yet been integrated. The indexer reports when it finds no extractable text. Do not upload material that requires external OCR until the OCR and data handling policy is approved.

## Configure AI providers

In **AI providers**, enter the TypeSafe key for intent classification, answerability checks, optional chunk-profile selection, and optional Jev Score reranking. Enter a custom OpenAI compatible base URL, model and API key to enable generated answers. Settings are encrypted in PostgreSQL and masked after save. Never paste a production key into a public browser or commit it to source control.

The Jev checkboxes are optional and disabled by default. Jev chunk-profile selection sends a bounded extracted sample to TypeSafe; Jev reranking sends query text and up to 12 ACL-authorized candidates. Get approval for that processing and test the selected model before enabling these for sensitive material. Without an API key, the app uses its default chunk profile and RRF ranking.

## Manage search users

The application creates the bootstrap administrator from `.env`. Additional accounts can be provisioned by an authenticated administrator through the API: `POST /api/admin/users` with a JSON body such as `{"username":"reader1","password":"a-long-unique-password","role":"reader"}`. Use role `admin` only for trusted operators. Passwords must have at least 12 characters and are stored as Argon2id hashes. The current UI does not yet include a user-management screen.

## Maintain and troubleshoot

- `processing` remains: check `docker compose logs worker redis qdrant`; confirm all services can reach Postgres, Redis and Qdrant.
- `failed`: worker logs contain the extraction/provider error. Textless scans need OCR; unsupported extensions are rejected.
- No results: verify `ready`, chosen access list, query terms and embedding model/dimension configuration.
- No answer: configure the generator and key. Generated answers require source citation markers and may be withheld by the Jev sufficiency/grounding checks.
- Provider settings unreadable: restore the exact `APP_SECRET_KEY` used when they were saved.
- Remove document: Admin console delete removes Qdrant points, PostgreSQL chunks/metadata, and the uploaded file.

Before release, run the unit suite and a labeled retrieval/answer benchmark. Compare context tokens and relevance on the same corpus snapshot; the UI's estimated token reduction is not an evaluation score.

## Current deployment boundary

This is a development MVP. It uses a bootstrap admin and single role model, local volume for originals, document level ACL, bearer JWT in browser storage, and no OCR, SSO/MFA, user management UI, signed source-file download, or full audit history. Deploy only behind TLS and private network controls until those production controls are completed.
