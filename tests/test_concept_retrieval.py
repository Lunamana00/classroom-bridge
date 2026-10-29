"""Course-grounded GPT selection over the complete approved concept catalog.

Provider calls are stubbed. These tests exercise the service integration and its
source snapshot boundaries, independently of the selector's response parser.
"""
import asyncio
from copy import deepcopy
import json

import pytest

from classroom.judge import JudgeError
from classroom.service import Service


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "codex")
    monkeypatch.setenv("OPEN_API_KEY", "test-concept-retrieval-key")
    monkeypatch.setenv("CONCEPT_EXTRACTION_MODE", "disabled")
    # Old service tests opt into lexical retrieval; these exercise the actual
    # default for configured GPT evidence selection.
    monkeypatch.delenv("CLASSROOM_RETRIEVAL", raising=False)
    count = 0

    def make():
        nonlocal count
        count += 1
        return Service(str(tmp_path / f"concept-retrieval-{count}.sqlite"), seed=False)

    return make


def source(service, course_id, *, title="역전파 강의", text=None, page=7):
    text = text or "# Chain rule\nBackpropagation multiplies upstream gradient by local gradient."
    material = service.add_text(course_id, title, text, "lecture.pdf", [(page, text)])
    chunk = service.get_material(course_id, material["id"])["chunks"][0]
    return {
        "chunk_id": chunk["id"], "material_id": material["id"], "title": title,
        "text": chunk["text"], "page": chunk["page"], "section": chunk["section"],
    }


def concept(service, course_id, name, sources, *, status="approved", aliases=None):
    with service._db() as db:
        concept_id = db.execute(
            "INSERT INTO course_concepts(course_id,name,description,aliases_json,status,"
            "revision,approved_by,approved_at,extraction_model,created_at,updated_at) "
            "VALUES(?,?,?,?,?,1,? ,?,'test-extractor','2026-10-30','2026-10-30')",
            (course_id, name, f"{name}의 자료 기반 설명", json.dumps(aliases or [], ensure_ascii=False),
             status, 1 if status == "approved" else None,
             "2026-10-30" if status == "approved" else None),
        ).lastrowid
        db.executemany("INSERT INTO concept_sources VALUES(?,?)", [
            (concept_id, item["chunk_id"]) for item in sources
        ])
    return concept_id


def selected(catalog, concept_id):
    item = next(item for item in catalog if item["id"] == concept_id)
    return {
        "concepts": [{
            "id": item["id"], "name": item["name"], "description": item["description"],
            "aliases": item["aliases"], "revision": item["revision"],
            "source_chunk_ids": item["source_chunk_ids"], "score": 0.91,
            "reason": "질문의 연쇄법칙을 설명하는 개념입니다.",
        }],
        "evidence": [{**item["sources"][0], "score": 0.91}],
        "sufficient": True,
    }


class SelectorStub:
    configured = True
    available = True
    model = "test-gpt-concept-selector"

    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    async def select(self, question, catalog):
        self.calls.append((question, deepcopy(catalog)))
        await asyncio.sleep(0)
        return self.callback(question, catalog)


def forbid_lexical(*args, **kwargs):
    raise AssertionError("GPT concept retrieval must not prefilter or fall back to lexical retrieval")


async def forbid_selector(*args, **kwargs):
    raise AssertionError("This path must not call the GPT concept selector")


def test_retrieval_catalog_includes_all_live_approved_concepts_only(make_service):
    service = make_service()
    course = service.create_course("개념 검색 수업")
    other = service.create_course("다른 수업")
    own = source(service, course["id"])
    foreign = source(service, other["id"], text="다른 수업의 비공개 원문")
    expected = [concept(service, course["id"], f"승인 개념 {i}", [own]) for i in range(45)]
    for status in ("draft", "rejected", "merged"):
        concept(service, course["id"], f"숨겨진 {status} 개념", [own], status=status)
    concept(service, course["id"], "출처 없는 승인 개념", [])
    concept(service, course["id"], "다른 수업의 출처만 연결된 개념", [foreign])
    concept(service, other["id"], "다른 수업 승인 개념", [foreign])

    catalog = service.concepts.retrieval_catalog(course["id"])

    assert {item["id"] for item in catalog} == set(expected)
    assert len(catalog) == 45
    assert all(item["source_chunk_ids"] == [own["chunk_id"]] for item in catalog)
    assert all(item["sources"] == [own] for item in catalog)
    assert "다른 수업의 비공개 원문" not in json.dumps(catalog, ensure_ascii=False)
    assert "숨겨진" not in json.dumps(catalog, ensure_ascii=False)


@pytest.mark.asyncio
async def test_default_gpt_receives_question_and_complete_catalog_then_preserves_citations(make_service, monkeypatch):
    service = make_service()
    course = service.create_course("개념 검색 수업")
    own = source(service, course["id"])
    ids = [concept(service, course["id"], f"앞쪽 개념 {i}", [own]) for i in range(40)]
    target = concept(service, course["id"], "연쇄법칙", [own], aliases=["chain rule"])
    secret = concept(service, course["id"], "공개 전 개념", [own], status="draft")
    selector = SelectorStub(lambda question, catalog: selected(catalog, target))
    monkeypatch.setattr(service, "gpt_concept_selector", selector, raising=False)
    monkeypatch.setattr(service, "_retrieve", forbid_lexical)
    monkeypatch.setattr(service.judge, "select", forbid_selector)
    question = "오차 신호를 이전 층으로 보낼 때 두 미분값을 곱하는 까닭을 설명해 줘."

    result = await service.ask(course["id"], question)

    assert len(selector.calls) == 1
    assert selector.calls[0][0] == question
    assert {item["id"] for item in selector.calls[0][1]} == {*ids, target}
    assert secret not in {item["id"] for item in selector.calls[0][1]}
    assert result["retrieval_method"] == "gpt_all_concepts"
    assert result["concept_catalog_size"] == 41
    assert result["sufficient"] is True
    assert result["concepts"][0]["id"] == target
    assert result["concepts"][0]["aliases"] == ["chain rule"]
    assert result["evidence"] == [{**own, "score": 0.91}]
    assert own["text"] in result["answer"]
    assert "7쪽" in result["answer"]
    assert "selected_concepts" in result["prompt"]
    assert "연쇄법칙" in result["prompt"] and "chain rule" in result["prompt"]
    assert "공개 전 개념" not in result["prompt"]


