# CASE-OPS backend

Evidentiary ingestion, de-noising and chronology API for trial lawyers. Core rule from the PRD, enforced in the
database schema: **every insight (timeline event, contradiction, deadline) links to a raw source line** (a document line
with page/bounding box, or an audio segment with a timestamp). Foreign keys to `doc_lines` are `NOT NULL`, so an insight
without proof cannot be stored.

```
backend/      FastAPI app (app/), Alembic migrations, pytest suite (tests/), Dockerfile
ai_engine/    extraction providers (mock + local PDF/text/OCR/speech), date finder, contradiction-proposal validation
infrastructure/   docker-compose.yml (nginx + API + Postgres 16), nginx/ (TLS, rate limits, same-domain proxy)
```

## Run it

Local, no Docker (SQLite, HTTP, development only):

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export PYTHONPATH=.:..                      # backend/ and the repo root (for ai_engine)
export CASEOPS_ENV=development CASEOPS_COOKIE_SECURE=false CASEOPS_AUTO_CREATE_TABLES=true
export CASEOPS_DATABASE_URL=sqlite:///./dev.db
export CASEOPS_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export CASEOPS_WEBHOOK_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export CASEOPS_CORS_ORIGINS='["http://localhost:8080"]'
python -m app.cli create-user --email lawyer@organization.com --role admin   # prompts for a 12+ char password
uvicorn app.main:create_app --factory --port 8000                            # docs at /docs (dev only)
```

PowerShell equivalents: `$env:NAME = "value"` instead of `export`, and `.\.venv\Scripts\python.exe` instead of activating.
For OCR install Tesseract (`apt install tesseract-ocr poppler-utils`); without it scans stay `awaiting_extraction`.

Production-shaped (Postgres, TLS, one domain): follow the numbered steps at the top of `infrastructure/docker-compose.yml`
(`migrate`, `create-user`, `up`, then schedule `reap`). Copy `.env.example` to `.env` first.

Tests: `cd backend && python -m pytest -q` (80 tests; two migration tests need Postgres and are skipped on SQLite).
Set `CASEOPS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@host/db` to run everything on Postgres (use a throw-away
database: tests create and drop tables). CI does both. OCR tests need `tesseract` and `pdftoppm` and skip without them.

## What extraction does (`CASEOPS_INGESTION_PROVIDER=local`, the default)

| Upload | Result |
|---|---|
| `.txt` | every physical line is a proof line (blank lines kept so "line N" matches the file). WhatsApp/chat exports give one event per timestamped message with the speaker as actor; other text yields an event per line holding an explicit date |
| `.pdf` | text layer read with pdfminer: one proof line per text line with page and bounding box (PDF points, origin top-left). A PDF with no text layer is rasterised (`pdftoppm`) and OCR'd with Tesseract |
| `.jpg` `.png` | Tesseract OCR: line boxes in pixels, per-line and overall confidence |
| `.wav` `.mp3` | faster-whisper transcription when installed (`pip install -r requirements-asr.txt`): one proof line per segment with `start_sec` |

- Events come only from **explicit dates** in the text. Numeric dates like `03/04/2026` are ambiguous, so they are read with
  `CASEOPS_INGEST_DATE_ORDER` (`mdy` default, or `dmy`); a part above 12 always settles it. Dates without a time are placed at
  12:00 in the case's timezone. All events start as `pending`.
- If an engine is missing, the document stays `awaiting_extraction` with metadata-only proof lines that say why. A corrupt or
  empty file ends as `failed`. Text is never invented.
- **Contradictions** are never produced by rules. Enter them yourself (`POST /cases/{id}/contradictions`), or enable
  `CASEOPS_CONTRADICTION_PROVIDER=anthropic` (needs `CASEOPS_ANTHROPIC_API_KEY` and `CASEOPS_CONTRADICTION_MODEL`) and call
  `POST /cases/{id}/contradictions/scan`. **That sends case evidence text to the provider** - check confidentiality and
  privilege obligations first. Every proposal must cite two real lines and quote each verbatim; anything else is discarded.
  Survivors are stored as `origin=ai, review_state=unreviewed` until a lawyer confirms or dismisses them.

## API (`/api/v1`)

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/login` | email + password (+ `totp` if MFA is on; `401 mfa_required` asks for it). Sets httpOnly `co_session`; returns `csrf_token` |
| GET | `/auth/me` | current user + a fresh `csrf_token` (call after a page reload) |
| POST | `/auth/logout` | revokes every session of the user |
| POST | `/auth/password` | change own password (revokes all sessions) |
| POST | `/auth/mfa/setup`, `/enable`, `/disable` | TOTP enrolment (authenticator app) |
| GET/POST | `/users`, `/users/{id}/disable`, `/enable`, `/reset-password`, `/reset-mfa` | administrators only |
| GET/POST | `/cases` | list my cases / create (attorney or admin) |
| GET/POST/DELETE | `/cases/{id}/members` | owners share a case with an existing active user by email |
| GET | `/cases/{id}`, `/summary`, `/timeline`, `/deadlines`, `/documents` | case-scoped reads |
| GET | `/cases/{id}/search?q=` | grounded search: every word must appear in a real line; each hit carries its source |
| GET/POST | `/cases/{id}/contradictions` | list (dismissed hidden) / create manually (two line ids) |
| POST | `/cases/{id}/contradictions/scan`, `/{cid}/review` | model proposals (opt-in) / `{"decision":"confirm"\|"dismiss"}` |
| POST | `/cases/{id}/deadlines` | needs `trigger_line_id` (proof) and exactly one of `rule_days` / `rule_months` |
| POST | `/cases/{id}/documents` | multipart `file`; 202 + background ingestion (poll the document's `status`) |
| GET | `/cases/{id}/documents/{doc}`, `/download` | metadata / attachment download (audited) |
| GET | `/cases/{id}/proofs/{line_id}` | the raw source behind any insight: page lines + `highlight_index`, bbox, or audio `start_sec` |
| GET | `/cases/{id}/audit`, `/audit/verify` | hash-chained ledger / integrity check |
| POST | `/webhooks/ingestion` | HMAC-signed callback from an external ingestion worker |

**Frontend contract:** call with `credentials: "include"`; keep `csrf_token` in memory and send it as `X-CSRF-Token` on every
POST/DELETE. After a reload, call `/auth/me` to get it again. Same domain (the nginx setup) needs no CORS. If the frontend and
API are on different *sites*, add the origin to `CASEOPS_CORS_ORIGINS` and set `CASEOPS_COOKIE_SAMESITE=none` (HTTPS required).

## Security model

- **Passwords:** argon2id; unknown emails still run a hash so timing doesn't reveal accounts; identical 401 for every failure.
- **Brute force:** failures (wrong password *or* wrong MFA code) are counted per sha256(email) in the database; 5 free attempts,
  then 30 s doubling to 15 min, `429` + `Retry-After`. A per-IP limiter in the app and in nginx floods bound the rest.
- **MFA:** RFC 6238 TOTP; secrets are encrypted at rest with a key derived from `CASEOPS_SECRET_KEY` (rotating it means re-enrolling), and a code works once.
- **Sessions:** short-lived JWT (HS256, algorithm pinned) in an httpOnly, Secure cookie. Logout, password change, disabling a user and MFA reset bump `token_version`, killing replayed tokens.
- **CSRF:** every state-changing request needs `X-CSRF-Token` = HMAC bound to the session, checked in the auth dependency (cannot be forgotten per route). CORS is an explicit allow-list; wildcards are refused at startup.
- **SQL injection:** all queries use SQLAlchemy bound parameters; search escapes LIKE wildcards. Tests fire injection payloads at login, search and text inputs.
- **Authorization:** every case route resolves membership first; non-members get `404` (IDOR-safe). Proof lookups are re-checked against the case. Sharing is by email among active users of one organisation (single-tenant model: an owner can learn whether an email has an account).
- **Uploads:** streamed with a size cap (middleware + per-file), SHA-256 while writing, extension allow-list **and** magic-byte check, random storage keys, duplicate detection per case, files served as `attachment` + `nosniff`. PDFs/images are parsed with page, pixel and time limits.
- **Audit ledger:** append-only rows chained by SHA-256, serialized per case (Postgres advisory lock + unique `(chain_key, seq)`); `/audit/verify` detects edits and deletions. After `migrate`, database triggers also **refuse UPDATE/DELETE/TRUNCATE** on `audit_log` (a superuser can still drop the trigger: use a restricted DB role for the app).
- **Language model:** off by default; evidence is sent as delimited data, the model has no tools, and its output must pass verbatim-quote validation. Its proposals are labelled and unreviewed until a human confirms.
- **Config:** no default secrets; the app will not start without them, with short ones, with insecure cookies in production, or with wildcard CORS.
- **Webhooks:** HMAC-SHA256 over `timestamp.body`, 5-minute window, authenticated before the body is parsed.

## Operations

- **Schema:** `python -m app.cli migrate` (Alembic). `init-db` is a development shortcut without migrations or audit triggers.
- **Stuck jobs:** ingestion runs as an in-process background task, so a crash can leave a document in `queued`/`processing`.
  Schedule `python -m app.cli reap` (default: untouched for 15 min) to re-run them.
- **Backups:** back up Postgres **and** the uploads volume together; hashes in the database let you prove a file is unchanged.

## Known limits (be aware before real client data)

- **Extraction quality:** OCR accuracy depends on scan quality (the confidence figures are Tesseract's own); only English is installed in the image; handwriting is not reliably read. Audio needs the optional faster-whisper install and downloads a model on first use. Dates in prose are found by pattern, not understood: always review `pending` events.
- **No summarisation or semantic search:** search is literal word matching. There is no embedding/RAG pipeline (`ai_engine/pipelines/rag_search.py`, `summarization.py` are empty).
- **Language-model contradiction scan is untested against the live API** (it is tested with a mocked transport). It looks at the first 150 dated lines of a case per run.
- **Deadline rules are data, not law:** `rule_verified` is `false` for every deadline until counsel confirms the days/months and citation. The calculator only does date arithmetic.
- **Background tasks run in-process** (single node); move to a real queue before scaling out. In-app rate limits are per process.
- **Local file storage:** fine for one host. Use encrypted object storage and malware scanning before multi-node or production use.
- **No password-reset email flow:** administrators reset passwords and MFA. No SSO.
- **Not verified here:** the Docker image build, nginx configuration and the live Anthropic API. The test suite itself passes on SQLite and on Postgres 16.
