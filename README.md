# JEV Gmail Intelligence

A local FastAPI application that progressively loads Gmail metadata and uses
JEV/TypeSafe System 1 to classify only the messages visible in the browser.
Email bodies are retrieved only when a user opens a message and are never sent
to JEV.

## Run locally

Copy `.env.example` to `.env`, fill in the required secrets, and keep the Google
OAuth client file at `google_client_secret.json`.

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` and connect Gmail.

## API

- `GET /emails?max_results=20&page_token=...` — paginated metadata
- `POST /emails/classify` — one metadata-only JEV request per batch
- `GET /emails/{message_id}` — full content, fetched on demand
- `GET /usage` — actual token usage, latency, batching, and configured cost

The default database is local SQLite (`jev.db`). Gmail credentials are encrypted
before they are stored, while classifications, page caches, and JEV usage are
persisted so refreshes and server restarts do not repeat completed JEV calls.
Set `DATABASE_URL` to a PostgreSQL SQLAlchemy URL when you are ready to move the
same repository to PostgreSQL.

The dashboard also provides a **Classify 100 in one call** action. It loads up
to 100 pending metadata records and submits their 500 classification questions
through one JEV `system_one()` request.

## Tests

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```
