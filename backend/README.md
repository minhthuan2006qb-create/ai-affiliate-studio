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
