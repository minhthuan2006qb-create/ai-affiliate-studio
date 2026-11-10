# AI Affiliate Studio v2 architecture

## Product target

A mobile-first web app controlled from Android that produces a downloadable vertical MP4 for affiliate content. Initial workflow: user product details + supplied product photos/video -> Vietnamese narration -> timed captions -> motion graphics/stock or user footage -> FFmpeg MP4 render.

## Separation of concerns

- Frontend: static PWA hosted on GitHub Pages. It collects inputs, submits jobs, polls status, previews and downloads completed MP4 files.
- API: FastAPI service on a separate host. It validates requests, authenticates users, records jobs in SQLite, and exposes job status.
- Worker: separate process on a low-cost VPS. It claims queued jobs, builds a scene plan, creates motion graphics, generates/uses narration, generates subtitles, assembles with FFmpeg, and reports completion.
- Storage: persistent job database plus temporary render files. Move finished MP4s to object storage with expiring download links when needed.

## Initial free-first render path

1. Use user-supplied product media and/or properly licensed free stock footage.
2. Use Remotion/HTML motion graphics for the opening problem scene, product callouts, captions, and CTA.
3. Use FFmpeg for vertical composition, audio mixing, subtitle burn-in, and MP4 encoding.
4. Verify Vietnamese TTS quality and licensing before selecting a default voice. Paid TTS/video providers remain disabled unless the user explicitly enables them and sees the estimated cost.

This path produces a real MP4 but is not the same as unrestricted AI text-to-video. Arbitrary generated moving scenes may require a paid provider.

## Safety and cost controls

- Never store secrets in frontend code, Git history, or browser localStorage.
- Require backend authentication; restrict CORS to the deployed frontend origin.
- Validate media type/size, job duration, provider selection, and output size.
- Render one job at a time initially; enforce timeouts and clean temporary files.
- Keep paid provider calls disabled by default and require explicit cost confirmation.
- Do not expose the API publicly until HTTPS, persistent storage, rate limiting, and worker isolation are configured.

## Rollout gates

1. Preserve `main`; all rebuild work happens on `rebuild/video-studio-v2`.
2. Build and test the API scaffold.
3. Build a deterministic render worker and test a sample MP4.
4. Verify Vietnamese narration and captions on Android.
5. Integrate the new PWA UI with the API.
6. Deploy API/worker to a VPS only after local/container tests pass.
7. Open a pull request; merge to `main` only after regression checks and user review.

GitHub Pages cannot run Python, Node render workers, or FFmpeg. It remains the static frontend only.
