# AI Affiliate Studio backend

This is the separate API service for the rebuilt studio. It is intentionally not connected to GitHub Pages yet.

## Current scope

- FastAPI health endpoint.
- SQLite-backed video job records.
- Authenticated job creation and status lookup.
- Docker image definition for a future VPS deployment.

**Important:** this scaffold accepts and records jobs only. It does not render MP4 files yet. The render worker, media pipeline, output storage, and frontend integration are later steps.

## Local development

Python 3.11+:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ALLOWED_ORIGINS="http://localhost:8000"
uvicorn app.main:app --reload
```

Health check: `GET /health`

Create a job: `POST /v1/jobs` with `Authorization: Bearer <API_KEY>`.

Get a job: `GET /v1/jobs/{job_id}` with the same authorization header.

## Deployment safety

- Do not commit real API keys or provider credentials.
- Do not deploy publicly until HTTPS, a strong API key, restrictive CORS, persistent storage, rate limits, and a render-worker timeout policy are configured.
- Mount persistent storage at `/app/data` when running the container.
- GitHub Pages remains the frontend; this API and the FFmpeg/Remotion worker run separately.
