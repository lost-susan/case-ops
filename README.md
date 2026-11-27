# CASE-OPS backend (milestone 1)

Evidentiary ingestion, de-noising and chronology API for trial lawyers. Core rule from the PRD, enforced in the
database schema: **every insight (timeline event, contradiction, deadline) links to a raw source line** (a document line
with page/bounding box, or an audio segment with a timestamp). Foreign keys to `doc_lines` are `NOT NULL`, so an insight
without proof cannot be stored.

```
backend/      FastAPI app (app/), pytest suite (tests/), Dockerfile
ai_engine/    ingestion provider contract + MockProvider (OCR/ASR/extraction plug in here)
infrastructure/docker-compose.yml   Postgres 16 + API
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
python -m app.cli create-user --email lawyer@organization.com --role attorney   # prompts for a 12+ char password
uvicorn app.main:create_app --factory --port 8000                               # docs at /docs (dev only)
```

Docker (Postgres): copy `.env.example` to `.env`, fill in the three secrets, then follow the commands at the top of
`infrastructure/docker-compose.yml` (`up`, `init-db`, `create-user`).

Tests: `cd backend && python -m pytest -q` (45 tests, SQLite). Set `CASEOPS_TEST_DATABASE_URL` to run them on Postgres;
the CI workflow does both.

## API (`/api/v1`)

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/login` | email + password. Sets httpOnly `co_session` cookie; returns `csrf_token` |
| GET | `/auth/me` | current user + a fresh `csrf_token` (call after a page reload) |
| POST | `/auth/logout` | revokes every session of the user |
| GET/POST | `/cases` | list my cases / create (attorney or admin) |
| GET | `/cases/{id}`, `/summary`, `/timeline`, `/contradictions`, `/deadlines`, `/documents` | case-scoped reads |
| POST | `/cases/{id}/deadlines` | needs `trigger_line_id` (proof) and exactly one of `rule_days` / `rule_months` |
| POST | `/cases/{id}/documents` | multipart `file`; 202 + background ingestion |
| GET | `/cases/{id}/documents/{doc}` , `/download` | metadata / attachment download (audited) |
| GET | `/cases/{id}/proofs/{line_id}` | the raw source behind any insight: page lines + `highlight_index`, bbox, or audio `start_sec` |
| GET | `/cases/{id}/audit`, `/audit/verify` | hash-chained ledger / integrity check |
| POST | `/webhooks/ingestion` | HMAC-signed callback from an external ingestion worker |

**Frontend contract:** call with `credentials: "include"`; keep `csrf_token` in memory and send it as `X-CSRF-Token` on every
POST/PUT/DELETE. After a reload, call `/auth/me` to get it again. Add the frontend origin to `CASEOPS_CORS_ORIGINS`.
If the frontend and API are on different *sites*, set `CASEOPS_COOKIE_SAMESITE=none` (HTTPS required).

## Security model

- **Passwords:** argon2id; unknown emails still run a hash so timing doesn't reveal accounts; identical 401 for every failure.
- **Brute force:** failures are counted per sha256(email) in the database (unknown emails too); 5 free attempts, then 30 s doubling to 15 min, `429` + `Retry-After`.
- **Sessions:** short-lived JWT (HS256, algorithm pinned) in an httpOnly, Secure cookie. Logout bumps `token_version`, killing replayed tokens.
- **CSRF:** every state-changing request needs `X-CSRF-Token` = HMAC bound to the session, checked in the auth dependency (cannot be forgotten per route). CORS is an explicit allow-list; wildcards are refused at startup.
- **SQL injection:** all queries go through SQLAlchemy bound parameters; there is no string-built SQL. Tests fire injection payloads at login and every text input.
- **Authorization:** every case route resolves membership first; non-members get `404` (IDOR-safe). Proof lookups are re-checked against the case.
- **Uploads:** streamed with a size cap (middleware + per-file), SHA-256 while writing, extension allow-list **and** magic-byte check, random storage keys (client filename is display-only), duplicate detection per case, files served as `attachment` + `nosniff`.
- **Audit ledger:** append-only rows chained by SHA-256, serialized per case (Postgres advisory lock + unique `(chain_key, seq)`); `/audit/verify` detects edits and deletions.
- **Config:** no default secrets; the app will not start without them, with short ones, with insecure cookies in production, or with wildcard CORS.
- **Webhooks:** HMAC-SHA256 over `timestamp.body`, 5-minute window, authenticated before the body is parsed.

## Known limits (be aware before real client data)

- **Mock extraction:** `MockProvider` records file metadata and one `pending` event. There is no OCR, transcription, event extraction or contradiction detection yet; `ai_engine/pipelines/extraction.py`, `rag_search.py` and `summarization.py` are still empty. Contradictions are only created by inserting rows.
- **Deadline rules are data, not law:** `rule_verified` is `false` for every deadline until counsel confirms the days/months and citation. The calculator only does date arithmetic.
- **No migrations yet:** tables come from `init-db` / `create_all`. Add Alembic before the first schema change in production.
- **Background tasks run in-process:** a crash mid-ingestion leaves a document in `processing`. Move `run_ingestion` to a real queue with retries and a reaper when OCR/ASR arrives.
- **Local file storage:** fine for one host. Use encrypted object storage (S3/GCS with SSE) and malware scanning before multi-node or production use.
- **No per-IP rate limiting**, no MFA, no password reset, no user-management API (CLI only): put the API behind a rate-limiting proxy and add these next.
- **Audit table immutability** is detectable, not enforced; add a Postgres trigger or restricted role that forbids `UPDATE`/`DELETE` on `audit_log`.
- Verified on SQLite only in the build environment. The Postgres CI job is configured but its first run is the first real test of Postgres-specific code (advisory lock, JSON, constraints).
