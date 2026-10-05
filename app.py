"""Vercel FastAPI entrypoint. This service never mounts or opens a user's library."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from community.protocol import Contribution, VIDEO_ID, youtube_url
from community.store import Store, display_snippet

MAX_BODY = 2 * 1024 * 1024


def create_app(store: Store | None = None, admin_token: str | None = None) -> FastAPI:
    api = FastAPI(title="Metachlorian Community", version="1.0", docs_url="/api/docs")
    store = store or (Store(os.environ["DATABASE_URL"]) if os.environ.get("DATABASE_URL") else None)
    admin_token = admin_token or os.environ.get("COMMUNITY_ADMIN_TOKEN", "")
    ready = False
    init_lock = threading.Lock()

    def database():
        nonlocal ready
        if store is None:
            raise HTTPException(503, "Community database is not configured")
        if not ready:
            with init_lock:
                if not ready:
                    store.init()
                    ready = True
        return store

    def quota(request: Request, category: str, maximum: int):
        # Do not trust forwarded IP headers for abuse limits. Vercel supplies the ASGI client.
        address = request.client.host if request.client else "unknown"
        key = hashlib.sha256((category + ":" + address).encode()).hexdigest()
        db = database()
        if not db.rate_limit(key, maximum) or not db.rate_limit(category + ":global", maximum * 10):
            raise HTTPException(429, "Too many requests; try again shortly", headers={"Retry-After": "60"})

    @api.middleware("http")
    async def privacy_headers(request, call_next):
        response = await call_next(request)
        # Moderator removals should be reflected without stale CDN/browser results.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @api.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home():
        return Path(__file__).with_name("public").joinpath("index.html").read_text()

    @api.get("/health")
    def health():
        database()
        return {"ok": True, "schema_version": 1, "sharing_mode": "warning_and_opt_out"}

    @api.post("/v1/contributions")
    async def contribute(request: Request):
        await run_in_threadpool(quota, request, "contribute", 5)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                raise HTTPException(413, "Contribution too large")
        try:
            contribution = Contribution.model_validate_json(bytes(body))
        except ValidationError:
            # Validation details can echo private input values; don't return them.
            raise HTTPException(422, "Invalid community contribution") from None
        db = database()
        if await run_in_threadpool(db.suppressed, contribution.video_id):
            raise HTTPException(403, "Video is excluded from the community index")
        metadata = {k: getattr(contribution, k) for k in ("title", "channel", "license", "duration")}
        duration = metadata.get("duration")
        if isinstance(duration, (int, float)) and any(s.end_s > duration + 1 for s in [*contribution.shots, *contribution.moments]):
            raise HTTPException(422, "Analysis timestamps exceed the video duration")
        result = await run_in_threadpool(db.save, contribution, metadata)
        if result == "suppressed":
            raise HTTPException(403, "Video is excluded from the community index")
        return {"status": result, "video_id": contribution.video_id}

    @api.get("/v1/search")
    def search(request: Request, q: str = Query(default="", max_length=300), limit: int = Query(default=20, ge=1, le=50),
               offset: int = Query(default=0, ge=0, le=10000)):
        quota(request, "search", 60)
        db = database()
        rows = db.candidates(q, limit + 1, offset)
        results = []
        for r in rows[:limit]:
            vid = r["video_id"]
            metadata = json.loads(r["metadata"])
            fields = json.loads(r["fields"])
            results.append({"video_id": vid, "url": youtube_url(vid, r["start_s"]), **metadata, "kind": r["kind"],
                            "start_s": r["start_s"], "end_s": r["end_s"], "fields": fields,
                            "snippet": display_snippet(r["kind"], fields, r["body"]), "analysis_is_community_supplied": True,
                            "metadata_is_community_supplied": True, "visibility_verified": False})
        return {"results": results, "next_offset": offset + limit if len(rows) > limit else None,
                "hidden_pending_visibility": 0, "query": q}

    @api.delete("/v1/videos/{video_id}")
    def remove_video(video_id: str, request: Request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not admin_token or not hmac.compare_digest(token.encode(), admin_token.encode()):
            raise HTTPException(401, "Moderator authentication required")
        if not VIDEO_ID.fullmatch(video_id):
            raise HTTPException(422, "Invalid YouTube video id")
        database().remove(video_id, permanent=True)
        return {"removed": True}

    return api


app = create_app()
