import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import create_app
from community.protocol import Contribution
from community.store import Store
from community.visibility import NotPublic

VIDEO = "abcdefghijk"
CAPTION = {"value": "Coastal drone sunset over ocean waves", "source": "fusion", "confidence": 0.8, "model_version": "1"}
PAYLOAD = {"video_id": VIDEO, "shots": [{"start_s": 12.0, "end_s": 20.0, "fields": {"content.caption": CAPTION}}],
           "moments": [{"kind": "speech", "start_s": 14.0, "end_s": 17.0, "text": "Look at the ocean", "source": "speech", "model_version": "1"}]}


@pytest.fixture
def service(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "community.sqlite"))
    calls = []
    def verifier(vid):
        calls.append(vid)
        return {"title": "Public coastal film", "channel": "Public channel", "license": "CC BY", "duration": 30}
    client = TestClient(create_app(store, verifier, admin_token="test-moderator"))
    return client, store, calls


def test_contribute_content_search_and_timestamp_link(service):
    client, store, calls = service
    assert client.post("/v1/contributions", json=PAYLOAD).json()["status"] == "stored"
    result = client.get("/v1/search", params={"q": "coastal drone"}).json()
    shot = result["results"][0]
    assert shot["url"] == f"https://www.youtube.com/watch?v={VIDEO}&t=12s"
    assert shot["title"] == "Public coastal film" and shot["fields"]["content.caption"]["source"] == "fusion"
    speech = client.get("/v1/search", params={"q": "Look ocean"}).json()["results"][0]
    assert speech["kind"] == "speech" and speech["start_s"] == 14
    assert client.get("/v1/search", params={"q": "no-match"}).json()["results"] == []
    assert client.get("/v1/search", params={"q": "' OR 1=1 --"}).status_code == 200
    assert calls == [VIDEO]
    assert client.get("/v1/search").headers["cache-control"] == "no-store"


def test_duplicates_richer_records_and_pagination(service):
    client, store, _ = service
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
    client, store, calls = service
    res = client.post("/v1/contributions", json={**PAYLOAD, **extra})
    assert res.status_code == 422 and "PRIVATE" not in res.text and calls == []


def test_unknown_signals_human_output_bad_ids_and_ranges_rejected(service):
    client, store, _ = service
    for payload in [{**PAYLOAD, "video_id": "../private"},
                    {"video_id": VIDEO, "shots": [{"start_s": 0.0, "end_s": 1.0, "fields": {"people.identities": CAPTION}}]},
                    {"video_id": VIDEO, "shots": [{"start_s": 0.0, "end_s": 1.0, "fields": {"content.caption": {**CAPTION, "source": "human"}}}]},
                    {"video_id": VIDEO, "shots": [{"start_s": 20.0, "end_s": 12.0}]}]:
        assert client.post("/v1/contributions", json=payload).status_code == 422
    assert client.post("/v1/contributions", json={"video_id": VIDEO, "shots": [{"start_s": 0.0, "end_s": 40.0}]}).status_code == 422


def test_nonpublic_and_unavailable_visibility_store_nothing(tmp_path):
    store = Store("sqlite:///" + str(tmp_path / "community.sqlite"))
    for error, code in [(NotPublic("unlisted"), 403), (RuntimeError("network unavailable"), 503)]:
        def fail(_):
            raise error
        client = TestClient(create_app(store, fail))
        assert client.post("/v1/contributions", json=PAYLOAD).status_code == code
        assert store.candidates("", 20, 0) == []


def test_revoked_public_visibility_removes_metadata(service):
    client, store, _ = service
    client.post("/v1/contributions", json=PAYLOAD)
    with store.connect() as c:
        c.execute("UPDATE videos SET checked_at=0")
    def revoked(_):
        raise NotPublic("private now")
    client = TestClient(create_app(store, revoked))
    assert client.get("/v1/search").json()["results"] == []
    assert store.candidates("", 20, 0) == []


def test_moderator_removal_suppresses_future_contributions(service):
    client, store, _ = service
    client.post("/v1/contributions", json=PAYLOAD)
    assert client.delete(f"/v1/videos/{VIDEO}").status_code == 401
    assert client.delete(f"/v1/videos/{VIDEO}", headers={"Authorization": "Bearer test-moderator"}).status_code == 200
    assert client.post("/v1/contributions", json=PAYLOAD).status_code == 403
    assert store.candidates("", 20, 0) == []


def test_size_rate_limits_and_query_bounds(service):
    client, _, _ = service
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
