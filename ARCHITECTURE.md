# AI Affiliate Studio v2 architecture

## Product target

A mobile-first Android-friendly PWA that can submit product details, generate a real vertical MP4, poll render progress, and download the result. The current renderer is a deterministic text-based motion-graphic template; it is a first working render path, not unrestricted AI text-to-video.

## Separation of concerns

- Frontend: static PWA hosted on GitHub Pages. It generates a script/storyboard locally, submits a render job to the API, polls job status, and downloads the completed MP4. The user enters the API URL and key at render time; the key is not persisted in localStorage.
- API: FastAPI service on a separate host. It validates requests, authenticates users, records jobs in SQLite, exposes job status, and serves completed MP4s through an authenticated download endpoint.
- Worker: separate process/container using the same SQLite database and output volume. It claims queued jobs, creates three Vietnamese motion-graphic scenes with Pillow, animates them with FFmpeg, and updates job status.
- Storage: persistent SQLite database and MP4 output directory. Temporary scene images are deleted after rendering.

## Current free-first render path

1. Use Pillow and installed Noto fonts to render Vietnamese title/body cards for problem, product/benefits, and call to action.
2. Use FFmpeg zoom-pan motion and H.264 encoding to produce a 720×1280 MP4.
3. No TTS, supplied product media, stock footage, or AI-generated video is included yet. These are later features and should be added as separate tested stages.
4. Paid TTS/video providers remain disabled unless the user explicitly enables them and sees the estimated cost.

This deterministic template proves the end-to-end job/render/download path without paid AI APIs. It is not yet the final affiliate-video quality target.

## Safety and cost controls

- Never commit real API keys or put them in frontend code or localStorage.
- The PWA accepts the API key in a password field and holds it only in page memory. Use HTTPS and only on a trusted personal device.
- Restrict CORS to the exact deployed frontend origin.
- Validate request fields and duration; constrain downloads to files inside the configured output directory.
- Run API and worker as separate processes sharing the same database/output volume.
- Do not expose the API publicly until HTTPS, persistent storage, rate limits, and worker isolation/timeouts are configured.

## Rollout gates

1. Preserve `main`; all rebuild work happens on `rebuild/video-studio-v2`.
2. Build and test the API scaffold.
3. Build a deterministic render worker and test a real sample MP4 in CI.
4. Connect PWA job creation, polling, and download UI.
5. Deploy API/worker only after container configuration and Android testing.
6. Review the PR and merge to `main` only after user review.

GitHub Pages cannot run Python, Node render workers, or FFmpeg. It remains the static frontend only.
