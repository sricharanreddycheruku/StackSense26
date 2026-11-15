import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("stacksense.engine")
from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402

client = TestClient(app)


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "StackSense" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_schema():
    r = client.get("/api/schema")
    assert r.status_code == 200
    assert json.dumps(r.json())


def test_samples_listing_and_fetch():
    items = client.get("/api/samples").json()
    ids = {i["id"] for i in items}
    assert "public/P2" in ids and "edge/E01_minimal_required_only" in ids
    r = client.get("/api/samples/public/P2")
    assert r.status_code == 200
    assert r.json()["scenario"]["scenario_id"] == "P2"
    assert client.get("/api/samples/public/..%2Fapp").status_code == 404
    assert client.get("/api/samples/other/P1").status_code == 404


def test_recommend_public_sample():
    scenario = client.get("/api/samples/public/P2").json()["scenario"]
    r = client.post("/api/recommend", json=scenario)
    assert r.status_code == 200
    out = r.json()
    assert out["scenario_id"] == "P2"
    assert out["status"] in ("ok", "ok_with_warnings")
    assert out["stack"]["frontend"]["choice"]


def test_recommend_never_500_on_bad_input():
    for body in (b"{not json", b"[1, 2]", b"null", b'{"project": 5}', b""):
        r = client.post("/api/recommend", content=body,
                        headers={"content-type": "application/json"})
        assert r.status_code == 200
        assert r.json()["status"] == "invalid_input"


def test_every_sample_runs():
    for item in client.get("/api/samples").json():
        scenario = client.get(f"/api/samples/{item['id']}").json()["scenario"]
        r = client.post("/api/recommend", json=scenario)
        assert r.status_code == 200, item["id"]


def test_describe():
    r = client.post("/api/describe", json={"text": "chat app with payments and offline mode"})
    assert r.status_code == 200
    assert isinstance(r.json()["suggestions"], list)
    assert client.post("/api/describe", json={"text": 5}).status_code == 200


def test_schema_is_sectioned_with_typed_fields():
    s = client.get("/api/schema").json()
    fields = [f for sec in s["sections"] for f in sec["fields"]]
    assert len(fields) > 40
    assert {"path", "type", "enum", "required"} <= set(fields[0])
    assert "project.description" in {f["path"] for f in fields}


def test_describe_uses_text_key_and_returns_field_value_pairs():
    r = client.post("/api/describe", json={"text": "farmers get paid online and chat live, offline in villages"})
    assert r.status_code == 200
    got = {(s["field"], json.dumps(s["value"])) for s in r.json()["suggestions"]}
    assert ("requirements.payments", "true") in got
    assert ("requirements.offline_support", "true") in got
