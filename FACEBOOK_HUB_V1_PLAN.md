# Facebook Automation Hub — V1 implementation plan

## Product decisions
- One central, mobile-first website manages all Facebook Pages.
- Each Page has its own source assignments; a source may be shared by multiple Pages.
- Source polling target: every 60 minutes (implemented in a later collection phase, not by the V1 dashboard).
- Publishing mode: automatic only after validation; low-confidence or sensitive items are held, never auto-published.
- One central content website with a separate section/slug for each Page.
- Monetization later combines website traffic and affiliate links.
- Preserve the existing AI Affiliate Studio experience. Facebook Hub is an isolated module.

## Repository and rollout
- Base: `rebuild/video-studio-v2`.
- Feature branch: `feature/facebook-hub-v1`.
- Do not modify `main` directly.
- Keep existing PWA and video-render flow working.
- Deploy/merge only after tests and user review.

## Existing architecture to preserve
The current project is a static mobile-first PWA plus a separate FastAPI backend and worker. GitHub Pages hosts only static frontend assets; it cannot run Python, FFmpeg, scheduled jobs, or background workers. The current backend/worker design uses persistent storage and a shared job database/output volume. Do not introduce a second backend stack or Supabase migration in V1 without a separate migration decision.

## V1 scope
1. Facebook Hub dashboard (real API data when connected; clearly labeled empty/demo state otherwise).
2. Page manager: list, connect state, page category, active/paused state, per-Page auto-publish setting (default OFF until the owner explicitly enables it).
3. Source manager UI and data model: create/edit/archive source records and assign/unassign sources to Pages, including shared sources. Actual scraping/polling is V2.
4. Content manager shell: article/post records and statuses; no AI auto-generation or publishing in V1.
5. Activity log and system settings.
6. Meta OAuth integration scaffold and Page discovery only after Meta app credentials, app mode, review/access permissions, and a valid callback URL are configured.

## Proposed data model
- `facebook_accounts`: owner/user reference, Meta app-scoped account identifier, token ciphertext, token expiry, granted scopes, connection status, timestamps.
- `facebook_pages`: internal id, Meta Page id, name, category, connection status, account reference, timestamps.
- `page_settings`: Page id, active flag, auto-publish flag (default false), source-check interval (default 60 minutes), website section slug.
- `sources`: source type (RSS/website/authorized API/manual), canonical URL, display name, active flag, last checked timestamp.
- `page_sources`: many-to-many relation between Pages and sources, per-assignment enabled flag.
- `articles`: canonical source URL, content fingerprint, title, excerpt, published/source timestamps, collection status.
- `generated_posts`: target Page, article reference, generated text, confidence/review status, publish status.
- `activity_logs`: actor, action, target type/id, result, safe error detail, timestamp.

Use database migrations and foreign keys. Keep secrets out of ordinary Page rows and out of frontend/localStorage. Encrypt access tokens at rest with a server-side secret; never log tokens.

## API surface (initial)
- `GET /api/facebook-hub/dashboard`
- `GET /api/facebook-hub/pages`
- `PATCH /api/facebook-hub/pages/{page_id}/settings`
- `GET|POST /api/facebook-hub/sources`
- `PATCH|DELETE /api/facebook-hub/sources/{source_id}`
- `POST /api/facebook-hub/pages/{page_id}/sources/{source_id}`
- `DELETE /api/facebook-hub/pages/{page_id}/sources/{source_id}`
- `GET /api/facebook-hub/activity`
- OAuth start/callback endpoints only after Meta app configuration is supplied.

All routes require authentication and owner-level authorization. Validate inputs, rate-limit sensitive actions, and use CSRF/state validation for OAuth.

## V1 acceptance criteria
- Existing AI Affiliate Studio screens and render flow remain unchanged.
- Facebook Hub is reachable as a separate module on mobile and desktop.
- Dashboard clearly distinguishes connected, disconnected, paused, and demo/empty states.
- Page/source assignment supports many-to-many sharing without duplicating source records.
- Page settings can be edited and persist after reload.
- Activity log records successful and failed admin actions without secrets.
- No real Meta tokens or API keys are committed.
- Tests cover permissions, settings validation, source assignments, and migration rollback.

## Explicitly out of scope for V1
- 60-minute source polling and scraping.
- AI article generation and confidence scoring.
- Automatic Facebook publishing.
- Website article publishing and affiliate tracking.
- Bulk operations across 100 Pages.
These are subsequent phases and must be implemented with Meta policy, copyright, rate-limit, and source-reliability checks.
