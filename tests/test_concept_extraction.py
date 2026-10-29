import asyncio
from copy import deepcopy
import json

import httpx2 as httpx
from openai import AsyncOpenAI
import pytest

from classroom.concept_extraction import ConceptExtractor, ConceptExtractionError


CHUNKS = [{"chunk_id": 7, "material_id": 2, "title": "최적화", "section": "정의", "page": 3,
           "text": "경사하강법은 기울기 반대 방향으로 매개변수를 갱신합니다."}]
EXISTING = [{"id": 4, "name": "경사하강법", "description": "최적화 방법", "aliases": ["gradient descent"], "status": "approved"}]
PROPOSAL = {"name": "경사하강법", "description": "기울기 반대 방향으로 매개변수를 갱신한다.",
            "aliases": ["gradient descent"], "source_chunk_ids": [7], "merge_target_id": 4}


def response_for(concepts=None):
    return {"status": "completed", "output": [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": json.dumps({"concepts": [PROPOSAL] if concepts is None else concepts})},
        ]},
    ]}


@pytest.fixture
def extractor(monkeypatch):
    monkeypatch.setenv("OPEN_API_KEY", "private-test-key")
    monkeypatch.setenv("CLASSROOM_MODE", "codex")
    monkeypatch.setenv("CONCEPT_EXTRACTION_MODE", "auto")
    monkeypatch.delenv("CONCEPT_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    return ConceptExtractor()


@pytest.fixture
def transport(monkeypatch):
    original = AsyncOpenAI

    def install(handler):
        monkeypatch.setattr("classroom.concept_extraction.AsyncOpenAI", lambda **kwargs: original(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), **kwargs))
    return install


async def test_source_grounded_request_and_draft_output(extractor, transport):
    chunks = deepcopy(CHUNKS)
    concepts = deepcopy(EXISTING)

    def handler(request):
        assert str(request.url) == "https://factchat-cloud.mindlogic.ai/v1/gateway/responses"
        assert request.headers["Authorization"] == "Bearer private-test-key"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert body["store"] is False and "tools" not in body
        assert "untrusted data" in body["instructions"]
        assert "never claim or perform approval" in body["instructions"]
        schema = body["text"]["format"]
        assert schema["strict"] is True
        assert schema["schema"]["additionalProperties"] is False
        assert schema["schema"]["$defs"]["ExtractedConcept"]["additionalProperties"] is False
        payload = json.loads(body["input"])
        assert payload["source_passages"] == CHUNKS
        assert payload["existing_approved_concepts"][0]["id"] == 4
        assert payload["batch"] == {"number": 1, "total": 1}
        assert "private-test-key" not in body["input"]
        return httpx.Response(200, json=response_for())

    transport(handler)
    assert await extractor.extract(chunks, concepts) == [PROPOSAL]
    assert chunks == CHUNKS and concepts == EXISTING


@pytest.mark.parametrize("mode,classroom,key,expected", [
    ("auto", "auto", "key", True), ("auto", "codex", "key", True),
    ("auto", "jev", "key", True), ("auto", "demo", "key", False),
    ("auto", "auto", "", False), ("disabled", "auto", "key", False),
    ("api", "demo", "key", True), ("api", "auto", "", False),
])
def test_provider_configuration(monkeypatch, mode, classroom, key, expected):
    monkeypatch.setenv("CONCEPT_EXTRACTION_MODE", mode)
    monkeypatch.setenv("CLASSROOM_MODE", classroom)
    monkeypatch.setenv("OPEN_API_KEY", key)
    provider = ConceptExtractor()
    assert provider.configured is expected
    assert bool(provider.configuration_error) is (not expected)


def test_model_and_configuration_validation(monkeypatch):
    monkeypatch.setenv("CODEX_MODEL", "judge-model")
    assert ConceptExtractor().model == "judge-model"
    monkeypatch.setenv("CONCEPT_MODEL", "extraction-model")
    assert ConceptExtractor().model == "extraction-model"
    monkeypatch.setenv("CONCEPT_EXTRACTION_MODE", "typo")
    with pytest.raises(ValueError, match="CONCEPT_EXTRACTION_MODE"):
        ConceptExtractor()


async def test_missing_configuration_and_empty_input_make_no_calls(monkeypatch, transport):
    def forbidden(request):
        raise AssertionError("must not call network")
    transport(forbidden)
    monkeypatch.setenv("CONCEPT_EXTRACTION_MODE", "auto")
    provider = ConceptExtractor()
    assert await provider.extract([], []) == []
    with pytest.raises(ConceptExtractionError, match="OPEN_API_KEY"):
        await provider.extract(CHUNKS, [])


