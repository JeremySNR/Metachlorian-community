import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from community import visibility

VIDEO = "abcdefghijk"


def resource(**overrides):
    return {"id": VIDEO, "status": {"privacyStatus": "public", "uploadStatus": "processed", "license": "creativeCommon"},
            "snippet": {"title": "Public video", "channelTitle": "A channel", "liveBroadcastContent": "none"},
            "contentDetails": {"duration": "PT1M19S"}, **overrides}


def check(data, code=200):
    calls = []
    def receive(request):
        calls.append(request)
        return httpx.Response(code, json=data)
    result = visibility.verify_api(VIDEO, "secret-key", httpx.MockTransport(receive))
    assert calls[0].url.host == "www.googleapis.com"
    assert calls[0].url.params["part"] == "snippet,status,contentDetails"
    assert "authorization" not in calls[0].headers and "cookie" not in calls[0].headers
    return result


def test_official_api_reads_public_status_and_duration():
    result = check({"items": [resource()]})
    assert result["duration"] == 79 and result["title"] == "Public video"
    assert result["license"] == "Creative Commons Attribution"
    assert visibility.parse_duration("P1DT2H3M4.5S") == 93784.5


@pytest.mark.parametrize("data", [{"items": []}, {"items": [resource(status={"privacyStatus": "unlisted", "uploadStatus": "processed"})]},
    {"items": [resource(status={"privacyStatus": "private", "uploadStatus": "processed"})]},
    {"items": [resource(status={"privacyStatus": "public", "uploadStatus": "uploaded"})]},
    {"items": [resource(snippet={"liveBroadcastContent": "live"})]}, {"items": [resource(id="different12")]},
    {"items": [resource(contentDetails={"duration": "invalid"})]}, {"items": [resource(status={})]}])
def test_nonpublic_incomplete_and_ambiguous_api_resources_are_refused(data):
    with pytest.raises(visibility.NotPublic):
        check(data)


def test_api_outages_are_not_proof_of_visibility_and_never_echo_key():
    with pytest.raises(RuntimeError) as error:
        check({"error": "secret-key"}, 403)
    assert "secret-key" not in str(error.value)
    with pytest.raises(RuntimeError):
        check({})


def test_vercel_requires_api_key_and_does_not_use_cookies(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(visibility.subprocess, "run", lambda *a, **k: pytest.fail("hosted verifier must use the official API"))
    with pytest.raises(RuntimeError, match="YOUTUBE_API_KEY"):
        visibility.verify_public(VIDEO)
    monkeypatch.setenv("YOUTUBE_API_KEY", "service-key")
    monkeypatch.setattr(visibility, "verify_api", lambda vid, key: {"video_id": vid, "key_received": key == "service-key"})
    assert visibility.verify_public(VIDEO) == {"video_id": VIDEO, "key_received": True}
