# Metachlorian Community

A separate public index for machine analysis of explicitly public YouTube videos. The private Metachlorian library retains its videos and complete records; this service receives only the versioned allowlist in `community/protocol.py`.

## Run locally

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
DATABASE_URL=sqlite:///./community.sqlite .venv/bin/uvicorn app:app --port 8790
```

Open http://localhost:8790. Set the private application's Community service URL to `http://localhost:8790`. SQLite is for development only; Vercel requires hosted Postgres.

## Deploy

Import this repository into Vercel (FastAPI), create a **separate Neon Postgres database**, and connect it to this project. `DATABASE_URL` must be the encrypted pooled Postgres connection string. Tables and a GIN full-text index are created idempotently at first use. Never connect an existing private application's database. Do not use ephemeral filesystem storage in production.

Set `COMMUNITY_ADMIN_TOKEN` to a random secret in the project's encrypted environment variables. It grants only moderation through `DELETE /v1/videos/{youtube_id}`; removed videos are suppressed from future contributions. Production should expose the public search and contributions API without Vercel authentication. Preview environments should use their own database, never production's.

## Contract

- `POST /v1/contributions`: v1 `Contribution` JSON. Strict schema, 2 MiB maximum, 2,000 shots and 4,000 speech/OCR moments maximum. Accepted input is anonymous machine analysis, not clearance, verified truth or media.
- `GET /v1/search?q=coastal+drone&limit=20&offset=0`: content search across shot and moment records; stable pagination, links into YouTube, signal source/confidence/model versions.
- `GET /health`: database readiness.
- `DELETE /v1/videos/{id}` with moderator Bearer token: removes metadata and suppresses re-imports.

The service fetches title/channel/licence/duration itself from YouTube. It accepts no cookies, credentials, arbitrary URLs, paths, local IDs, embeddings, faces, human corrections or private-library rights. It independently checks YouTube's explicit `availability=public` with an anonymous yt-dlp subprocess before each contribution. Unlisted, private, members-only, live and ambiguous videos are refused. A network/YouTube block returns 503 and stores nothing. Clients retry later.

Public visibility is cached for up to **one hour** for search. A stale video must pass a fresh check before results are served; a non-public video is removed and uncertain visibility is hidden. This is not instantaneous detection of a visibility change. No CDN/browser caching is allowed. At most one stale video is checked per search request to bound latency; other stale results stay hidden until a later request checks them.

Contributions deduplicate by YouTube ID and a payload digest; a richer record is retained over a thinner one. Multiple contributions are not merged across incompatible shot boundaries. Source/version/confidence accompany every signal. Rate limits use database-backed per-client hashed buckets and global request limits. Hosting access logs may still contain visitor IPs. No analytics or contributor tracking is added.

Automatic checks prove eligibility, transport exclusions, opt-out, retries, schema validation, deduplication, timestamp search, visibility revocation and moderator suppression. yt-dlp can be blocked by YouTube's datacenter/bot protections; the service fails closed in that case. Vercel hosting alone cannot guarantee YouTube verification availability. For scale, move visibility verification to a bounded queue or the official YouTube API, and add contributor attestation/moderation rather than treating supplied captions as trusted facts.

## Tests

```sh
pip install pytest httpx
pytest -q
```

Apache-2.0. Protocol v1 matches the application; update both copies and their contract-parity test together.
