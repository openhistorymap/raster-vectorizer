"""Assisted tracing: flood-fill tracing on a georeferenced scan, and label reading through an
OpenAI-compatible endpoint configured by environment (simulated here with httpx.MockTransport)."""
from __future__ import annotations

import base64
import json

import httpx
import numpy as np
import pytest

from backend import llm
from backend.tests.test_georef import H, RES, W, gcps, scan_png, truth

SRC = "https://example.org/carta-1702"


@pytest.fixture
def scan(client):
    client.put("/api/worlds/fakeworld/sources", json={SRC: {"title": "Carta di Bologna, 1702", "type": "map"}})
    client.post("/api/worlds/fakeworld/scans", data={"source": SRC},
                files={"file": ("carta.png", scan_png(), "image/png")})
    client.put("/api/worlds/fakeworld/scans/carta/georef", json={"gcps": gcps(), "transformation": "polynomial1"})


# --------------------------------------------------------------------------- tracing

def test_trace_turns_the_red_square_into_a_cited_polygon(client, scan):
    r = client.post("/api/worlds/fakeworld/scans/carta/trace", json={"seed": [120, 70], "tolerance": 20})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["pixels"] == 40 * 40 and out["warnings"] == []
    ring = np.array(out["feature"]["geometry"]["coordinates"][0])
    lon0, lat_top = truth(100, 50)
    lon1, lat_bottom = truth(140, 90)
    tol = 1.0 * RES / 111_000 * 1.5                     # within a pixel of the true edges
    assert ring[:, 0].min() == pytest.approx(lon0, abs=tol) and ring[:, 0].max() == pytest.approx(lon1, abs=tol)
    assert ring[:, 1].max() == pytest.approx(lat_top, abs=tol) and ring[:, 1].min() == pytest.approx(lat_bottom, abs=tol)

    cit = out["feature"]["citations"][0]
    assert cit["source"] == SRC and cit["file"] == "carta" and cit["method"] == "traced"
    assert {s["type"] for s in cit["selector"]} == {"SvgSelector", "FragmentSelector"}
    # The traced feature can be saved and exported as valid Cited GeoJSON straight away.
    feat = {**out["feature"], "properties": {"name": "red square"}}
    client.put("/api/worlds/fakeworld/layers/traced", json={"type": "FeatureCollection", "features": [feat]})
    assert client.get("/api/worlds/fakeworld/layers/traced/cited-geojson").status_code == 200


def test_trace_warns_when_the_fill_leaks(client, scan):
    out = client.post("/api/worlds/fakeworld/scans/carta/trace", json={"seed": [5, 5]}).json()
    assert out["share_of_scan"] > 0.9 and "leaked" in out["warnings"][0]


def test_trace_needs_a_georeference(client):
    client.post("/api/worlds/fakeworld/scans", files={"file": ("loose.png", scan_png(), "image/png")})
    assert client.post("/api/worlds/fakeworld/scans/loose/trace", json={"seed": [1, 1]}).status_code == 409


# --------------------------------------------------------------------------- model configuration

@pytest.fixture
def model(monkeypatch):
    """Capture requests to the model endpoint and answer with `model.reply`."""
    class Fake:
        requests: list[httpx.Request] = []
        status = 200
        reply = '```json\n{"labels": [{"text": "Palazzo del Podestà", "kind": "building", "confidence": 0.9},' \
                ' {"text": "Piazza Maggiore", "kind": "plaza", "confidence": 2}], "notes": ""}\n```'

        def handler(self, request):
            self.requests.append(request)
            if self.status != 200:
                return httpx.Response(self.status, json={"error": {"message": "invalid key"}})
            return httpx.Response(200, json={"model": "test/vision-1",
                                             "choices": [{"message": {"content": self.reply}}]})
    fake = Fake()
    fake.requests = []
    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(fake.handler))
    for var in ("OFM_LLM_BASE_URL", "OFM_LLM_API_KEY", "OFM_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return fake


def test_unconfigured_model_is_reported_not_called(client, scan, model):
    assert client.get("/api/assist").json()["read"] == {"enabled": False, "provider": "openrouter.ai",
                                                        "model": llm.DEFAULT_MODEL}
    r = client.post("/api/worlds/fakeworld/scans/carta/read", json={"xywh": [100, 50, 40, 40]})
    assert r.status_code == 503 and "OFM_LLM_API_KEY" in r.json()["detail"]
    assert model.requests == []


def test_openrouter_reads_labels_and_returns_a_citation(client, scan, model, monkeypatch):
    monkeypatch.setenv("OFM_LLM_API_KEY", "sk-or-test")
    monkeypatch.setenv("OFM_LLM_MODEL", "qwen/qwen2.5-vl-72b-instruct")
    r = client.post("/api/worlds/fakeworld/scans/carta/read", json={"xywh": [100, 50, 40, 40], "hint": "Bologna, 1702"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["labels"] == [
        {"text": "Palazzo del Podestà", "kind": "building", "confidence": 0.9},
        {"text": "Piazza Maggiore", "kind": "other", "confidence": 1.0},   # unknown kind and confidence normalised
    ]
    assert out["model"] == "test/vision-1"
    assert out["citation"]["source"] == SRC and out["citation"]["method"] == "transcribed"
    assert out["citation"]["selector"] == [{"type": "FragmentSelector", "value": "xywh=100,50,40,40"}]

    req = model.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-or-test"
    assert "x-title" in req.headers
    body = json.loads(req.content)
    assert body["model"] == "qwen/qwen2.5-vl-72b-instruct" and body["temperature"] == 0
    parts = body["messages"][0]["content"]
    assert "Bologna, 1702" in parts[0]["text"]
    png = base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"                       # the cropped region, not the whole scan


def test_private_endpoint_without_key(client, scan, model, monkeypatch):
    monkeypatch.setenv("OFM_LLM_BASE_URL", "http://vllm.internal:8000/v1")
    assert client.get("/api/assist").json()["read"]["enabled"] is True
    assert client.post("/api/worlds/fakeworld/scans/carta/read", json={"xywh": [0, 0, 50, 50]}).status_code == 200
    req = model.requests[0]
    assert str(req.url) == "http://vllm.internal:8000/v1/chat/completions"
    assert "authorization" not in req.headers and "x-title" not in req.headers


def test_provider_errors_are_surfaced(client, scan, model, monkeypatch):
    monkeypatch.setenv("OFM_LLM_API_KEY", "bad")
    model.status = 401
    r = client.post("/api/worlds/fakeworld/scans/carta/read", json={"xywh": [0, 0, 50, 50]})
    assert r.status_code == 502 and "401" in r.json()["detail"] and "invalid key" in r.json()["detail"]


def test_non_json_replies_are_rejected_cleanly(client, scan, model, monkeypatch):
    monkeypatch.setenv("OFM_LLM_API_KEY", "k")
    model.reply = "I see a palace and a square."
    r = client.post("/api/worlds/fakeworld/scans/carta/read", json={"xywh": [0, 0, 50, 50]})
    assert r.status_code == 502 and "JSON" in r.json()["detail"]


def test_region_must_lie_inside_the_scan(client, scan, model, monkeypatch):
    monkeypatch.setenv("OFM_LLM_API_KEY", "k")
    r = client.post("/api/worlds/fakeworld/scans/carta/read", json={"xywh": [W - 10, H - 10, 50, 50]})
    assert r.status_code == 422 and model.requests == []
