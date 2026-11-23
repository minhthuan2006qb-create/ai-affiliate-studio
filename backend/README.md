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

Drafts with confidence below 70 are automatically placed in `held`. A candidate can enter `pending` only when metadata coverage is at least 70, at least two distinct publisher groups have been explicitly classified as `established` or `official`, and no numeric conflict heuristic fires. Otherwise it remains `held`. These are conservative intake rules, not an AI fact-check or proof of truth. No source polling, AI verification, Meta OAuth, Facebook publishing, or affiliate click tracking is active yet. Auto-publishing is intentionally blocked until a Page has a verified Meta connection.


## RSS source polling worker

The Facebook Hub RSS worker is separate from the Video Studio render worker. Run it alongside the API service:

```bash
cd backend
python -m app.source_worker
```

It checks enabled RSS sources every 60 minutes by default. For a one-time test, run `python -m app.source_worker --once`. You can change the interval with `--interval-minutes 60` (minimum 5 minutes). The worker stores only feed metadata (title, canonical link, short summary, publication timestamp and first-seen timestamp), deduplicated per source. It does not download or archive full linked articles.

Basic safety limits: HTTP(S) only, no URL credentials, reject non-public IP destinations, no redirects, 12-second timeout, 1 MB maximum feed, 100 entries per poll. Add only RSS feeds you are authorized to monitor. RSS parsing and deduplication do not verify the truth of a story and do not publish to Pages.


Operational note: The API and RSS worker must share the same persistent SQLite file. If `FACEBOOK_HUB_DB` is set, configure it identically for both processes; otherwise both use `DATABASE_PATH`. The RSS worker is a separate long-running process and is not started by the API container automatically.


## RSS candidate triage (preliminary only)

The RSS worker also sends newly discovered items through `app.content_processor`. It creates one candidate draft per assigned Page and records a preliminary metadata-coverage signal based on matching titles across distinct hostnames within 72 hours. Low-signal candidates are held; higher-signal candidates enter the pending queue. This is **not AI fact-checking**: hostnames may share ownership, syndicate the same story, or repeat incorrect information. Drafts explicitly state that they are unverified and are never auto-published by this worker.

The processor can also be run separately:

```bash
cd backend
python -m app.content_processor --once
```

The RSS and triage worker remain separate from the Video Studio render worker. No paid AI provider is required for this preliminary intake stage.


## Optional AI-assisted draft rewriting

AI rewriting is opt-in and disabled unless all three environment variables are set: `AI_API_BASE_URL`, `AI_API_KEY`, and `AI_MODEL`. The endpoint must be HTTPS (HTTP is accepted only for localhost development) and expose an OpenAI-compatible `/chat/completions` API. Keep provider credentials only in the backend environment; never enter them in the browser UI or commit them.

The worker sends only RSS publisher names, titles and short excerpts to the configured provider. It asks for a cautious Vietnamese rewrite and forbids adding unsupported details. Original source links are appended by the backend, not generated by the model. AI output remains unverified and requires human review; this feature never approves or publishes a draft. Leave the variables blank for a fully disabled, no-paid-provider setup.

Run a one-off rewrite batch from the backend directory:

```bash
python -m app.ai_writer --limit 5
```


## Page-specific editorial profiles

Each Facebook Page can store its own writing tone, target audience, preferred topics, topics/styles to avoid, and additional editorial guidance. Configure these in the Facebook Hub under **Quy tắc nội dung riêng cho từng Page**. The AI writer includes that profile in its rewrite prompt, while retaining the same no-invention and human-review safeguards.

The profile is managed through authenticated endpoints:

- `GET /api/facebook-hub/pages/{page_id}/editorial-profile`
- `PUT /api/facebook-hub/pages/{page_id}/editorial-profile`

Existing databases are upgraded additively when the Hub initializes; existing Page records receive neutral defaults. Editorial preferences guide style and topic selection only and do not count as evidence that a claim is true.


## Source trust registry and conflict warnings

Each source has a configurable trust profile in the Hub UI: `unverified`, `community`, `established`, or `official`; a publisher-group identifier; an explicit official-source flag; and a note explaining the basis for the classification. Use the same publisher-group value for outlets under the same owner, network, or syndication group. If no group is entered, the source hostname is used as a fallback and must not be interpreted as proof of editorial independence.

