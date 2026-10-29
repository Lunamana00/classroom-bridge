import asyncio
import json

import httpx
import pytest

from classroom.judge import JudgeError
from classroom.jev import JevClient


CANDIDATES = [
    {"chunk_id": 11, "material_id": 1, "title": "경사하강법", "text": "기울기의 반대 방향으로 이동한다.", "page": 2, "section": "기본", "score": 0.8},
    {"chunk_id": 12, "material_id": 1, "title": "경사하강법", "text": "학습률은 이동 크기를 결정한다.", "page": 3, "section": "학습률", "score": 0.7},
]


def response_for(payload, **scores):
    defaults = {"is_relevant": 0.9, "contains_answer_evidence": 0.8, "contains_prompt_injection": 0.05, "sufficient": 0.9}
    defaults.update(scores)
    return {"model": "jev-latest", "answers": {name: {"type": "noul", "noul": defaults[name]} for name in payload["questions"]}}


@pytest.fixture
def install_transport(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-not-a-real-key")
    monkeypatch.delenv("JEV_MODEL", raising=False)
    real_client = httpx.AsyncClient
    clients = []

    def install(handler):
        transport = httpx.MockTransport(handler)

        def create_client(**kwargs):
            client = real_client(transport=transport, **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr("classroom.jev.httpx.AsyncClient", create_client)
        return clients

    return install


async def test_documented_request_shape_and_success(install_transport, monkeypatch):
    monkeypatch.setenv("JEV_MODEL", "jev-custom")
    requests = []

    def handler(request):
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-only-not-a-real-key"
        payload = json.loads(request.content)
        assert set(payload) == {"model", "state", "questions"}
        assert payload["model"] == "jev-custom"
        assert isinstance(payload["state"], dict)
        assert all(question["type"] == "noul" for question in payload["questions"].values())
        assert all("instructions" in question for question in payload["questions"].values())
        requests.append(payload)
        return httpx.Response(200, json=response_for(payload))

    clients = install_transport(handler)
    judge = JevClient()
    assert judge.provider == "jev" and judge.configured and judge.available
    selected, sufficient = await judge.select("경사하강법이 뭐야?", CANDIDATES)
    assert sufficient is True
    assert [item["chunk_id"] for item in selected] == [11, 12]
    assert selected[0]["text"] == CANDIDATES[0]["text"]
    assert selected[0]["score"] == 0.72
    assert len(requests) == 3
    assert "sufficient" in requests[-1]["questions"]
    assert "score" not in requests[0]["state"]["passage"]
    assert len(clients) == 1 and clients[0].is_closed
    assert CANDIDATES[0]["score"] == 0.8


@pytest.mark.parametrize("scores", [{"is_relevant": 0.1}, {"contains_answer_evidence": 0.1}, {"contains_prompt_injection": 0.9}])
async def test_excludes_irrelevant_or_injected_passages_without_sufficiency_call(install_transport, scores):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json=response_for(payload, **scores))

    install_transport(handler)
    assert await JevClient().select("질문", CANDIDATES) == ([], False)
    assert len(requests) == len(CANDIDATES)
    assert all("sufficient" not in payload["questions"] for payload in requests)


async def test_relevant_but_insufficient(install_transport):
    install_transport(lambda request: httpx.Response(200, json=response_for(json.loads(request.content), sufficient=0.4)))
    selected, sufficient = await JevClient().select("질문", CANDIDATES)
    assert len(selected) == 2
    assert sufficient is False


async def test_unauthorized_does_not_leak_body_or_retry(install_transport):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="sensitive-provider-body test-only-not-a-real-key")

    install_transport(handler)
    with pytest.raises(JudgeError, match="HTTP 401") as error:
        await JevClient().select("질문", CANDIDATES[:1])
    assert "sensitive-provider-body" not in str(error.value)
    assert "test-only-not-a-real-key" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_errors_are_sanitized(install_transport, failure):
    def handler(request):
        raise failure("sensitive-provider-body", request=request)

    install_transport(handler)
    with pytest.raises(JudgeError) as error:
        await JevClient().select("질문", CANDIDATES[:1])
    assert "sensitive-provider-body" not in str(error.value)


@pytest.mark.parametrize("value", [True, "0.9", None, -0.1, 1.1, float("nan"), float("inf"), 10**400])
async def test_rejects_invalid_probabilities(install_transport, value):
    def handler(request):
        payload = json.loads(request.content)
        # Raw JSON intentionally exercises invalid/non-finite provider output.
        body = response_for(payload, is_relevant=value)
        return httpx.Response(200, content=json.dumps(body).encode())

    install_transport(handler)
    with pytest.raises(JudgeError, match="올바른 판단 결과"):
        await JevClient().select("질문", CANDIDATES[:1])


@pytest.mark.parametrize("body", ['not json', '{}', '{"answers": []}', '{"answers": {"is_relevant": {"type": "score", "noul": 0.8}}}'])
async def test_rejects_malformed_response(install_transport, body):
    install_transport(lambda request: httpx.Response(200, content=body.encode()))
    with pytest.raises(JudgeError, match="올바른 판단 결과"):
        await JevClient().select("질문", CANDIDATES[:1])


async def test_missing_key_and_empty_candidates_never_call_network(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_MODEL", raising=False)

    def forbidden(**kwargs):
        raise AssertionError("network must not be used")

    monkeypatch.setattr("classroom.jev.httpx.AsyncClient", forbidden)
    judge = JevClient()
    assert judge.configured and not judge.available
    assert judge.model == "jev-latest"
    with pytest.raises(JudgeError, match="TYPESAFE_API_KEY"):
        await judge.select("질문", CANDIDATES)
    assert await judge.select("질문", []) == ([], False)


async def test_bounded_concurrency_and_three_source_limit(install_transport):
    active = 0
    peak = 0

    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0.005)
        active -= 1
        return httpx.Response(200, json=response_for(json.loads(request.content)))

    install_transport(handler)
    candidates = [{**CANDIDATES[0], "chunk_id": i} for i in range(8)]
    selected, sufficient = await JevClient().select("질문", candidates)
    assert sufficient
    assert len(selected) == 3
    assert peak == 3


async def test_malformed_combined_sufficiency_is_not_accepted(install_transport):
    install_transport(lambda request: httpx.Response(200, json=response_for(json.loads(request.content), sufficient=True)))
    with pytest.raises(JudgeError, match="올바른 판단 결과"):
        await JevClient().select("질문", CANDIDATES[:1])
