import hashlib
import json
from pathlib import Path
import sqlite3
import stat

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from classroom.app import create_app, parse_document
from classroom.service import Service
from deploy import demo_setup


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "demo")
    path = tmp_path / "classroom.sqlite"
    app = create_app(str(path), seed=False)
    service, auth = app.state.service, app.state.auth
    teacher = auth.login_user({"sub": "teacher", "email": "teacher@example.test", "email_verified": True, "name": "Teacher"})
    course = service.create_course("[촬영] CS231n Backpropagation", owner_id=teacher["id"])
    tool = demo_setup.DemoSetup(path, tmp_path / "backups")
    return tool, service, auth, teacher["id"], course["id"]


@pytest.fixture
def assets(tmp_path, monkeypatch):
    directory = tmp_path / "assets"
    directory.mkdir()
    writer = PdfWriter()
    for number in range(1, 23):
        page = writer.add_blank_page(width=400, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = StreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 100 Td (Backpropagation page {number}: upstream local ReLU branch gradient) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(directory / "02_section_2_backprop.pdf")
    (directory / "01_backprop_cell_11.txt").write_text("grad_h = grad_z @ w2.T\ngrad_xw1 = grad_h * (h > 0)\n")
    (directory / "01_backprop_cell_18.txt").write_text("grad = (loss_plus - loss_minus) / (2 * h)\n")
    expected = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in demo_setup.EXPECTED_ASSETS}
    monkeypatch.setattr(demo_setup, "EXPECTED_ASSETS", expected)
    (directory / "manifest.json").write_text(json.dumps({"sources": [{"file": name, "kind": "pdf" if name.endswith(".pdf") else "code", "title": name, "sha256": digest} for name, digest in expected.items()]}))
    return directory


