"""HTTP lifecycle, teacher-only review, and unchanged question retrieval."""
import asyncio

import pytest

from classroom.app import create_app
from conftest import authenticate, authenticated_client


class Extractor:
    configured = True
    model = "test-concept-model"

    async def extract(self, chunks, existing_concepts):
        return [{
            "name": "경사하강법", "description": "손실의 기울기 반대 방향으로 매개변수를 갱신하는 방법.",
            "aliases": ["gradient descent"],
            "source_chunk_ids": [chunks[0]["chunk_id"]], "merge_target_id": None,
        }]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "demo")
    app = create_app(str(tmp_path / "concept-http.sqlite"), seed=False)
    client = authenticated_client(app)
    course_id = client.post("/api/courses", json={"title": "최적화"}).json()["id"]
    yield app, client, course_id
    client.close()


def upload(client, course_id):
    response = client.post(f"/api/courses/{course_id}/materials/text", json={
        "title": "경사하강법 강의", "text": "# 경사하강법\n\n경사하강법은 손실 기울기의 반대 방향으로 매개변수를 갱신합니다.",
    })
    assert response.status_code == 201, response.text
    return response.json()["id"]


def extract(app):
    assert asyncio.run(app.state.service.concepts.process_next(Extractor()))


def test_upload_extract_review_approve_and_original_search(workspace):
    app, client, course_id = workspace
    material_id = upload(client, course_id)
    path = f"/api/courses/{course_id}/concepts"
    before = app.state.service._retrieve(course_id, "경사하강법이 뭐야?")
    overview = client.get(path).json()
    assert overview["jobs"][0]["material_id"] == material_id
    assert overview["concepts"] == []
    extract(app)
    draft = client.get(path).json()["concepts"][0]
    assert draft["status"] == "draft"
    assert draft["sources"][0]["material_id"] == material_id
    detail = f"{path}/{draft['id']}"
    payload = {
        "expected_revision": draft["revision"], "name": "경사 하강법",
        "description": draft["description"], "aliases": draft["aliases"],
        "source_chunk_ids": draft["source_chunk_ids"], "merge_target_id": None,
    }
    edited = client.put(detail, json=payload)
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "draft"
    stale = client.post(detail + "/approve", json={"expected_revision": draft["revision"]})
    assert stale.status_code == 400
    approved = client.post(detail + "/approve", json={"expected_revision": edited.json()["revision"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by"]
    assert app.state.service._retrieve(course_id, "경사하강법이 뭐야?") == before
    original = client.get(f"/api/courses/{course_id}/materials/{material_id}").json()
    assert original["chunks"][0]["text"] == draft["sources"][0]["text"]


def test_student_cannot_read_review_or_queue_extraction(workspace):
    app, client, course_id = workspace
    material_id = upload(client, course_id)
    extract(app)
    draft = client.get(f"/api/courses/{course_id}/concepts").json()["concepts"][0]
    user = authenticate(client, email="student@example.test")
    with app.state.service._db() as db:
        db.execute("INSERT INTO course_members VALUES(?,?,'student')", (course_id, user["id"]))
    path = f"/api/courses/{course_id}/concepts"
    assert client.get(path).status_code == 403
    assert client.get(f"{path}/{draft['id']}").status_code == 403
    assert client.post(f"{path}/{draft['id']}/approve", json={"expected_revision": draft["revision"]}).status_code == 403
    assert client.post(f"/api/courses/{course_id}/materials/{material_id}/extract-concepts").status_code == 403
    assert client.get(f"/api/courses/{course_id}/materials/{material_id}").status_code == 200
    assert client.post("/api/ask", json={"course_id": course_id, "question": "경사하강法이 뭐야?"}).status_code == 200


def test_course_scope_source_validation_and_csrf(workspace):
    app, client, course_id = workspace
    material_id = upload(client, course_id)
    extract(app)
    draft = client.get(f"/api/courses/{course_id}/concepts").json()["concepts"][0]
    other = client.post("/api/courses", json={"title": "별도 수업"}).json()["id"]
    other_material = upload(client, other)
    other_chunk = client.get(f"/api/courses/{other}/materials/{other_material}").json()["chunks"][0]["id"]
    assert client.get(f"/api/courses/{other}/concepts/{draft['id']}").status_code == 404
    assert client.post(f"/api/courses/{other}/materials/{material_id}/extract-concepts").status_code == 404
    detail = f"/api/courses/{course_id}/concepts/{draft['id']}"
    payload = {"expected_revision": draft["revision"], "name": draft["name"], "description": draft["description"], "aliases": [], "source_chunk_ids": [other_chunk], "merge_target_id": None}
    assert client.put(detail, json=payload).status_code == 400
    client.headers.pop("X-CSRF-Token")
    assert client.post(detail + "/approve", json={"expected_revision": draft["revision"]}).status_code == 403


def test_extraction_unconfigured_retry_and_deleted_source(workspace):
    app, client, course_id = workspace
    material_id = upload(client, course_id)
    assert asyncio.run(app.state.service.concepts.process_next(app.state.service.concept_extractor))
    path = f"/api/courses/{course_id}/concepts"
    job = client.get(path).json()["jobs"][0]
    assert job["status"] == "not_configured"
    assert client.post(f"/api/courses/{course_id}/materials/{material_id}/extract-concepts").status_code == 202
    extract(app)
    draft = client.get(path).json()["concepts"][0]
    assert client.delete(f"/api/courses/{course_id}/materials/{material_id}").status_code == 200
    deleted = client.get(f"{path}/{draft['id']}").json()
    assert deleted["available"] is False
    assert client.post(f"{path}/{draft['id']}/approve", json={"expected_revision": draft["revision"]}).status_code == 400