async def test_all_source_batches_are_processed(extractor, transport, monkeypatch):
    monkeypatch.setattr("classroom.concept_extraction.SOURCE_BATCH_CHARS", 600)
    chunks = [{**CHUNKS[0], "chunk_id": index + 1, "text": "출처 본문 " * 65} for index in range(4)]
    seen, batch_numbers = [], []

    def handler(request):
        payload = json.loads(json.loads(request.content)["input"])
        batch_numbers.append(payload["batch"])
        assert len(payload["source_passages"]) == 1
        chunk = payload["source_passages"][0]
        seen.append(chunk)
        proposal = {**PROPOSAL, "source_chunk_ids": [chunk["chunk_id"]], "merge_target_id": None}
        return httpx.Response(200, json=response_for([proposal]))

    transport(handler)
    results = await extractor.extract(chunks, [])
    assert seen == chunks
    assert [item["source_chunk_ids"] for item in results] == [[1], [2], [3], [4]]
    assert batch_numbers == [{"number": number, "total": 4} for number in range(1, 5)]


async def test_cross_batch_source_ids_are_rejected(extractor, transport, monkeypatch):
    monkeypatch.setattr("classroom.concept_extraction.SOURCE_BATCH_CHARS", 600)
    chunks = [{**CHUNKS[0], "chunk_id": index + 1, "text": "출처 본문 " * 65} for index in range(2)]
    transport(lambda request: httpx.Response(200, json=response_for([
        {**PROPOSAL, "source_chunk_ids": [2], "merge_target_id": None},
    ])))
    with pytest.raises(ConceptExtractionError, match="출처"):
        await extractor.extract(chunks, [])


@pytest.mark.parametrize("change", [
    {"source_chunk_ids": [999]}, {"source_chunk_ids": [7, 7]}, {"source_chunk_ids": []},
    {"source_chunk_ids": ["7"]}, {"source_chunk_ids": [True]},
    {"merge_target_id": 999}, {"merge_target_id": "4"}, {"merge_target_id": True},
    {"name": " "}, {"description": ""}, {"aliases": [1]},
    {"approved": True}, {"name": "X" * 121},
])
async def test_invalid_generated_concepts_rejected(extractor, transport, change):
    transport(lambda request: httpx.Response(200, json=response_for([{**PROPOSAL, **change}])))
    with pytest.raises(ConceptExtractionError, match="올바른"):
        await extractor.extract(CHUNKS, EXISTING)


async def test_only_live_approved_targets_are_sent(extractor, transport):
    def handler(request):
        payload = json.loads(json.loads(request.content)["input"])
        assert payload["existing_approved_concepts"] == []
        return httpx.Response(200, json=response_for())
    transport(handler)
    with pytest.raises(ConceptExtractionError, match="올바른"):
        await extractor.extract(CHUNKS, [
            {**EXISTING[0], "status": "draft"},
            {**EXISTING[0], "id": 8, "available": False},
        ])


async def test_existing_drafts_supply_identity_without_approval_target(extractor, transport):
    draft = {**EXISTING[0], "status": "draft", "description": "교수가 편집한 정의"}
    original = deepcopy(draft)

    def handler(request):
        body = json.loads(request.content)
        payload = json.loads(body["input"])
        assert payload["existing_approved_concepts"] == []
        assert payload["existing_draft_concepts"] == [{
            "name": "경사하강법", "description": "교수가 편집한 정의",
            "aliases": ["gradient descent"], "description_excerpted": False,
        }]
        assert "reuse that canonical name" in body["instructions"]
        return httpx.Response(200, json=response_for([{**PROPOSAL, "merge_target_id": None}]))

    transport(handler)
    assert await extractor.extract(CHUNKS, [draft]) == [{**PROPOSAL, "merge_target_id": None}]
    assert draft == original


async def test_previous_batch_names_are_identity_hints_without_old_source_ids(extractor, transport, monkeypatch):
    monkeypatch.setattr("classroom.concept_extraction.SOURCE_BATCH_CHARS", 600)
    chunks = [{**CHUNKS[0], "chunk_id": index + 1, "text": "출처 본문 " * 65} for index in range(3)]
    previous_contexts = []

    def handler(request):
        payload = json.loads(json.loads(request.content)["input"])
        previous_contexts.append(payload["previous_batch_concepts"])
        number = payload["batch"]["number"]
        concept = {**PROPOSAL, "source_chunk_ids": [number], "merge_target_id": None}
        if number == 2:
            concept["aliases"] = ["gradient descent", "최급강하법"]
        if number == 3:
            context = payload["previous_batch_concepts"]
            assert len(context) == 1
            assert context[0]["name"] == "경사하강법"
            assert context[0]["aliases"] == ["gradient descent", "최급강하법"]
            assert "source_chunk_ids" not in json.dumps(context)
            assert "merge_target_id" not in json.dumps(context)
        return httpx.Response(200, json=response_for([concept]))

    transport(handler)
    proposals = await extractor.extract(chunks, [])
    assert previous_contexts[0] == []
    assert len(proposals) == 3
    # Building a compact identity history must not mutate earlier proposals.
    assert proposals[0]["aliases"] == ["gradient descent"]


