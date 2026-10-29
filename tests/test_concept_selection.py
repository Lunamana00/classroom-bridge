import asyncio
import copy
import json

import httpx2 as httpx
from openai import AsyncOpenAI
import pytest

from classroom.concept_selection import GPTConceptSelector
from classroom.judge import JudgeError


def concept(identifier=1, chunk_id=None):
    chunk_id = chunk_id if chunk_id is not None else identifier
    return {
        "id": identifier, "name": f"개념 {identifier}", "description": f"정의 {identifier}: ∇·F와 ∇×F를 구분합니다.",
        "aliases": [f"Concept {identifier}"], "revision": 2, "source_chunk_ids": [chunk_id],
        "sources": [{"chunk_id": chunk_id, "material_id": 4, "title": "벡터 미적분", "text": f"원문 {chunk_id}: ∇×F = 0\n조건을 확인합니다.", "page": 7, "section": "적용 조건"}],
        "approved_by": 900, "status": "approved", "extraction_model": "private-metadata",
    }


def decision(identifier=1, chunk_id=None):
    return {"selections": [{"concept_id": identifier, "score": 0.9, "reason": "질문의 적용 조건을 설명하는 데 필요합니다."}],
            "evidence": [{"chunk_id": chunk_id if chunk_id is not None else identifier, "score": 0.85}],
            "sufficient": True}


def response_for(value):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": json.dumps(value)}]},
    ]}


@pytest.fixture
def selector(monkeypatch):
    monkeypatch.setenv("OPEN_API_KEY", "private-test-key")
    monkeypatch.setenv("CLASSROOM_MODE", "codex")
    monkeypatch.setenv("CODEX_MODEL", "gpt-test-concepts")
    return GPTConceptSelector()


@pytest.fixture
def transport(monkeypatch):
    original = AsyncOpenAI

    def install(handler):
        monkeypatch.setattr("classroom.judge.AsyncOpenAI", lambda **kwargs: original(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs))
    return install


async def test_all_concepts_sent_in_one_call_and_last_candidate_selectable(selector, transport):
    concepts = [concept(i) for i in range(1, 41)]
    original = copy.deepcopy(concepts)
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url).endswith("/responses")
        body = json.loads(request.content)
        assert body["model"] == "gpt-test-concepts"
        assert body["store"] is False and "tools" not in body
        assert "untrusted data" in body["instructions"]
        schema = body["text"]["format"]
        assert schema["strict"] is True and schema["schema"]["additionalProperties"] is False
        payload = json.loads(body["input"])
        assert payload["question"] == "질문과 단어가 겹치지 않아도 필요한 개념은?"
        assert [c["id"] for c in payload["concepts"]] == list(range(1, 41))
        assert len(payload["passages"]) == 40
        assert payload["concepts"][-1]["description"] == concepts[-1]["description"]
        assert "approved_by" not in body["input"] and "private-metadata" not in body["input"]
        return httpx.Response(200, json=response_for(decision(40)))

    transport(handler)
    result = await selector.select("질문과 단어가 겹치지 않아도 필요한 개념은?", concepts)
    assert result["concepts"][0]["id"] == 40 and result["sufficient"] is True
    assert set(result["concepts"][0]) == {"id", "name", "description", "aliases", "revision", "source_chunk_ids", "score", "reason"}
    assert result["evidence"] == [{**concepts[-1]["sources"][0], "score": 0.85}]
    assert concepts == original and len(calls) == 1


async def test_shared_sources_deduplicated_and_more_than_three_selections_retained(selector, transport):
    concepts = [concept(i, chunk_id=1) for i in range(1, 6)]
    answer = decision()
    answer["selections"] = [decision(i)["selections"][0] for i in range(1, 6)]

    def handler(request):
        payload = json.loads(json.loads(request.content)["input"])
        assert len(payload["passages"]) == 1 and len(payload["concepts"]) == 5
        return httpx.Response(200, json=response_for(answer))

    transport(handler)
    result = await selector.select("질문", concepts)
    assert len(result["concepts"]) == 5 and len(result["evidence"]) == 1


def invalid_decisions():
    values = []
    for field, invalid in (("concept_id", 999), ("concept_id", True), ("concept_id", "1"),
                           ("score", True), ("score", "0.8"), ("score", float("nan")),
                           ("score", 1.1), ("reason", "   ")):
        value = decision()
        value["selections"][0][field] = invalid
        values.append(value)
    value = decision()
    value["selections"] *= 2
    values.append(value)
    value = decision()
    value["evidence"] *= 2
    values.append(value)
    for chunk_id in (True, "1", 999, 2):
        value = decision()
        value["evidence"][0]["chunk_id"] = chunk_id
        values.append(value)
    value = decision()
    value["evidence"] = []
    values.append(value)
    value = decision()
    value["selections"] = []
    values.append(value)
    value = decision()
    value["sufficient"] = "true"
    values.append(value)
    return values


@pytest.mark.parametrize("answer", invalid_decisions())
async def test_invalid_ids_types_and_cross_concept_evidence_rejected(selector, transport, answer):
    transport(lambda request: httpx.Response(200, json=response_for(answer)))
    with pytest.raises(JudgeError, match="올바른"):
        await selector.select("질문", [concept(1), concept(2)])


async def test_empty_selection_forces_insufficient(selector, transport):
    transport(lambda request: httpx.Response(200, json=response_for({"selections": [], "evidence": [], "sufficient": True})))
    assert await selector.select("범위 밖 질문", [concept()]) == {"concepts": [], "evidence": [], "sufficient": False}


async def test_empty_catalog_no_network_and_missing_key_error(transport):
    def forbidden(request):
        raise AssertionError("must not call API")
    transport(forbidden)
    selector = GPTConceptSelector()
    assert await selector.select("질문", []) == {"concepts": [], "evidence": [], "sufficient": False}
    with pytest.raises(JudgeError, match="OPEN_API_KEY"):
        await selector.select("질문", [concept()])


@pytest.mark.parametrize("body", [
    {}, {"status": "incomplete", "output": []}, {"status": "completed", "output": []},
    {"status": "completed", "output": None},
    {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "private-test-key"}]}]},
])
async def test_malformed_refused_incomplete_response(selector, transport, body):
    transport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(JudgeError, match="올바른") as error:
        await selector.select("질문", [concept()])
    assert "private-test-key" not in str(error.value)


@pytest.mark.parametrize("status", [400, 401, 413, 429, 500])
async def test_http_errors_sanitized_no_retry_or_truncation(selector, transport, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": "context_length_exceeded", "message": "private-test-key sensitive-source"}})
    transport(handler)
    with pytest.raises(JudgeError, match=f"HTTP {status}") as error:
        await selector.select("질문", [concept()])
    assert "private-test-key" not in str(error.value) and "sensitive-source" not in str(error.value)
    if status in {400, 413}:
        assert "입력 한도" in str(error.value) and "자동으로 줄이지" in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_error_sanitized(selector, transport, failure):
    def handler(request):
        raise failure("private-test-key", request=request)
    transport(handler)
    with pytest.raises(JudgeError) as error:
        await selector.select("질문", [concept()])
    assert "private-test-key" not in str(error.value)


async def test_concurrency_is_bounded(selector, transport):
    active = peak = 0
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return httpx.Response(200, json=response_for(decision()))
    transport(handler)
    await asyncio.gather(*(selector.select("질문", [concept()]) for _ in range(5)))
    assert peak == 2