@pytest.mark.asyncio
async def test_selected_concepts_and_retrieval_metadata_survive_restart(make_service, monkeypatch):
    service = make_service()
    course = service.create_course("기록 수업")
    own = source(service, course["id"])
    target = concept(service, course["id"], "연쇄법칙", [own])
    monkeypatch.setattr(service, "gpt_concept_selector", SelectorStub(
        lambda question, catalog: selected(catalog, target)), raising=False)

    result = await service.ask(course["id"], "왜 미분값을 곱해?")
    restarted = Service(service.db_path, seed=False)

    with restarted._db() as db:
        row = db.execute("SELECT concepts_json,retrieval_method,concept_catalog_size,evidence_json "
                         "FROM questions WHERE id=?", (result["question_id"],)).fetchone()
        assert db.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    assert json.loads(row["concepts_json"]) == result["concepts"]
    assert json.loads(row["evidence_json"]) == result["evidence"]
    assert row["retrieval_method"] == "gpt_all_concepts"
    assert row["concept_catalog_size"] == 1


@pytest.mark.asyncio
async def test_no_approved_concepts_abstains_without_lexical_fallback(make_service, monkeypatch):
    service = make_service()
    course = service.create_course("승인 대기 수업")
    own = source(service, course["id"], text="경사하강법은 기울기 반대 방향으로 이동합니다.")
    concept(service, course["id"], "경사하강법", [own], status="draft")
    selector = SelectorStub(lambda *args: None)
    selector.select = forbid_selector
    monkeypatch.setattr(service, "gpt_concept_selector", selector, raising=False)
    monkeypatch.setattr(service, "_retrieve", forbid_lexical)
    monkeypatch.setattr(service.judge, "select", forbid_selector)

    result = await service.ask(course["id"], "경사하강법 알려줘")

    assert result["sufficient"] is False
    assert result["evidence"] == [] and result["concepts"] == []
    assert result["retrieval_method"] == "gpt_all_concepts"
    assert result["concept_catalog_size"] == 0
    assert "개념" in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "selected_revision", "unselected_revision", "selected_status",
    "source_text", "material_deleted", "new_approved_concept",
])
async def test_catalog_changes_while_awaiting_gpt_fail_without_question_history(make_service, monkeypatch, change):
    service = make_service()
    course = service.create_course("동시 변경 수업")
    own = source(service, course["id"])
    other_source = source(service, course["id"], title="다른 개념 자료", text="별도 개념의 원문입니다.")
    target = concept(service, course["id"], "연쇄법칙", [own])
    unselected = concept(service, course["id"], "다른 승인 개념", [other_source])

    def mutate_then_return(question, catalog):
        result = selected(catalog, target)
        with service._db() as db:
            if change in {"selected_revision", "unselected_revision"}:
                changed_id = target if change == "selected_revision" else unselected
                db.execute("UPDATE course_concepts SET description='수정된 설명',revision=revision+1 WHERE id=?", (changed_id,))
            elif change == "selected_status":
                db.execute("UPDATE course_concepts SET status='rejected',revision=revision+1 WHERE id=?", (target,))
            elif change == "source_text":
                db.execute("UPDATE chunks SET text='변경된 원문' WHERE id=?", (own["chunk_id"],))
            elif change == "material_deleted":
                db.execute("DELETE FROM materials WHERE id=?", (own["material_id"],))
        if change == "new_approved_concept":
            concept(service, course["id"], "뒤늦게 승인된 개념", [other_source])
        return result

    monkeypatch.setattr(service, "gpt_concept_selector", SelectorStub(mutate_then_return), raising=False)
    monkeypatch.setattr(service, "_retrieve", forbid_lexical)

    with pytest.raises(JudgeError):
        await service.ask(course["id"], "두 미분값을 곱하는 이유는?")

    with service._db() as db:
        assert db.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,explicit_lexical", [("demo", False), ("codex", True), ("jev", False)])
async def test_legacy_modes_use_lexical_candidates(make_service, monkeypatch, mode, explicit_lexical):
    monkeypatch.setenv("CLASSROOM_MODE", mode)
    if explicit_lexical:
        monkeypatch.setenv("CLASSROOM_RETRIEVAL", "lexical")
    service = make_service()
    course = service.create_course("기존 검색 수업")
    own = source(service, course["id"], text="경사하강법은 기울기 반대 방향으로 이동합니다.")
    calls = []

    async def legacy_judge(question, candidates):
        calls.append(candidates)
        return candidates[:1], True

    monkeypatch.setattr(service.judge, "select", legacy_judge)
    selector = SelectorStub(lambda *args: None)
    selector.select = forbid_selector
    monkeypatch.setattr(service, "gpt_concept_selector", selector, raising=False)

    result = await service.ask(course["id"], "경사하강법 알려줘")

    assert result["retrieval_method"] == "lexical"
    assert result["concepts"] == []
    assert result["concept_catalog_size"] == 0
    assert result["evidence"][0]["chunk_id"] == own["chunk_id"]
    assert bool(calls) == (mode != "demo")