async def test_identity_description_excerpt_is_explicit_and_sources_remain_whole(extractor, transport):
    existing = [{**EXISTING[0], "description": "x" * 2000}]

    def handler(request):
        payload = json.loads(json.loads(request.content)["input"])
        identity = payload["existing_approved_concepts"][0]
        assert len(identity["description"]) == 300
        assert identity["description_excerpted"] is True
        assert payload["source_passages"] == CHUNKS
        return httpx.Response(200, json=response_for())

    transport(handler)
    assert await extractor.extract(CHUNKS, existing) == [PROPOSAL]


def test_identity_history_preserves_semantic_symbols():
    from classroom.concept_extraction import _combine_identities
    concepts = [{"name": name, "description": name, "aliases": []} for name in ["C", "C++", "C#"]]
    assert [value["name"] for value in _combine_identities(concepts)] == ["C", "C++", "C#"]


async def test_empty_concept_result_is_valid(extractor, transport):
    transport(lambda request: httpx.Response(200, json=response_for([])))
    assert await extractor.extract(CHUNKS, []) == []


@pytest.mark.parametrize("body", [
    {}, {"status": "incomplete", "output": []}, {"status": "completed", "output": []},
    {"status": "completed", "output": None},
    {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
     "content": [{"type": "refusal", "refusal": "private-test-key"}]}]},
    {"status": "completed", "output": [{"type": "function_call", "name": "approve"}]},
])
async def test_malformed_incomplete_and_refused_responses(extractor, transport, body):
    transport(lambda request: httpx.Response(200, json=body))
    with pytest.raises(ConceptExtractionError, match="올바른") as error:
        await extractor.extract(CHUNKS, EXISTING)
    assert "private-test-key" not in str(error.value)


@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_http_failures_are_sanitized_without_retry(extractor, transport, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private-test-key sensitive-provider-output")
    transport(handler)
    with pytest.raises(ConceptExtractionError, match=f"HTTP {status}") as error:
        await extractor.extract(CHUNKS, [])
    assert "private" not in str(error.value) and "sensitive" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_failures_are_sanitized(extractor, transport, failure):
    def handler(request):
        raise failure("private-test-key", request=request)
    transport(handler)
    with pytest.raises(ConceptExtractionError) as error:
        await extractor.extract(CHUNKS, [])
    assert "private-test-key" not in str(error.value)


async def test_limits_and_bad_inputs_fail_before_any_request(extractor, transport, monkeypatch):
    def forbidden(request):
        raise AssertionError("must not call network")
    transport(forbidden)
    with pytest.raises(ConceptExtractionError, match="구절이 너무"):
        await extractor.extract(CHUNKS + [{**CHUNKS[0], "chunk_id": 8, "text": "x" * 16_000}], [])
    with pytest.raises(ConceptExtractionError, match="정보"):
        await extractor.extract(CHUNKS * 2, [])
    monkeypatch.setattr("classroom.concept_extraction.EXISTING_CONTEXT_CHARS", 20)
    with pytest.raises(ConceptExtractionError, match="한도"):
        await extractor.extract(CHUNKS, EXISTING)


async def test_failure_of_a_later_batch_is_not_reported_as_partial_success(extractor, transport, monkeypatch):
    monkeypatch.setattr("classroom.concept_extraction.SOURCE_BATCH_CHARS", 600)
    chunks = [{**CHUNKS[0], "chunk_id": index + 1, "text": "출처 본문 " * 65} for index in range(2)]
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(503, text="private-test-key")
        return httpx.Response(200, json=response_for([{**PROPOSAL, "source_chunk_ids": [1], "merge_target_id": None}]))
    transport(handler)
    with pytest.raises(ConceptExtractionError, match="HTTP 503"):
        await extractor.extract(chunks, [])
    assert len(calls) == 2


async def test_concurrency_is_bounded(extractor, transport):
    active = peak = 0
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=response_for())
    transport(handler)
    await asyncio.gather(*(extractor.extract(CHUNKS, EXISTING) for _ in range(5)))
    assert peak == 2
