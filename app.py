"""Vercel FastAPI entrypoint. This service never mounts or opens a user's library."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from community.protocol import Contribution, VIDEO_ID, youtube_url
from community.store import Store, display_snippet
from community.visibility import NotPublic, verify_public

log = logging.getLogger(__name__)
MAX_BODY = 2 * 1024 * 1024
PUBLIC_TTL = 3600


def create_app(store: Store | None = None, verifier: Callable = verify_public, admin_token: str | None = None) -> FastAPI:
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
        # Search results must not remain in CDN/browser caches after a video's visibility changes.
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
        return {"ok": True, "schema_version": 1, "visibility_ready": bool(os.environ.get("YOUTUBE_API_KEY")) or not bool(os.environ.get("VERCEL"))}

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
        try:
            metadata = await run_in_threadpool(verifier, contribution.video_id)
        except NotPublic:
            await run_in_threadpool(db.remove, contribution.video_id)
            raise HTTPException(403, "Only explicitly public YouTube videos are accepted") from None
        except Exception:
            raise HTTPException(503, "YouTube visibility could not be verified; retry later") from None
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
        results, verified, hidden = [], {}, 0
        for r in rows[:limit]:
            vid = r["video_id"]
            metadata = json.loads(r["metadata"])
            if time.time() - r["checked_at"] >= PUBLIC_TTL:
                if vid not in verified:
                    if verified:
                        hidden += 1
                        continue
                    try:
                        verified[vid] = verifier(vid)
                        db.checked(vid, verified[vid])
                    except NotPublic:
                        db.remove(vid)
                        verified[vid] = None
                    except Exception:
                        # Uncertain visibility: hide it rather than serve stale public metadata.
                        verified[vid] = None
                metadata = verified[vid]
            if metadata is None:
                hidden += 1
                continue
            fields = json.loads(r["fields"])
            results.append({"video_id": vid, "url": youtube_url(vid, r["start_s"]), **metadata, "kind": r["kind"],
                            "start_s": r["start_s"], "end_s": r["end_s"], "fields": fields,
                            "snippet": display_snippet(r["kind"], fields, r["body"]), "analysis_is_community_supplied": True})
        return {"results": results, "next_offset": offset + limit if len(rows) > limit else None,
                "hidden_pending_visibility": hidden, "query": q}

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
