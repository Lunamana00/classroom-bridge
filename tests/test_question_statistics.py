import pytest
from fastapi.testclient import TestClient

from classroom.app import create_app
from conftest import authenticate


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "demo")
    app = create_app(str(tmp_path / "statistics.sqlite"), seed=False)
    with TestClient(app) as client:
        authenticate(client)
        course_id = client.post("/api/courses", json={"title": "질문 통계 수업"}).json()["id"]
        other_id = client.post("/api/courses", json={"title": "다른 수업"}).json()["id"]
        topic_ids = []
        with app.state.service._db() as db:
            for index in range(25):
                topic_ids.append(db.execute(
                    "INSERT INTO questions(course_id,question,topic,created_at,sufficient,is_demo,mode) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (course_id, f"질문 {index}", "경사하강법", f"2026-10-{29 if index < 20 else 30}T10:00:00+09:00",
                     int(index % 3 != 0), int(index % 4 == 0), "demo"),
                ).lastrowid)
            for topic, sufficient, is_demo in [
                ("경사하강법 응용", 1, 0), ("다른 주제", 0, 1), ("", 1, 0), ("x' OR 1=1 --", 0, 0),
            ]:
                db.execute(
                    "INSERT INTO questions(course_id,question,topic,created_at,sufficient,is_demo,mode) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (course_id, f"{topic} 질문", topic, "2026-10-30T11:00:00+09:00", sufficient, is_demo, "codex"),
                )
            db.execute(
                "INSERT INTO questions(course_id,question,topic,created_at,sufficient,is_demo,mode) "
                "VALUES (?,?,?,?,?,?,?)",
                (other_id, "다른 수업의 비공개 질문", "경사하강법", "2026-10-30T12:00:00+09:00", 1, 0, "demo"),
            )
        yield app, client, course_id, other_id, topic_ids


def endpoint(course_id):
    return f"/api/courses/{course_id}/question-statistics"


def totals(data):
    return {key: data[key] for key in (
        "total_questions", "grounded_questions", "insufficient_questions", "real_questions", "demo_questions",
    )}


def test_complete_totals_are_independent_of_default_page_and_course(workspace):
    _, client, course_id, other_id, _ = workspace
    response = client.get(endpoint(course_id))
    assert response.status_code == 200
    data = response.json()
    assert totals(data) == {
        "total_questions": 29, "grounded_questions": 18, "insufficient_questions": 11,
        "real_questions": 21, "demo_questions": 8,
    }
    assert len(data["questions"]) == 20
    assert (data["offset"], data["limit"], data["has_more"]) == (0, 20, True)
    for question in data["questions"]:
        assert set(question) == {"id", "question", "topic", "created_at", "sufficient", "is_demo", "mode"}
        assert isinstance(question["sufficient"], bool)
        assert isinstance(question["is_demo"], bool)
        assert question["question"] != "다른 수업의 비공개 질문"
    other = client.get(endpoint(other_id)).json()
    assert other["total_questions"] == 1
    assert [row["question"] for row in other["questions"]] == ["다른 수업의 비공개 질문"]
    # Existing graph consumers keep the original topic/count schema.
    topics = client.get(f"/api/courses/{course_id}/analytics").json()["topics"]
    assert all(set(topic) == {"topic", "count"} for topic in topics)
    assert {row["topic"]: row["count"] for row in topics}["경사하강법"] == 25


def test_exact_topic_filter_and_parameterized_special_characters(workspace):
    _, client, course_id, _, _ = workspace
    data = client.get(endpoint(course_id), params={"topic": "경사하강법"}).json()
    assert totals(data) == {
        "total_questions": 25, "grounded_questions": 16, "insufficient_questions": 9,
        "real_questions": 18, "demo_questions": 7,
    }
    assert {row["topic"] for row in data["questions"]} == {"경사하강법"}
    for topic in ("경사하강법 응용", "", "x' OR 1=1 --"):
        filtered = client.get(endpoint(course_id), params={"topic": topic}).json()
        assert filtered["total_questions"] == 1
        assert [row["topic"] for row in filtered["questions"]] == [topic]
        assert filtered["has_more"] is False


def test_pagination_covers_all_questions_with_stable_tie_ordering(workspace):
    _, client, course_id, _, topic_ids = workspace
    first = client.get(endpoint(course_id), params={"topic": "경사하강법"}).json()
    second = client.get(endpoint(course_id), params={"topic": "경사하강법", "offset": 20}).json()
    assert [len(first["questions"]), len(second["questions"])] == [20, 5]
    assert [row["id"] for row in first["questions"] + second["questions"]] == list(reversed(topic_ids))
    assert first["has_more"] is True and second["has_more"] is False
    assert totals(first) == totals(second)
    beyond = client.get(endpoint(course_id), params={"topic": "경사하강법", "offset": 100}).json()
    assert beyond["questions"] == [] and beyond["has_more"] is False
    assert totals(beyond) == totals(first)
    complete = client.get(endpoint(course_id), params={"limit": 100}).json()
    assert len(complete["questions"]) == 29
    assert complete["limit"] == 100 and complete["has_more"] is False


def test_missing_topic_and_empty_course_return_zero_counts(workspace):
    _, client, course_id, _, _ = workspace
    empty_id = client.post("/api/courses", json={"title": "빈 수업"}).json()["id"]
    for selected_course, params in [(course_id, {"topic": "존재하지 않는 주제"}), (empty_id, {})]:
        response = client.get(endpoint(selected_course), params=params)
        assert response.status_code == 200
        data = response.json()
        assert all(value == 0 for value in totals(data).values())
        assert data["questions"] == [] and data["has_more"] is False


@pytest.mark.parametrize("params", [
    {"limit": 0}, {"limit": 101}, {"limit": -1}, {"limit": "all"}, {"limit": "1.5"},
    {"offset": -1}, {"offset": "1.5"}, {"offset": "next"},
])
def test_invalid_pagination_is_rejected(workspace, params):
    _, client, course_id, _, _ = workspace
    assert client.get(endpoint(course_id), params=params).status_code == 422


def test_student_nonmember_and_anonymous_cannot_read_statistics(workspace):
    app, teacher, course_id, _, _ = workspace
    student, nonmember, anonymous = TestClient(app), TestClient(app), TestClient(app)
    try:
        authenticate(student, "student@example.test")
        authenticate(nonmember, "other-teacher@example.test")
        invite = teacher.post(f"/api/courses/{course_id}/invite").json()["url"].split("#join=")[1]
        assert student.post("/api/join", json={"token": invite}).status_code == 200
        assert student.get(f"/api/courses/{course_id}/materials").status_code == 200
        assert student.get(endpoint(course_id)).status_code == 403
        assert nonmember.get(endpoint(course_id)).status_code == 403
        assert anonymous.get(endpoint(course_id)).status_code == 401
    finally:
        for client in (student, nonmember, anonymous):
            client.close()


@pytest.mark.parametrize("kwargs", [
    {"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 101}, {"limit": True}, {"topic": 123},
])
def test_service_rejects_invalid_arguments(workspace, kwargs):
    app, _, course_id, _, _ = workspace
    with pytest.raises(ValueError):
        app.state.service.question_statistics(course_id, **kwargs)
