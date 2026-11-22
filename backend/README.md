# AI Affiliate Studio backend

This is the separate API service and render worker for the rebuilt studio. It is not connected to GitHub Pages yet.

## Current scope

- FastAPI health endpoint.
- SQLite-backed video job records with authenticated job creation and status lookup.
- Deterministic 9:16 MP4 renderer using Pillow and FFmpeg.
- Three animated text-card scenes: problem hook, product/benefits, and call to action.
- Worker queue processing with `queued`, `processing`, `completed`, and `failed` states.

**Current limitation:** this first renderer creates motion-graphic videos from the user's Vietnamese text. It does not yet create arbitrary AI-generated footage, product-photo scenes, or spoken narration. Those will be separate features; no paid AI provider is enabled.

## Local development

Python 3.11+ and FFmpeg are required:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ALLOWED_ORIGINS="http://localhost:8000"
uvicorn app.main:app --reload
```

In another terminal, from the `backend` directory:

```bash
python -m app.worker
```

Process at most one queued job and exit:

```bash
python -m app.worker --once
```

The worker reads the same `DATABASE_PATH` as the API and writes MP4 files to `OUTPUT_DIR` (default: `backend/output`). For local use, set both processes to the same database and output directory.

## API

- Health check: `GET /health`
- Create a job: `POST /v1/jobs` with `Authorization: Bearer <API_KEY>`.
- Get job status: `GET /v1/jobs/{job_id}` with the same authorization header.

The API currently reports job status; authenticated MP4 download and frontend polling/download integration are still pending.

## Docker

The image installs FFmpeg and Noto fonts. It starts the API by default. Run the worker as a second process/container sharing the same persistent database and output volume, with the same `DATABASE_PATH` and `OUTPUT_DIR` environment values.

## Deployment safety

- Do not commit real API keys or provider credentials.
- Do not deploy publicly until HTTPS, a strong API key, restrictive CORS, persistent storage, rate limits, and worker isolation/timeouts are configured.
- Mount persistent storage for both the database and rendered output.
- GitHub Pages remains the static frontend; this API and FFmpeg worker run separately.


## Facebook Hub V1 API

The Facebook Hub is mounted in the same FastAPI service as the Video Studio API. It uses the same `API_KEY` by default; optionally set `FACEBOOK_HUB_API_KEY` for a separate key and `FACEBOOK_HUB_DB` for a separate SQLite file. If no separate database is configured, Hub tables share the configured `DATABASE_PATH` file without sharing tables.

Management UI: `/facebook-hub.html` (static frontend).

Current endpoints (all require `Authorization: Bearer <key>`):
- `GET /api/facebook-hub/dashboard`
- `POST /api/facebook-hub/pages`, `PATCH /api/facebook-hub/pages/{id}`, `DELETE /api/facebook-hub/pages/{id}`
- `POST /api/facebook-hub/sources`, `DELETE /api/facebook-hub/sources/{id}`
- `POST /api/facebook-hub/pages/{page_id}/sources/{source_id}`
- `GET /api/facebook-hub/pages/{page_id}/sources`
- `DELETE /api/facebook-hub/pages/{page_id}/sources/{source_id}`
- `POST /api/facebook-hub/drafts`, `GET /api/facebook-hub/drafts`
- `PATCH /api/facebook-hub/drafts/{id}/review`

Drafts with confidence below 70 are automatically placed in `held`; scores 70 and above enter `pending` for review. This score is only an intake rule, not an AI fact-check. No source polling, AI verification, Meta OAuth, Facebook publishing, or affiliate click tracking is active yet. Auto-publishing is intentionally blocked until a Page has a verified Meta connection.


## RSS source polling worker

The Facebook Hub RSS worker is separate from the Video Studio render worker. Run it alongside the API service:

```bash
cd backend
python -m app.source_worker
```

It checks enabled RSS sources every 60 minutes by default. For a one-time test, run `python -m app.source_worker --once`. You can change the interval with `--interval-minutes 60` (minimum 5 minutes). The worker stores only feed metadata (title, canonical link, short summary, publication timestamp and first-seen timestamp), deduplicated per source. It does not download or archive full linked articles.

Basic safety limits: HTTP(S) only, no URL credentials, reject non-public IP destinations, no redirects, 12-second timeout, 1 MB maximum feed, 100 entries per poll. Add only RSS feeds you are authorized to monitor. RSS parsing and deduplication do not verify the truth of a story and do not publish to Pages.


Operational note: The API and RSS worker must share the same persistent SQLite file. If `FACEBOOK_HUB_DB` is set, configure it identically for both processes; otherwise both use `DATABASE_PATH`. The RSS worker is a separate long-running process and is not started by the API container automatically.
