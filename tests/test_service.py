import json
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
    material = service.list_materials(course["id"])[0]
    original = service.get_material(course["id"], material["id"])["chunks"]
    assert [source["chunk_id"] for source in context["source_excerpts"]] == [source["id"] for source in original[:60]]
    assert "전체 강의 범위를 대표하지" in service.exam_plan(course["id"])["notice"]
    assert "전체 강의 범위를 대표하지" in service.teaching_brief(course["id"])["summary"]


def test_large_course_catalog_and_excerpts_reach_later_lectures_and_code(service):
    course = service.create_course("CS231n 2026")
    first = service.add_text(course["id"], "긴 첫 강의", "\n\n".join(f"## 도입 {i}\n첫 강의 내용 {i}" for i in range(80)), "lecture_1.pdf")
    later = [service.add_text(course["id"], title, text, filename) for title, text, filename in [
        ("CNN", "합성곱 신경망의 계산입니다.", "lecture_5.pdf"),
        ("Transformer", "Attention의 계산입니다.", "lecture_8.pdf"),
    ]]
    code = [service.add_text(course["id"], f"코드 셀 {i}", f"result_{i} = {i}\n", f"cell_{i}.txt") for i in range(80)]
    context = service._course_context(course["id"])
    catalog = context["material_catalog"]
    assert [row["id"] for row in catalog] == [first["id"], *[row["id"] for row in later + code]]
    assert catalog[0] == {"id": first["id"], "title": "긴 첫 강의", "filename": "lecture_1.pdf", "chunk_count": 80}
    selected = {source["material_id"] for source in context["source_excerpts"]}
    assert {first["id"], *[row["id"] for row in later], code[0]["id"], code[-1]["id"]} <= selected
    assert len(context["source_excerpts"]) == 60
    assert context["source_scope"]["total_materials"] == 83
    assert context["source_scope"]["included_materials"] == len(selected)
    assert context["source_scope"]["truncated"]


def test_large_course_prioritizes_live_recent_and_quiz_sources_without_crossing_courses(service):
    from classroom.auth import Auth

    auth = Auth(service)
    teacher = auth.login_user({"sub": "context-teacher", "email": "teacher@example.test", "email_verified": True, "name": "교수"})
    course = service.create_course("CS231n 2026", owner_id=teacher["id"])
    early = service.add_text(course["id"], "첫 강의", "\n\n".join(f"## 앞쪽 {i}\n첫 강의 내용 {i}" for i in range(80)), "lecture_1.pdf")
    late = service.add_text(course["id"], "오늘 역전파", "\n\n".join(f"## 뒤쪽 {i}\n오늘 강의 내용 {i}" for i in range(80)), "lecture_4.pdf")
    quiz_source = service.get_material(course["id"], early["id"])["chunks"][-1]
    recent_source = service.get_material(course["id"], late["id"])["chunks"][-1]
    foreign_material = service.list_materials(1)[0]
    foreign_source = service.get_material(1, foreign_material["id"])["chunks"][0]
    service.quizzes.create_draft(course["id"], teacher["id"], {
        "title": "현재 검토 중인 문제", "concepts": ["출처 확인"], "question": "어느 구절인가요?",
        "kind": "short_answer", "correct_answer": "끝 구절", "options": [], "accepted_answers": [],
        "explanation": "실제 끝 구절을 확인합니다.", "source_chunk_ids": [quiz_source["id"]],
    })
    with service._db() as db:
        db.execute(
            "INSERT INTO questions(course_id,question,topic,created_at,sufficient,mode,evidence_json) VALUES(?,?,?,?,1,'demo',?)",
            (course["id"], "오늘 질문", "역전파", "2026-10-30T12:00:00+09:00", json.dumps([
                {"chunk_id": recent_source["id"], "text": "기록 안의 원문 대체 문장은 사용하지 않는다"},
                {"chunk_id": foreign_source["id"]}, {"chunk_id": 999999},
            ])),
        )
    context = service._course_context(course["id"])
    selected = {source["chunk_id"]: source for source in context["source_excerpts"]}
    assert recent_source["id"] in selected and quiz_source["id"] in selected
    assert selected[recent_source["id"]]["text"] == recent_source["text"]
    assert foreign_source["id"] not in selected and 999999 not in selected
    assert context["source_scope"]["priority_chunks"] == 2
    assert len(context["source_excerpts"]) == 60


def test_retrieval_keeps_review_material_among_long_lecture_matches(service):
    course = service.create_course("CS231n 2026")
    text = "Backpropagation uses upstream gradient times local gradient."
    lecture = service.add_text(course["id"], "CS231n 2026 Backpropagation lecture", text, "lecture.pdf", [(page, text) for page in range(1, 10)])
    review = service.add_text(course["id"], "CS231n 2026 Backpropagation review", text, "review.pdf", [(6, text), (7, text)])
    foreign = service.create_course("별도 수업")
    service.add_text(foreign["id"], "CS231n 2026 Backpropagation review", text)
    question = "배운 역전파가 아직 헷갈려. upstream gradient와 local gradient를 왜 곱하는지 계산 예시로 설명해 줘."
    found = service._retrieve(course["id"], question)
    contextual = service._retrieve(course["id"], "CS231n 2026에서 " + question)
    assert len(found) == 6
    assert {row["material_id"] for row in found} == {lecture["id"], review["id"]}
    assert {row["page"] for row in found if row["material_id"] == review["id"]} == {6, 7}
    assert [(row["chunk_id"], row["score"]) for row in contextual] == [(row["chunk_id"], row["score"]) for row in found]


def test_natural_korean_concepts_find_english_evidence_but_course_name_is_not_evidence(service):
    course = service.create_course("CS231n 2026")
    relu = service.add_text(course["id"], "CS231n 2026 ReLU", "ReLU has zero gradient for negative input and passes the gradient for positive input.")
    attention = service.add_text(course["id"], "CS231n 2026 Transformer", "Transformer self-attention combines information from the input.")
    question = "이 코드에서 ReLU를 지난 뒤 음수 입력의 기울기를 0으로 만드는 이유가 뭐야?"
    found = service._retrieve(course["id"], question)
    assert found and found[0]["material_id"] == relu["id"]
    question = "Transformer의 self-attention은 입력 안에서 어떤 정보를 섞는 건가요? 수업 자료로 설명해 주세요."
    found = service._retrieve(course["id"], question)
    assert found and found[0]["material_id"] == attention["id"]
    assert service._retrieve(course["id"], "CS231n 2026의 다음 시험 날짜와 시험 강의실을 알려줘") == []


def test_retrieval_single_material_can_still_supply_six_candidates(service):
    course = service.create_course("한 자료 수업")
    text = "Backpropagation uses the upstream gradient and local gradient."
    material = service.add_text(course["id"], "Backpropagation", text, "review.pdf", [(page, text) for page in range(1, 9)])
    sources = service.get_material(course["id"], material["id"])["chunks"]
    found = service._retrieve(course["id"], "upstream gradient local gradient")
    assert [row["chunk_id"] for row in found] == [row["id"] for row in sources[:6]]