def test_reset_backups_wal_preserves_accounts_oauth_and_unrelated_course(data):
    tool, service, auth, teacher, course = data
    unrelated = service.create_course("실제 수업", owner_id=teacher)
    legacy = service.create_course("예전 기본 수업", owner_id=teacher)
    auth.new_session(teacher)
    service.add_text(course, "삭제할 자료", "촬영 데이터")
    with service._db() as db:
        db.execute("UPDATE courses SET is_demo=1 WHERE id=?", (legacy["id"],))
        db.execute("INSERT INTO metadata VALUES('keep-me','keep-this-value')")
        db.execute("INSERT INTO oauth_clients VALUES('client',?)", (b"opaque metadata",))
        db.execute("INSERT INTO oauth_grants VALUES('grant',?,'client','https://example.test/mcp/student','classroom:student',1,9999999999,0)", (teacher,))
        db.execute("INSERT INTO oauth_access VALUES('token-hash','grant',9999999999)")
    result = tool.reset_start()
    backup = Path(result["backup"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT COUNT(*) FROM courses").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == 1
    with service._db() as db:
        assert [r[0] for r in db.execute("SELECT id FROM courses")] == [unrelated["id"]]
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM oauth_grants").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM oauth_access").fetchone()[0] == 1
        assert db.execute("SELECT value FROM metadata WHERE key='keep-me'").fetchone()[0] == "keep-this-value"
    assert Service(str(tool.db_path), seed=True).list_courses()[0]["title"] == "실제 수업"
    assert len(Service(str(tool.db_path), seed=True).list_courses()) == 1


def test_mutation_aborts_if_backup_fails(data, monkeypatch):
    tool, service, _, _, course = data
    def fail(*args):
        raise OSError("backup unavailable")
    monkeypatch.setattr(demo_setup, "backup_database", fail)
    with pytest.raises(OSError, match="backup unavailable"):
        tool.reset_start()
    assert service.list_courses()[0]["id"] == course


def test_stage_reuses_manual_uploads_leaves_drafts_and_exact_page_sources(data, assets):
    tool, service, _, _, course = data
    text, pages = parse_document("02_section_2_backprop.pdf", (assets / "02_section_2_backprop.pdf").read_bytes())
    manual = service.add_text(course, "수업 중 직접 업로드한 PDF", text, "02_section_2_backprop.pdf", pages)
    # Direct pasted original code has no filename; exact chunks still permit reuse.
    code = service.add_text(course, "직접 입력한 원문 코드", (assets / "01_backprop_cell_11.txt").read_text())
    result = tool.stage(course, assets)
    assert result["approval_required"]
    assert result["materials"][0]["id"] == manual["id"]
    assert result["materials"][1]["id"] == code["id"]
    quizzes = service.quizzes.list_quizzes(course)
    assert len(quizzes) == 4 and all(q["status"] == "draft" and q["approved_by"] is None for q in quizzes)
    assert [q["sources"][0]["page"] for q in sorted(quizzes, key=lambda q: q["id"])] == [7, 6, 16, 8]
    repeated = tool.stage(course, assets)
    assert [q["id"] for q in repeated["quizzes"]] == [q["id"] for q in result["quizzes"]]
    assert len(service.list_materials(course)) == 3


def test_stage_rejects_changed_asset_and_foreign_course(data, assets):
    tool, service, _, teacher, course = data
    (assets / "01_backprop_cell_11.txt").write_text("print('unapproved material')")
    with pytest.raises(ValueError, match="원본과 다른"):
        tool.stage(course, assets)
    assert service.list_materials(course) == []
    foreign = service.create_course("실제 수업", owner_id=teacher)
    with pytest.raises(ValueError, match="수업만"):
        tool._course(service, create_app(str(tool.db_path), seed=False).state.auth, foreign["id"])


def test_activity_requires_publication_then_creates_labeled_idempotent_first_responses(data, assets):
    tool, service, _, teacher, course = data
    tool.stage(course, assets)
    with pytest.raises(ValueError, match="먼저 검토하고 승인"):
        tool.seed_activity(course)
    with service._db() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    for quiz in service.quizzes.list_quizzes(course):
        service.quizzes.publish(course, quiz["id"], teacher, quiz["revision"])
    seeded = tool.seed_activity(course)
    assert seeded["inserted_attempts"] == 12
    assert seeded["quiz_insights"]["first_attempt_count"] == 12
    metrics = {row["concept"]: row for row in seeded["quiz_insights"]["concepts"]}
    assert metrics["chain rule"]["correct_count"] == 1
    assert metrics["numeric backprop"]["correct_count"] == 2
    assert metrics["ReLU"]["correct_count"] == 3
    assert metrics["branch gradient"]["correct_count"] == 1
    assert all(row["first_attempt_count"] == 3 for row in metrics.values())
    assert tool.seed_activity(course)["inserted_attempts"] == 0
    with service._db() as db:
        assert db.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM questions WHERE is_demo=1 AND mode='demo-fixture'").fetchone()[0] == 12
        assert db.execute("SELECT COUNT(*) FROM users WHERE name LIKE '[데모]%' AND email LIKE '%@example.invalid'").fetchone()[0] == 3


def test_activity_rejects_quiz_changed_before_human_publication(data, assets):
    tool, service, _, teacher, course = data
    tool.stage(course, assets)
    quizzes = service.quizzes.list_quizzes(course)
    edited = dict(quizzes[0])
    payload = {key: edited[key] for key in demo_setup.quiz_payloads({6: 1, 7: 1, 8: 1, 16: 1})[0]}
    payload["correct_answer"] = "B"
    service.quizzes.update_draft(course, edited["id"], teacher, payload)
    for quiz in service.quizzes.list_quizzes(course):
        service.quizzes.publish(course, quiz["id"], teacher, quiz["revision"])
    with pytest.raises(ValueError, match="정확한 문항"):
        tool.seed_activity(course)
    assert service.quiz_insights(course)["first_attempt_count"] == 0


def test_cli_resolves_paths_before_leaving_unrelated_cwd(data, tmp_path, monkeypatch, capsys):
    tool, service, auth, _, course = data
    caller = tmp_path / "ssh-caller"
    caller.mkdir()
    (caller / ".env").write_text("CLASSROOM_BASE_URL=https://unrelated.example\n")
    (caller / "server.env").write_text("FILMING_CLI_PATH_TEST=loaded\n")
    (caller / "assets").mkdir()
    import classroom.app
    observed = {}
    def create_from_project(db_path, seed):
        observed.update(cwd=Path.cwd(), db=Path(db_path), seed=seed)
        return type("App", (), {"state": type("State", (), {"service": service, "auth": auth})()})()
    def backup_from_project(db_path, backup_dir):
        observed.update(backup_db=db_path, backup_dir=backup_dir)
        return None
    monkeypatch.setattr(classroom.app, "create_app", create_from_project)
    monkeypatch.setattr(demo_setup, "backup_database", backup_from_project)
    monkeypatch.chdir(caller)
    monkeypatch.setenv("FILMING_CLI_PATH_TEST", "before")
    demo_setup.main(["reset-start", "--db", "../classroom.sqlite", "--backup-dir", "backups", "--assets", "assets", "--env-file", "server.env"])
    assert observed == {"cwd": demo_setup.PROJECT_ROOT, "db": tool.db_path, "seed": False, "backup_db": tool.db_path, "backup_dir": caller / "backups"}
    import os
    assert os.environ["FILMING_CLI_PATH_TEST"] == "loaded"
    assert auth.base_url == "http://127.0.0.1:8000"
    assert course in json.loads(capsys.readouterr().out)["deleted_course_ids"]


def test_materials_only_accepts_course_title_and_reset_preserves_unmanaged_namesake(data, assets):
    tool, service, _, teacher, course = data
    with service._db() as db:
        db.execute("UPDATE courses SET title='CS231n 2026' WHERE id=?", (course,))
    unrelated = service.create_course("CS231n 2026", owner_id=teacher)
    result = tool.materials(course, assets)
    assert result["asset_count"] == 3 and result["quizzes_created"] == 0
    assert service.quizzes.list_quizzes(course) == []
    tool.reset_start()
    assert [row["id"] for row in service.list_courses()] == [unrelated["id"]]


def test_complete_manifest_is_pinned_and_preserves_distinct_source_provenance(data, assets, monkeypatch):
    tool, service, _, _, course = data
    manifest_path = assets / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["format_version"] = 2
    # Identical code in different original cells still has distinct provenance.
    extra = dict(manifest["sources"][1], file="another_cell.txt", title="Another original code cell")
    (assets / extra["file"]).write_bytes((assets / manifest["sources"][1]["file"]).read_bytes())
    manifest["sources"].append(extra)
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(demo_setup, "FULL_CORPUS_MANIFEST_SHA256", hashlib.sha256(manifest_path.read_bytes()).hexdigest())
    first = tool.materials(course, assets)
    assert len({row["id"] for row in first["materials"]}) == 4
    repeated = tool.materials(course, assets)
    assert [row["id"] for row in first["materials"]] == [row["id"] for row in repeated["materials"]]
    assert len(service.list_materials(course)) == 4
    manifest["course_title"] = "Changed manifest"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="승인한 전체"):
        tool.materials(course, assets)
