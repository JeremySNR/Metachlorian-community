import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import create_app
from community.protocol import Contribution
from community.store import Store

VIDEO = "abcdefghijk"
CAPTION = {"value": "Coastal drone sunset over ocean waves", "source": "fusion", "confidence": 0.8, "model_version": "1"}
PAYLOAD = {"video_id": VIDEO, "title": "Imported coastal film", "channel": "A channel", "license": "CC BY", "duration": 30.0, "shots": [{"start_s": 12.0, "end_s": 20.0, "fields": {"content.caption": CAPTION}}],
           "moments": [{"kind": "speech", "start_s": 14.0, "end_s": 17.0, "text": "Look at the ocean", "source": "speech", "model_version": "1"}]}


@pytest.fixture
def service(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "community.sqlite"))
    client = TestClient(create_app(store, admin_token="test-moderator"))
    return client, store


def test_contribute_content_search_and_timestamp_link(service):
    client, store = service
    assert client.post("/v1/contributions", json=PAYLOAD).json()["status"] == "stored"
    result = client.get("/v1/search", params={"q": "coastal drone"}).json()
    shot = result["results"][0]
    assert shot["url"] == f"https://www.youtube.com/watch?v={VIDEO}&t=12s"
    assert shot["title"] == "Imported coastal film" and shot["fields"]["content.caption"]["source"] == "fusion"
    assert shot["snippet"] == CAPTION["value"]
    speech = client.get("/v1/search", params={"q": "Look ocean"}).json()["results"][0]
    assert speech["kind"] == "speech" and speech["start_s"] == 14
    assert speech["snippet"] == "Look at the ocean" and speech["fields"]["audio.transcript"]["source"] == "speech"
    assert client.get("/v1/search", params={"q": "no-match"}).json()["results"] == []
    assert client.get("/v1/search", params={"q": "' OR 1=1 --"}).status_code == 200
    assert client.get("/v1/search").headers["cache-control"] == "no-store"


def test_duplicates_richer_records_and_pagination(service):
    client, store = service
    client.post("/v1/contributions", json=PAYLOAD)
    assert client.post("/v1/contributions", json=PAYLOAD).json()["status"] == "duplicate"
    thin = {"video_id": VIDEO, "shots": PAYLOAD["shots"]}
    assert client.post("/v1/contributions", json=thin).json()["status"] == "retained_richer"
    first = client.get("/v1/search?limit=1").json()
    second = client.get("/v1/search?limit=1&offset=1").json()
    assert first["next_offset"] == 1 and second["next_offset"] is None
    assert first["results"][0]["kind"] != second["results"][0]["kind"]


@pytest.mark.parametrize("extra", [{"path": "/PRIVATE/file.mp4"}, {"face_embeddings": [1]}, {"actor": "PRIVATE"}, {"cookies": "PRIVATE"}])
def test_private_extra_fields_are_rejected_without_echoing(service, extra):
    client, store = service
    res = client.post("/v1/contributions", json={**PAYLOAD, **extra})
    assert res.status_code == 422 and "PRIVATE" not in res.text


def test_unknown_signals_human_output_bad_ids_and_ranges_rejected(service):
    client, store = service
    for payload in [{**PAYLOAD, "video_id": "../private"},
                    {"video_id": VIDEO, "shots": [{"start_s": 0.0, "end_s": 1.0, "fields": {"people.identities": CAPTION}}]},
                    {"video_id": VIDEO, "shots": [{"start_s": 0.0, "end_s": 1.0, "fields": {"content.caption": {**CAPTION, "source": "human"}}}]},
                    {"video_id": VIDEO, "shots": [{"start_s": 20.0, "end_s": 12.0}]}]:
        assert client.post("/v1/contributions", json=payload).status_code == 422
    assert client.post("/v1/contributions", json={"video_id": VIDEO, "duration": 30.0, "shots": [{"start_s": 0.0, "end_s": 40.0}]}).status_code == 422


def test_hosted_contributions_need_no_youtube_key_or_visibility_probe(service, monkeypatch):
    import httpx
    import subprocess

    client, store = service
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no extractor should be run"))
    monkeypatch.setattr(httpx.Client, "get", lambda *a, **k: pytest.fail("no YouTube request should be made"))
    assert client.get("/health").json()["sharing_mode"] == "warning_and_opt_out"
    assert client.post("/v1/contributions", json=PAYLOAD).status_code == 200
    # Metadata remains searchable without any external recheck, even for old entries.
    with store.connect() as c:
        c.execute("UPDATE videos SET checked_at=0")
    result = client.get("/v1/search").json()["results"][0]
    assert result["visibility_verified"] is False and result["metadata_is_community_supplied"] is True
    assert result["title"] == PAYLOAD["title"]


def test_older_clients_and_missing_download_metadata_have_safe_defaults(service):
    client, _ = service
    old = {k: v for k, v in PAYLOAD.items() if k not in {"title", "channel", "license", "duration"}}
    assert client.post("/v1/contributions", json=old).status_code == 200
    result = client.get("/v1/search").json()["results"][0]
    assert result["title"] == "YouTube video" and result["channel"] == "" and result["license"] == ""


def test_public_site_explains_private_imports_and_opt_out(service):
    client, _ = service
    html = client.get("/").text
    assert "private or unlisted YouTube imports can publish metadata too" in html
    assert "Turn sharing off before importing" in html and "Video visibility is not checked" in html


def test_moderator_removal_suppresses_future_contributions(service):
    client, store = service
    client.post("/v1/contributions", json=PAYLOAD)
    assert client.delete(f"/v1/videos/{VIDEO}").status_code == 401
    assert client.delete(f"/v1/videos/{VIDEO}", headers={"Authorization": "Bearer test-moderator"}).status_code == 200
    assert client.post("/v1/contributions", json=PAYLOAD).status_code == 403
    assert store.candidates("", 20, 0) == []


def test_size_rate_limits_and_query_bounds(service):
    client, _ = service
    assert client.post("/v1/contributions", content=b"x" * (2 * 1024 * 1024 + 1)).status_code == 413
    for _ in range(4):
        assert client.post("/v1/contributions", json={}).status_code == 422
    assert client.post("/v1/contributions", json=PAYLOAD).status_code == 429
    assert client.get("/v1/search?limit=99999").status_code == 422
    assert client.get("/v1/search?offset=-1").status_code == 422


def test_protocol_matches_application():
    app_protocol = Path(__file__).parents[2] / "Metachlorian-app/core/metachlorian/community/protocol.py"
    if app_protocol.exists():
        assert app_protocol.read_bytes() == (Path(__file__).parents[1] / "community/protocol.py").read_bytes()
