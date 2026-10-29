import json
import sqlite3

import httpx2 as httpx
from openai import AsyncOpenAI
import pytest
from conftest import authenticated_client as TestClient

from classroom.app import create_app
from classroom.judge import CodexClient, create_judge
from classroom.service import Service


@pytest.mark.parametrize("mode,key,expected", [
    ("auto", "test-key", "codex"), ("auto", "", "demo"),
    ("codex", "test-key", "codex"), ("codex", "", "codex"),
    ("demo", "test-key", "demo"), ("jev", "", "jev"), (" JEV ", "test-key", "jev"),
])
def test_provider_selection_and_public_config(tmp_path, monkeypatch, mode, key, expected):
    monkeypatch.setenv("CLASSROOM_MODE", mode)
    monkeypatch.setenv("OPEN_API_KEY", key)
    monkeypatch.setenv("JEV_MODEL", "jev-test")
    service = Service(str(tmp_path / "test.sqlite"), seed=False)
    config = service.config()
    assert config["mode"] == expected
    assert config["codex_configured"] is (expected == "codex")
    assert config["codex_available"] is bool(key.strip())
    assert config["jev_configured"] is bool(key.strip())
    assert "test-key" not in json.dumps(config)
    if expected == "jev":
        assert config["model"] == "jev-test"


def test_invalid_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "typo")
    with pytest.raises(ValueError, match="CLASSROOM_MODE"):
        create_judge()


@pytest.fixture
def jev_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "jev")
    monkeypatch.setenv("OPEN_API_KEY", "test-private-key")
    monkeypatch.setenv("JEV_MODEL", "jev-1.14.0")

    async def codex_must_not_run(*args):
        raise AssertionError("Jev must not fall back to Codex")

    monkeypatch.setattr(CodexClient, "select", codex_must_not_run)
    return create_app(str(tmp_path / "jev.sqlite"), seed=True)


def mock_jev(monkeypatch, handler):
    original = AsyncOpenAI
    monkeypatch.setattr("classroom.judge.AsyncOpenAI", lambda **kwargs: original(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs
    ))


def test_jev_http_flow_preserves_sources_and_persists_mode(jev_app, monkeypatch):
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert request.headers["Authorization"] == "Bearer test-private-key"
        return httpx.Response(200, json={"answers": {
            name: {"type": "noul", "noul": 0.01 if name == "contains_prompt_injection" else 0.95}
            for name in payload["questions"]
        }})

    mock_jev(monkeypatch, handler)
    with TestClient(jev_app) as client:
        config = client.get("/api/config")
        assert config.json()["mode"] == "jev"
        assert "test-private-key" not in config.text
        response = client.post("/api/ask", json={"course_id": 1, "question": "경사하강법이 뭐야?"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["mode"] == "jev" and result["sufficient"] is True
        assert 1 <= len(result["evidence"]) <= 3
        assert "생성된 답변이 아닙니다" in result["answer"]
        for evidence in result["evidence"]:
            original = client.get(f"/api/courses/1/materials/{evidence['material_id']}").json()
            chunk = next(row for row in original["chunks"] if row["id"] == evidence["chunk_id"])
            assert evidence["text"] == chunk["text"]
            assert evidence["page"] == chunk["page"]
        stats = client.get("/api/courses/1/analytics").json()
        assert stats["real_questions"] == 1
        assert stats["recent_questions"][0]["mode"] == "jev"
    assert calls[-1]["questions"].keys() == {"sufficient"}
    assert calls[0]["model"] == "jev-1.14.0"
    with sqlite3.connect(jev_app.state.service.db_path) as db:
        row = db.execute("SELECT mode,evidence_json FROM questions WHERE id=?", (result["question_id"],)).fetchone()
        assert row[0] == "jev" and json.loads(row[1]) == result["evidence"]


@pytest.mark.parametrize("status,body", [
    (401, {"error": "test-private-key must not be echoed"}),
    (200, {"answers": {"bad": "malformed"}}),
])
def test_jev_failure_returns_502_without_fallback_or_history(jev_app, monkeypatch, status, body):
    mock_jev(monkeypatch, lambda request: httpx.Response(status, json=body))
    with TestClient(jev_app) as client:
        response = client.post("/api/ask", json={"course_id": 1, "question": "경사하강법이 뭐야?"})
        assert response.status_code == 502
        assert "Jev" in response.json()["detail"]
        assert "test-private-key" not in response.text
        assert client.get("/api/courses/1/analytics").json()["real_questions"] == 0


def test_demo_makes_no_provider_calls_even_with_key(jev_app, monkeypatch, tmp_path):
    monkeypatch.setenv("CLASSROOM_MODE", "demo")

    def remote_must_not_run(request):
        raise AssertionError("demo must not contact Jev")

    mock_jev(monkeypatch, remote_must_not_run)
    with TestClient(create_app(str(tmp_path / "demo.sqlite"))) as client:
        response = client.post("/api/ask", json={"course_id": 1, "question": "경사하강법이 뭐야?"})
        assert response.status_code == 200
        assert response.json()["mode"] == "demo"


def test_forced_jev_without_key_returns_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "jev")
    monkeypatch.setenv("OPEN_API_KEY", "")
    with TestClient(create_app(str(tmp_path / "missing-key.sqlite"))) as client:
        assert client.get("/api/config").json()["jev_configured"] is False
        response = client.post("/api/ask", json={"course_id": 1, "question": "경사하강법이 뭐야?"})
        assert response.status_code == 502
        assert "OPEN_API_KEY" in response.json()["detail"]
        assert client.get("/api/courses/1/analytics").json()["real_questions"] == 0