A candidate enters the pending review queue only if it has metadata coverage of at least 70, at least two distinct publisher groups classified as `established` or `official` (or explicitly flagged as official), no detected numeric conflict, and a publication timestamp no older than 72 hours or more than 6 hours in the future. Missing or invalid publication timestamps are recorded as unknown; they do not by themselves prove that a story is fresh. Unclassified sources do not count as trusted independent groups. Conflicting numeric claims with the same unit (for example, different attendee counts) force the candidate to `held`. These checks are deliberately conservative heuristics: they do not establish source ownership, semantic agreement, or factual truth. A human must still verify the original sources before approval. This pipeline never auto-publishes.


### Freshness and duplicate-news safeguards

The candidate processor checks the feed item's publication timestamp when it is available. Items older than 72 hours, or dated more than 6 hours into the future, are held with an explanation. If a feed omits a valid publication timestamp, freshness is marked unknown and the item still has to pass the other conservative checks. This timestamp rule is a triage heuristic, not a guarantee that the underlying event is still current. Similar headlines are clustered within a 72-hour first-seen window; the processor does not yet perform semantic event resolution or detect every paraphrased duplicate.


### Per-Page duplicate history

- When a draft is approved, its normalized source URL, title, body, Page ID, and approval time are stored in `fb_page_content_history`.
- New drafts on that same Page are held when the source URL matches after removing common tracking parameters (such as `utm_*` and `fbclid`), or when normalized title/body token similarity reaches the conservative near-duplicate threshold.
- Duplicate history is isolated by Page: the same story may be independently used by another Page with a different audience or editorial profile.
- Revoking approval removes that draft from the history. The review endpoint also rejects approval if another approved item on that Page is already a match.
- This is a heuristic duplicate guard, not semantic fact-checking. A genuinely updated story can still be flagged and should be reviewed manually; there is not yet an explicit “publish as update” override.
- This records approval in the Hub only. It does not mean the post has been published to Facebook; Meta OAuth and actual publishing are still not implemented.


#### Updated-story handling

- A candidate matching an already-approved story is not automatically treated as a duplicate if its title/body adds at least five new meaningful tokens and those tokens represent at least 25% of the candidate's normalized vocabulary.
- This rule is a conservative heuristic to let substantial updates (for example, a newly announced time, venue, lineup, or ticket detail) reach review.
- Minor wording changes and repeated source URLs without meaningful new information remain held.
- The rule does not verify whether new details are true. The existing source-trust and evidence checks still apply, and a human should review the update before any publication.


## Per-Page publishing schedule queue

The Facebook Hub now supports a separate internal schedule for each Page:

- Configure timezone, allowed posting times, maximum posts per day, and minimum spacing.
- Pause or enable schedule generation independently for each Page.
- The planner selects only drafts with `review_status='approved'`, prioritizes higher confidence, and never queues pending/held/rejected drafts.
- Existing queued slots count toward daily limits and minimum spacing. A draft can appear only once in the queue.
- Queue items can be listed and cancelled through the schedule API.

Endpoints (all require the Hub API key):

- `GET/PUT /api/facebook-hub/pages/{page_id}/schedule-settings`
- `POST /api/facebook-hub/pages/{page_id}/schedule/plan` with `{"days":7}` (1–30 days)
- `GET /api/facebook-hub/pages/{page_id}/schedule`
- `PATCH /api/facebook-hub/schedule/{schedule_id}?state=cancelled`

**Important:** these are planned slots in the local database only. They do not publish to Facebook, do not call Meta APIs, and do not change a draft's review status to published. Meta OAuth and a separate controlled publishing worker are still required for real posting.


### Schedule queue safety synchronization

- Revoking a draft's approval (moving it to pending, held, or rejected) automatically cancels its queued or paused slot.
- Disabling a Page schedule pauses its queued slots without deleting them.
- Re-enabling the Page restores only slots whose drafts are still approved; stale slots are cancelled.
- These transitions affect only the internal queue. No Facebook publishing is triggered.



### Due-schedule safety worker (internal only)

Run this as a separate backend process using the same persistent SQLite database as the API:

```bash
cd backend
python -m app.facebook_scheduler
```

For a one-time check:

```bash
python -m app.facebook_scheduler --once
```

The worker checks due queue entries every 60 seconds by default (minimum 10 seconds), revalidates Page enabled state, Page schedule enabled state, draft approval, content snapshot hash, and duplicate history, then changes eligible entries from `queued` to `ready`. Failed checks move the entry to `blocked`. The worker does not call Meta APIs, does not publish, and does not mark drafts as published. A future Meta publishing worker must perform a fresh final validation immediately before sending each post; `ready` is not proof that Facebook accepted a post.
