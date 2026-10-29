"""Student HTTP requests use the full approved concept catalog by default."""
import asyncio
import copy
import json

import pytest

from classroom.app import create_app
from classroom.judge import JudgeError
from conftest import authenticate, authenticated_client


class Extractor:
    configured = True
    model = "private-extraction-model"

    async def extract(self, chunks, existing_concepts):
        return [
            {"name": "연쇄법칙", "description": "합성함수의 미분은 각 함수의 미분을 곱해 구합니다.",
             "aliases": ["chain rule"], "source_chunk_ids": [chunks[0]["chunk_id"]], "merge_target_id": None},
            {"name": "검토 전 비공개 개념", "description": "아직 교수가 검토하지 않은 설명입니다.",
             "aliases": [], "source_chunk_ids": [chunks[0]["chunk_id"]], "merge_target_id": None},
        ]


class Selector:
    model = "fake-gpt-selector"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def select(self, question, concepts):
        self.calls.append((question, copy.deepcopy(concepts)))
        if self.error:
            raise self.error
        concept = concepts[0]
        selected = {key: value for key, value in concept.items() if key != "sources"}
        return {
            "concepts": [{**selected, "score": 0.96, "reason": "합성함수의 도함수를 구하는 데 필요합니다."}],
            "evidence": [{**concept["sources"][0], "score": 0.95}],
            "sufficient": True,
        }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("CLASSROOM_RETRIEVAL", raising=False)
    monkeypatch.setenv("CLASSROOM_MODE", "codex")
    monkeypatch.setenv("OPEN_API_KEY", "test")
    # Keep the absent retrieval setting absent even if a developer has .env.local.
    monkeypatch.setattr("classroom.config.ROOT", tmp_path)

    def no_sdk(*args, **kwargs):
        pytest.fail("HTTP integration tests must not create a provider client")

    monkeypatch.setattr("classroom.judge.AsyncOpenAI", no_sdk)
    app = create_app(str(tmp_path / "concept-api.sqlite"), seed=False)
    service = app.state.service
    assert service.retrieval_mode == "concepts"
    assert service._uses_concepts()
    assert not service.concept_extractor.configured
    client = authenticated_client(app)
    course = client.post("/api/courses", json={"title": "미분학"}).json()["id"]
    other = client.post("/api/courses", json={"title": "다른 수업"}).json()["id"]
    uploaded = client.post(f"/api/courses/{course}/materials/text", json={
        "title": "합성함수 미분 강의", "text": "# 연쇄법칙\n\n합성함수 y=f(g(x))의 도함수는 f'(g(x))g'(x)입니다.",
    })
    assert uploaded.status_code == 201, uploaded.text
    assert asyncio.run(service.concepts.process_next(Extractor()))
    drafts = client.get(f"/api/courses/{course}/concepts").json()["concepts"]
    draft = next(item for item in drafts if item["name"] == "연쇄법칙")
    approval = client.post(f"/api/courses/{course}/concepts/{draft['id']}/approve",
                           json={"expected_revision": draft["revision"]})
    assert approval.status_code == 200, approval.text
    approved = approval.json()
    assert approved["approved_by"] and approved["extraction_model"] == Extractor.model
    student = authenticate(client, email="student@example.test")
    with service._db() as db:
        db.execute("INSERT INTO course_members VALUES(?,?,'student')", (course, student["id"]))
    yield app, client, course, other, approved
    client.close()


def test_student_ask_uses_default_gpt_catalog_and_returns_public_concepts(workspace):
    app, client, course, _, approved = workspace
    selector = Selector()
    app.state.service.gpt_concept_selector = selector
    question = "여러 함수가 안쪽에 겹쳐 있을 때 도함수는 어떻게 구해?"
    response = client.post("/api/ask", json={"course_id": course, "question": question})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["retrieval_method"] == "gpt_all_concepts"
    assert result["mode"] == "codex" and result["sufficient"] is True
    assert result["concept_catalog_size"] == 1
    assert len(selector.calls) == 1 and selector.calls[0][0] == question
    catalog = selector.calls[0][1]
    assert [concept["id"] for concept in catalog] == [approved["id"]]
    assert set(catalog[0]) == {"id", "name", "description", "aliases", "revision", "source_chunk_ids", "sources"}
    assert result["concepts"][0]["id"] == approved["id"]
    assert result["concepts"][0]["reason"] == "합성함수의 도함수를 구하는 데 필요합니다."
    assert set(result["concepts"][0]) == {"id", "name", "description", "aliases", "revision", "source_chunk_ids", "score", "reason"}
    assert result["evidence"] == [{**catalog[0]["sources"][0], "score": 0.95}]
    assert result["evidence"][0]["text"] == approved["sources"][0]["text"]
    for private in ("검토 전 비공개 개념", "approved_by", "approved_at", "reviewed_by", "extraction_model", Extractor.model):
        assert private not in response.text
    with app.state.service._db() as db:
        saved = db.execute("SELECT * FROM questions WHERE id=?", (result["question_id"],)).fetchone()
        assert json.loads(saved["concepts_json"]) == result["concepts"]
        assert json.loads(saved["evidence_json"]) == result["evidence"]
        assert saved["retrieval_method"] == "gpt_all_concepts"


def test_foreign_course_denied_and_selector_failure_does_not_record_question(workspace):
    app, client, course, other, _ = workspace
    selector = Selector(JudgeError("GPT 개념 선택 응답 시간이 초과되었습니다. 다시 시도해 주세요."))
    app.state.service.gpt_concept_selector = selector
    forbidden = client.post("/api/ask", json={"course_id": other, "question": "다른 수업의 개념을 보여줘"})
    assert forbidden.status_code in {403, 404}
    assert not selector.calls
    response = client.post("/api/ask", json={"course_id": course, "question": "합성함수는 어떻게 미분해?"})
    assert response.status_code == 502, response.text
    assert response.json()["detail"] == str(selector.error)
    assert len(selector.calls) == 1
    with app.state.service._db() as db:
        assert db.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
