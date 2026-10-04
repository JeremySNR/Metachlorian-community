"""The service independently checks YouTube visibility; no contributor's credentials are accepted."""
import json
import logging
import os
import re
import subprocess
import sys

import httpx

from .protocol import youtube_url

log = logging.getLogger(__name__)


class NotPublic(ValueError):
    pass


def verify_public(video_id: str) -> dict:
    youtube_url(video_id)  # Reject anything other than a canonical video ID before any network request.
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if api_key:
        return verify_api(video_id, api_key)
    if os.environ.get("VERCEL"):
        raise RuntimeError("YOUTUBE_API_KEY is required for reliable hosted visibility checks")
    result = subprocess.run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs", "--no-warnings",
                             "--skip-download", "--no-playlist", "--socket-timeout", "8", "--retries", "0", "-J", "--",
                             youtube_url(video_id)], capture_output=True, text=True, timeout=30,
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    if result.returncode:
        reason = "missing_dependency" if "No module named" in result.stderr else "youtube_login_required" if "Sign in" in result.stderr else "extractor_or_network"
        log.warning("Anonymous YouTube visibility failed: %s", reason)
        raise RuntimeError("Anonymous visibility check unavailable")
    data = json.loads(result.stdout)
    if (data.get("id") != video_id or (data.get("extractor_key") or "").lower() != "youtube" or
            data.get("availability") != "public" or data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming")):
        raise NotPublic("This video is not explicitly public")
    return {"title": str(data.get("title") or "YouTube video")[:500],
            "channel": str(data.get("channel") or data.get("uploader") or "")[:300],
            "license": str(data.get("license") or "")[:500], "duration": data.get("duration")}


def verify_api(video_id: str, api_key: str, transport=None) -> dict:
    """An application key reads public resources; never use OAuth or a contributor's YouTube login."""
    with httpx.Client(timeout=10, trust_env=False, follow_redirects=False, transport=transport) as client:
        response = client.get("https://www.googleapis.com/youtube/v3/videos",
                              params={"id": video_id, "part": "snippet,status,contentDetails", "key": api_key})
    if response.status_code != 200:
        # Do not propagate the request URL or an HTTP exception containing the API key.
        raise RuntimeError("YouTube Data API visibility check unavailable")
    data = response.json()
    items = data.get("items")
    if not isinstance(items, list):
        raise RuntimeError("YouTube Data API returned an incomplete response")
    if not items:
        raise NotPublic("Video is not available as a public resource")
    video = items[0]
    status, snippet, details = (video.get(k, {}) for k in ("status", "snippet", "contentDetails"))
    if (video.get("id") != video_id or status.get("privacyStatus") != "public" or status.get("uploadStatus") != "processed"
            or snippet.get("liveBroadcastContent") != "none"):
        raise NotPublic("Video is not explicitly public and complete")
    duration = parse_duration(details.get("duration", ""))
    if not duration or duration > 604800:
        raise NotPublic("Video duration is unavailable or unsupported")
    return {"title": str(snippet.get("title") or "YouTube video")[:500],
            "channel": str(snippet.get("channelTitle") or "")[:300],
            "license": "Creative Commons Attribution" if status.get("license") == "creativeCommon" else "Standard YouTube licence",
            "duration": duration}


def parse_duration(value: str) -> float | None:
    # YouTube contentDetails.duration uses ISO 8601 days, hours, minutes and seconds.
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
    return sum(float(n or 0) * scale for n, scale in zip(match.groups(), (86400, 3600, 60, 1))) if match else None
