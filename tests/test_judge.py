import asyncio
import json

import httpx2 as httpx
from openai import AsyncOpenAI
import pytest

from classroom.judge import CodexClient, JudgeError
from classroom.jev import JevClient

CANDIDATES = [{"chunk_id": 7, "material_id": 1, "title": "자료", "section": "정의", "page": 3, "text": "기울기 반대로 이동합니다.", "score": 0.6}]
VALID = {"selections": [{"chunk_id": 7, "score": 0.95}], "sufficient": True}


def response_for(decision=VALID):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": json.dumps(decision)}]},
    ]}


@pytest.fixture(params=[CodexClient])
def adapter(request, monkeypatch):
    monkeypatch.setenv("OPEN_API_KEY", "private-test-key")
    monkeypatch.setenv("CLASSROOM_MODE", request.param.provider)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("JEV_MODEL", raising=False)
    return request.param()


@pytest.fixture
def transport(monkeypatch):
    original = AsyncOpenAI

    def install(handler):
        monkeypatch.setattr("classroom.judge.AsyncOpenAI", lambda **kwargs: original(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs))
    return install


async def test_request_and_source_preservation(adapter, transport):
    def handler(request):
        assert str(request.url) == "https://factchat-cloud.mindlogic.ai/v1/gateway/responses"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        body = json.loads(request.content)
        assert body["model"] == adapter.default_model
        assert body["store"] is False and "tools" not in body
        assert "untrusted data" in body["instructions"]
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        data = json.loads(body["input"])
        assert data["question"] == "질문"
        assert "score" not in data["passages"][0]
        return httpx.Response(200, json=response_for())
    transport(handler)
    selected, sufficient = await adapter.select("질문", CANDIDATES)
    assert selected == [{**CANDIDATES[0], "score": 0.95}] and sufficient
    assert CANDIDATES[0]["score"] == 0.6


@pytest.mark.parametrize("decision", [
    {"selections": [{"chunk_id": 999, "score": 0.9}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": True}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": "0.9"}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": 1.2}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": -0.1}], "sufficient": True},
    {"selections": [{"chunk_id": "7", "score": 0.9}], "sufficient": True},
    {"selections": [{"chunk_id": 7, "score": 0.9}] * 2, "sufficient": True},
    {"selections": [], "sufficient": "true"},
])
async def test_bad_decisions(adapter, transport, decision):
    transport(lambda request: httpx.Response(200, json=response_for(decision)))
    with pytest.raises(JudgeError, match="올바른"):
        await adapter.select("질문", CANDIDATES)


@pytest.mark.parametrize("body", [
    {}, {"status": "incomplete", "output": []}, {"status": "completed", "output": []},
    {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "private-test-key"}]}]},
    {"status": "completed", "output": None},
])
async def test_malformed_or_refused_output(adapter, transport, body):
    transport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(JudgeError, match="올바른"):
        await adapter.select("질문", CANDIDATES)


@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_http_failure_sanitized_without_retry(adapter, transport, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-test-key sensitive-provider-output")
    transport(handler)
    with pytest.raises(JudgeError, match=f"HTTP {status}") as error:
        await adapter.select("질문", CANDIDATES)
    assert "private" not in str(error.value) and "sensitive" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_failure(adapter, transport, failure):
    def handler(request):
        raise failure("private-test-key", request=request)
    transport(handler)
    with pytest.raises(JudgeError) as error:
        await adapter.select("질문", CANDIDATES)
    assert "private-test-key" not in str(error.value)


async def test_empty_selection_cannot_be_sufficient(adapter, transport):
    transport(lambda request: httpx.Response(200, json=response_for({"selections": [], "sufficient": True})))
    assert await adapter.select("질문", CANDIDATES) == ([], False)


async def test_missing_key_and_empty_candidates(monkeypatch, transport):
    def forbidden(request):
        raise AssertionError("must not call network")
    transport(forbidden)
    judge = CodexClient()
    assert await judge.select("질문", []) == ([], False)
    with pytest.raises(JudgeError, match="OPEN_API_KEY"):
        await judge.select("질문", CANDIDATES)


async def test_concurrency_limit(adapter, transport):
    active = peak = 0
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return httpx.Response(200, json=response_for())
    transport(handler)
    await asyncio.gather(*(adapter.select("질문", CANDIDATES) for _ in range(5)))
    assert peak == 2
