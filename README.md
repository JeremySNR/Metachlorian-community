# Metachlorian Community

A separate public index for community-supplied machine analysis of YouTube videos. Metachlorian retains its media and complete local records; this service receives only the versioned allowlist in `community/protocol.py`.

**Sharing is on by default. Video visibility is not checked.** Private, unlisted or sensitive YouTube imports can publish titles, descriptions, transcripts and on-screen text. Turn off Community sharing before importing them if you want that metadata to stay private. Local/personal files, other websites and duplicate local footage are excluded by the app. Previously published metadata remains searchable until removed by the operator, even if a video becomes private or is deleted.

## Run locally

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt
DATABASE_URL=sqlite:///./community.sqlite .venv/bin/uvicorn app:app --port 8790
```

Open http://localhost:8790. Set the application's Community service URL to `http://localhost:8790`. SQLite is for development only; use hosted Postgres on Vercel.

## Deploy

Import this repository into Vercel (FastAPI), create a **separate Neon Postgres database**, and connect it to this project. `DATABASE_URL` must be the encrypted pooled Postgres connection string. Tables and a GIN full-text index are created idempotently at first use. Never connect an existing private application's database. Do not use ephemeral filesystem storage in production.

Set `COMMUNITY_ADMIN_TOKEN` to a random secret in encrypted production environment variables. It grants moderation through `DELETE /v1/videos/{youtube_id}`; removed videos are suppressed from future contributions. Expose public search/contributions without Vercel authentication. Preview environments should use their own database, never production's.

No YouTube API key, Google Cloud project, OAuth or YouTube login is needed. Contributions and searches make no requests to YouTube. Opening a result takes the visitor to YouTube, where the video's own access restrictions apply.

## Contract

- `POST /v1/contributions`: v1 `Contribution` JSON. Strict schema, 2 MiB maximum, 2,000 shots and 4,000 speech/OCR moments maximum. Optional download title/channel/licence/duration fields have bounded defaults for older clients. Timestamps are checked against the supplied duration when available.
- `GET /v1/search?q=coastal+drone&limit=20&offset=0`: content search across shot and moment records, pagination, YouTube timestamp links and signal provenance. `visibility_verified` is false; metadata and analysis are community supplied. The legacy `hidden_pending_visibility` field is always zero.
- `GET /health`: database readiness and `sharing_mode: warning_and_opt_out`.
- `DELETE /v1/videos/{id}` with moderator Bearer token: removes metadata and suppresses re-imports.

The service accepts no cookies, credentials, arbitrary URLs, paths, local IDs, embeddings, faces, human corrections or local rights records. Download metadata is contributor supplied; descriptions and licences are not verified truth or permission to reuse footage. The site warns about public metadata, private/unlisted imports and opt-out. Contributions are rate limited; moderator removal remains available. No videos, frames or audio files are accepted.

## Tests

```sh
python -m pytest -q
```

Tests cover strict payload exclusions, no-key operation on Vercel, contributor metadata, timestamp validation/search, deduplication, quotas and moderator suppression. The application separately tests enrollment, opt-out, unchanged downloaded bytes and payload exclusions. There is no automatic visibility revocation mechanism.

Apache-2.0. See LICENSE.
