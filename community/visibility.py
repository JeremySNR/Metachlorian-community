"""The service independently checks YouTube visibility; no contributor's credentials are accepted."""
import json
import subprocess
import sys

from .protocol import youtube_url


class NotPublic(ValueError):
    pass


def verify_public(video_id: str) -> dict:
    result = subprocess.run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-plugin-dirs", "--no-warnings",
                             "--skip-download", "--no-playlist", "--socket-timeout", "8", "--retries", "0", "-J", "--",
                             youtube_url(video_id)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Anonymous visibility check unavailable")
    data = json.loads(result.stdout)
    if (data.get("id") != video_id or (data.get("extractor_key") or "").lower() != "youtube" or
            data.get("availability") != "public" or data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming")):
        raise NotPublic("This video is not explicitly public")
    return {"title": str(data.get("title") or "YouTube video")[:500],
            "channel": str(data.get("channel") or data.get("uploader") or "")[:300],
            "license": str(data.get("license") or "")[:500], "duration": data.get("duration")}
