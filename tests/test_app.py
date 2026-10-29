import io
import os
import sys
from pathlib import Path

import pytest
from conftest import authenticated_client as TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from classroom.app import create_app
from classroom.mcp_server import ROOT, database_path


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "demo")
    with TestClient(create_app(str(tmp_path / "test.sqlite"), seed=False), base_url="http://127.0.0.1:8000") as client:
        yield client


def make_course(client, title="최적화 수업"):
    response = client.post("/api/courses", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_upload_question_insights_persistence_and_delete(client):
    course_id = make_course(client)
    other_id = make_course(client, "별도 수업")
    content = "# 경사하강법\n\n경사하강법은 손실함수의 기울기 반대 방향으로 매개변수를 업데이트하는 최적화 방법입니다. 학습률은 이동하는 크기를 결정합니다."
    upload = client.post(f"/api/courses/{course_id}/materials", files={"file": ("강의노트.md", content.encode(), "text/markdown")})
    assert upload.status_code == 201, upload.text
    material_id = upload.json()["id"]
    assert client.get(f"/api/courses/{other_id}/materials/{material_id}").status_code == 404
    assert client.delete(f"/api/courses/{other_id}/materials/{material_id}").status_code == 404

    answer = client.post("/api/ask", json={"course_id": course_id, "question": "경사하강법이 뭐야?"})
    assert answer.status_code == 200, answer.text
    data = answer.json()
    assert data["mode"] == "demo"
    assert data["evidence"] and "경사하강법" in data["evidence"][0]["text"]
    assert data["evidence"][0]["material_id"] == material_id
    assert data["prompt"] and data["answer"]
    stats = client.get(f"/api/courses/{course_id}/analytics").json()
    assert stats["real_questions"] == 1
    assert stats["demo_questions"] == 0
    assert stats["recent_questions"][0]["question"] == "경사하강법이 뭐야?"
    assert client.get(f"/api/courses/{other_id}/analytics").json()["total_questions"] == 0
    assert client.get(f"/api/courses/{course_id}/teaching-brief").json()["prompt"]
    plan = client.post(f"/api/courses/{course_id}/exam-plan", json={"total_questions": 11}).json()
    assert sum(row["count"] for row in plan["distribution"]) == 11
    assert plan["prompt"]

    assert client.delete(f"/api/courses/{course_id}/materials/{material_id}").status_code == 200
    after = client.post("/api/ask", json={"course_id": course_id, "question": "경사하강법이 뭐야?"}).json()
    assert not after["evidence"] and not after["sufficient"]
    assert client.get(f"/api/courses/{course_id}/analytics").json()["real_questions"] == 2


def pdf_bytes():
    writer = PdfWriter()
    for text in ["Gradient descent minimizes a loss function.", "Learning rate determines the update step size."]:
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 30 700 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_page_citations_and_invalid_uploads(client):
    course_id = make_course(client)
    uploaded = client.post(f"/api/courses/{course_id}/materials", files={"file": ("lecture.pdf", pdf_bytes(), "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    source = client.get(f"/api/courses/{course_id}/materials/{uploaded.json()['id']}").json()
    assert {chunk["page"] for chunk in source["chunks"]} == {1, 2}
    for filename, content in [("image.png", b"fake"), ("blank.md", b" "), ("broken.pdf", b"not pdf")]:
        response = client.post(f"/api/courses/{course_id}/materials", files={"file": (filename, content)})
        assert response.status_code == 400, response.text
    assert client.post(f"/api/courses/{course_id}/exam-plan", json={"total_questions": 0}).status_code == 422
    assert client.post("/api/ask", json={"course_id": 999, "question": "질문"}).status_code == 403


def test_cross_origin_changes_are_rejected(client):
    response = client.post("/api/courses", json={"title": "외부 요청"}, headers={"Origin": "https://unrelated.example"})
    assert response.status_code == 403


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}


def test_database_path_is_independent_of_client_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLASSROOM_DB", "data/example.sqlite")
    assert database_path() == str(ROOT / "data/example.sqlite")
    monkeypatch.setenv("CLASSROOM_DB", str(tmp_path / "custom.sqlite"))
    assert database_path() == str(tmp_path / "custom.sqlite")


def rpc(client, role, method, params=None, request_id=1):
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = client.post(f"/mcp/{role}", json=payload, headers=MCP_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def test_mcp_requires_bearer_token_even_with_web_login(client):
    response = client.post('/mcp/instructor', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert response.status_code == 401
    assert 'resource_metadata' in response.headers['www-authenticate']


def test_stdio_is_blocked_until_authenticated_identity_is_supported(tmp_path):
    import subprocess
    result = subprocess.run([sys.executable, '-m', 'classroom.mcp_server', '--role', 'instructor'], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'Google 로그인' in result.stderr
