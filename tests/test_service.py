import sqlite3

import pytest

from classroom.judge import JudgeError
from classroom.service import Service


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "demo")
    return Service(str(tmp_path / "nested" / "classroom.sqlite"))


def test_seed_is_durable_idempotent_and_separates_synthetic_data(service):
    before = service.analytics(1)
    restarted = Service(service.db_path)
    assert len(restarted.list_courses()) == 1
    assert len(restarted.list_materials(1)) == 4
    assert restarted.analytics(1)["total_questions"] == before["total_questions"]
    assert before["demo_questions"] == 12
    assert before["real_questions"] == 0
    assert all(q["is_demo"] for q in before["recent_questions"])


@pytest.mark.asyncio
async def test_korean_question_finds_verbatim_source_and_records_once(service):
    result = await service.ask(1, "경사하강법이 뭐야?")
    assert result["mode"] == "demo"
    assert result["sufficient"] is True
    assert result["evidence"][0]["section"] == "경사하강법의 직관"
    assert "생성된 답변이 아닙니다" in result["answer"]
    assert "[1]" in result["answer"]
    for evidence in result["evidence"]:
        source = service.get_material(1, evidence["material_id"])
        assert evidence["text"] in [chunk["text"] for chunk in source["chunks"]]
    stats = service.analytics(1)
    assert stats["total_questions"] == 13
    assert stats["demo_questions"] == 12
    assert stats["real_questions"] == 1
    assert stats["recent_questions"][0]["is_demo"] is False


@pytest.mark.asyncio
async def test_out_of_scope_question_abstains(service):
    result = await service.ask(1, "내일 서울 날씨 알려줘")
    assert not result["sufficient"]
    assert result["evidence"] == []
    assert "충분한 근거" in result["answer"]
    assert "근거 부족" in result["prompt"]
    assert result["topic"] == "자료 밖 질문"


@pytest.mark.asyncio
async def test_course_isolation_material_deletion_and_no_evidence(service):
    other = service.create_course("별도 수업")
    material = service.add_text(other["id"], "천체", "블랙홀은 중력이 강한 천체이다.")
    for operation in (service.get_material, service.delete_material):
        with pytest.raises(LookupError):
            operation(1, material["id"])
    assert not (await service.ask(1, "블랙홀 알려줘"))["sufficient"]
    assert (await service.ask(other["id"], "블랙홀 알려줘"))["sufficient"]
    service.delete_material(other["id"], material["id"])
    assert not (await service.ask(other["id"], "블랙홀 알려줘"))["sufficient"]
    assert service.analytics(other["id"])["total_questions"] == 2
    with sqlite3.connect(service.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chunks WHERE material_id=?", (material["id"],)).fetchone()[0] == 0


def test_page_numbers_and_sections_survive_ingestion(service):
    pages = [(1, "# 첫 절\n첫 페이지의 내용입니다."), (4, "# 다음 절\n네 번째 페이지입니다.")]
    material = service.add_text(1, "페이지 자료", "\n".join(text for _, text in pages), "pages.pdf", pages)
    chunks = service.get_material(1, material["id"])["chunks"]
    assert [(chunk["page"], chunk["section"]) for chunk in chunks] == [(1, "첫 절"), (4, "다음 절")]


def test_teaching_and_exam_handoffs_are_grounded_and_do_not_claim_distribution(service):
    brief = service.teaching_brief(1)
    assert "합성" in brief["summary"]
    assert "source_excerpts" in brief["prompt"]
    assert brief["focus_topics"]
    for total in (1, 2, 7, 10, 50):
        plan = service.exam_plan(1, total)
        assert sum(item["count"] for item in plan["distribution"]) == total
        assert sum(item["count"] for item in plan["topics"]) == total
        assert "정규분포" in plan["notice"]
        assert "보장하지" in plan["notice"]
        assert "source_excerpts" in plan["prompt"]
    empty = service.create_course("자료 없는 수업")
    with pytest.raises(ValueError):
        service.exam_plan(empty["id"])


@pytest.mark.asyncio
async def test_judge_failure_never_silently_falls_back(service, monkeypatch):
    class FailingJudge:
        provider = "codex"
        configured = True
        model = "test"

        async def select(self, question, candidates):
            raise JudgeError("판단 실패")

    service.judge = FailingJudge()
    before = service.analytics(1)["total_questions"]
    with pytest.raises(JudgeError):
        await service.ask(1, "경사하강법이 뭐야?")
    assert service.analytics(1)["total_questions"] == before


@pytest.mark.asyncio
async def test_input_limits_and_missing_course(service):
    with pytest.raises(ValueError):
        await service.ask(1, "   ")
    with pytest.raises(ValueError):
        await service.ask(1, "가" * 2001)
    with pytest.raises(LookupError):
        await service.ask(999999, "경사하강법?")
    with pytest.raises(ValueError):
        service.add_text(1, "자료", "")
    with pytest.raises(ValueError):
        service.exam_plan(1, 0)


def test_large_course_discloses_prompt_scope_limit(service):
    course = service.create_course("많은 자료")
    service.add_text(course["id"], "많은 절", "\n\n".join(f"## 절 {i}\n범위 검증용 본문 {i}" for i in range(65)))
    context = service._course_context(course["id"])
    assert context["source_scope"]["total_chunks"] == 65
    assert context["source_scope"]["included_chunks"] == 60
    assert context["source_scope"]["truncated"] is True
    assert all("chunk_id" in source for source in context["source_excerpts"])
    assert "전체 강의 범위를 대표하지" in service.exam_plan(course["id"])["notice"]
    assert "전체 강의 범위를 대표하지" in service.teaching_brief(course["id"])["summary"]
