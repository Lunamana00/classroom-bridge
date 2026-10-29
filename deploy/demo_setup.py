"""Private, repeatable CS231n filming setup. This is a CLI, never a login route.

Every mutating command backs up SQLite first. Quiz publication remains a human
action in the web workspace or the explicit MCP review/approval flow.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

MANAGED_PREFIXES = ("[촬영] CS231n", "[리허설] CS231n")
COURSE_TITLE = "CS231n 2026"
# Pinned after exporting the user-supplied complete course corpus.
FULL_CORPUS_MANIFEST_SHA256 = "30fc77f61f5c7b4e0f6dac8de91a7a63ad208d10b86abcdebc40a93553e82ebd"
META_PREFIX = "cs231n_filming_v1:"
EXPECTED_ASSETS = {
    "02_section_2_backprop.pdf": "e9eb8fa56c8e89e680de8a9fe2a6b9c9330bc517f66e8097fd23b22379db011d",
    "01_backprop_cell_11.txt": "18fb7f648874d0d5588b96e41efe938f5f4ec81bf94420e44185865c18c0d634",
    "01_backprop_cell_18.txt": "c30bfa3ac634120604f5cc40e12d009883f4a236b2e16a97e1e276c7baa482ca",
}


def backup_database(db_path: Path, backup_dir: Path) -> Path | None:
    """Use SQLite's snapshot API, including committed WAL data, before writes."""
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%dT%H%M%S%f%z")
    target = backup_dir / f"{db_path.stem}-{stamp}-{secrets.token_hex(3)}.sqlite"
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as source:
            with sqlite3.connect(target) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("DB 백업 무결성 검사에 실패했습니다.")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def load_assets(directory: Path) -> list[dict]:
    from classroom.app import parse_document
    directory = directory.resolve()
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = manifest.get("sources", [])
    complete = manifest.get("format_version") == 2
    if complete:
        if hashlib.sha256(manifest_bytes).hexdigest() != FULL_CORPUS_MANIFEST_SHA256:
            raise ValueError("승인한 전체 CS231n 자료 목록과 다릅니다. 원본에서 다시 준비해 주세요.")
        if not entries or len({row.get("file") for row in entries}) != len(entries):
            raise ValueError("전체 CS231n 자료 목록에 누락 또는 중복이 있습니다.")
    elif len(entries) != len(EXPECTED_ASSETS) or {row.get("file") for row in entries} != set(EXPECTED_ASSETS):
        raise ValueError("지정한 CS231n PDF와 코드 원문만 사용할 수 있습니다.")
    result = []
    for row in entries:
        filename = row["file"]
        path = directory / filename
        if Path(filename).is_absolute() or ".." in Path(filename).parts or not path.resolve().is_relative_to(directory) or path.is_symlink():
            raise ValueError("자료 파일은 에셋 폴더 내부에 있어야 합니다.")
        if row.get("kind") not in {"pdf", "code"} or path.suffix.lower() not in {".pdf", ".txt"}:
            raise ValueError("CS231n PDF와 코드 원문만 등록할 수 있습니다.")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if (not complete and digest != EXPECTED_ASSETS[filename]) or row.get("sha256") != digest:
            raise ValueError(f"원본과 다른 촬영 자료입니다: {filename}")
        text, pages = parse_document(filename, content)
        if filename == "02_section_2_backprop.pdf" and not {6, 7, 8, 16}.issubset({p for p, _ in pages or []}):
            raise ValueError("퀴즈 출처 PDF 페이지를 확인할 수 없습니다.")
        result.append({**row, "text": text, "pages": pages})
    return result


def quiz_payloads(page_ids: dict[int, int]) -> list[dict]:
    return [
        {"title": "[데모] 연쇄법칙: upstream × local", "concepts": ["chain rule", "연쇄법칙"],
         "question": "Backpropagation에서 각 노드가 입력 쪽으로 전달할 gradient는 어떻게 계산하나요?",
         "kind": "multiple_choice", "options": ["upstream gradient × local gradient", "두 gradient를 더한다", "local gradient만 사용한다", "순전파 출력만 사용한다"],
         "correct_answer": "A", "accepted_answers": [],
         "explanation": "복습 PDF 7쪽은 upstream gradient와 local gradient를 곱하여 입력 방향의 gradient를 전달하는 연쇄법칙을 보여 줍니다.",
         "source_chunk_ids": [page_ids[7]]},
        {"title": "[데모] 수치 역전파: ∂f/∂x", "concepts": ["numeric backprop", "수치 역전파"],
         "question": "f=(x+y)z, x=-2, y=5, z=-4일 때 ∂f/∂x의 값은? 숫자만 입력하세요.",
         "kind": "short_answer", "options": [], "correct_answer": "-4", "accepted_answers": ["−4"],
         "explanation": "복습 PDF 5~6쪽 계산 그래프에서 ∂f/∂x=z=-4입니다.", "source_chunk_ids": [page_ids[6]]},
        {"title": "[데모] ReLU의 음수 입력 gradient", "concepts": ["ReLU"],
         "question": "복습 PDF의 ReLU backward에서 입력이 -2일 때 local gradient는?",
         "kind": "multiple_choice", "options": ["0", "1", "-2", "정해지지 않는다"], "correct_answer": "A", "accepted_answers": [],
         "explanation": "복습 PDF 16쪽의 ReLU는 음수 입력의 gradient를 0으로 막고 양수 입력에서 전달합니다.",
         "source_chunk_ids": [page_ids[16]]},
        {"title": "[데모] 분기 경로의 gradient 합산", "concepts": ["branch gradient", "분기 gradient"],
         "question": "계산 그래프의 분기 경로에서 돌아오는 gradient들은 어떻게 결합하나요? 한 단어로 답하세요.",
         "kind": "short_answer", "options": [], "correct_answer": "합산", "accepted_answers": ["합산한다", "더한다", "sum"],
         "explanation": "복습 PDF 8쪽의 분기에서는 각 경로에서 돌아온 gradient를 더합니다.", "source_chunk_ids": [page_ids[8]]},
    ]


class DemoSetup:
    def __init__(self, db_path: str | Path, backup_dir: str | Path):
        self.db_path = Path(db_path).resolve()
        self.backup_dir = Path(backup_dir).resolve()

    def _begin(self):
        backup = backup_database(self.db_path, self.backup_dir)
        # Schema migrations happen only after the original DB snapshot exists.
        from classroom.app import create_app
        app = create_app(str(self.db_path), seed=False)
        return app.state.service, app.state.auth, str(backup) if backup else None

    @staticmethod
    def _course(service, auth, course_id):
        with service._db() as db:
            course = service._course(db, course_id)
            if course["title"] != COURSE_TITLE and not course["title"].startswith(MANAGED_PREFIXES):
                raise ValueError("CS231n 2026 또는 기존 촬영용 접두사의 수업만 변경할 수 있습니다.")
            teachers = db.execute("SELECT u.id,u.email FROM users u JOIN course_members m ON m.user_id=u.id WHERE m.course_id=? AND m.role='teacher' ORDER BY u.id", (course_id,)).fetchall()
        owner = next((row["id"] for row in teachers if row["email"] in auth.teacher_emails), None)
        if owner is None:
            raise PermissionError("현재 교수 허용 목록에 있는 담당 교수가 필요합니다.")
        return owner

    @staticmethod
    def _key(course_id):
        return f"{META_PREFIX}{course_id}"

    def status(self):
        if not self.db_path.exists():
            return {"db": str(self.db_path), "exists": False}
        with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in ("users", "auth_sessions", "oauth_grants", "materials", "questions", "concept_quizzes", "quiz_attempts") if name in tables}
            courses = [dict(row) for row in db.execute("SELECT id,title,is_demo FROM courses ORDER BY id")] if "courses" in tables else []
            return {"db": str(self.db_path), "exists": True, "integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "courses": courses, "counts": counts}

    def reset_start(self):
        service, _, backup = self._begin()
        with service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            courses = db.execute("SELECT id,title,is_demo FROM courses").fetchall()
            managed = {int(row[0][len(META_PREFIX):]) for row in db.execute("SELECT key FROM metadata WHERE key LIKE ?", (META_PREFIX + "%",)) if row[0][len(META_PREFIX):].isdigit()}
            selected = [row["id"] for row in courses if row["is_demo"] or row["title"].startswith(MANAGED_PREFIXES) or row["id"] in managed]
            for course_id in selected:
                db.execute("DELETE FROM courses WHERE id=?", (course_id,))
                db.execute("DELETE FROM metadata WHERE key=?", (self._key(course_id),))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('demo_seed_v1','1')")
        return {"backup": backup, "deleted_course_ids": selected, "preserved": "users, sessions, OAuth connections, unrelated courses", "state": self.status()}

    def materials(self, course_id: int, assets: str | Path):
        return self.stage(course_id, assets, with_quizzes=False)

    def stage(self, course_id: int, assets: str | Path, *, with_quizzes=True):
        sources = load_assets(Path(assets))
        service, auth, backup = self._begin()
        owner = self._course(service, auth, course_id)
        from classroom.service import _chunks
        with service._db() as db:
            saved_row = db.execute("SELECT value FROM metadata WHERE key=?", (self._key(course_id),)).fetchone()
        saved = json.loads(saved_row[0]) if saved_row else {}
        # Compare original chunks once per existing material, rather than
        # reopening every source for every one of the full-course assets.
        existing_materials = service.list_materials(course_id)
        by_chunks = {}
        for material in existing_materials:
            detail = service.get_material(course_id, material["id"])
            fingerprint = tuple((row["text"], row["page"], row["section"]) for row in detail["chunks"])
            by_chunks.setdefault(fingerprint, []).append(material)
        materials = []
        used_ids = set()
        for source in sources:
            expected = tuple(_chunks(source["text"].strip(), source["pages"]))
            material = next((item for item in by_chunks.get(expected, []) if item["id"] not in used_ids), None)
            if material is None:
                for existing_material in existing_materials:
                    if existing_material["filename"] == source["file"] or existing_material["title"] == source["title"]:
                        raise ValueError(f"동일 이름의 자료가 원본과 다릅니다. 직접 확인해 주세요: {source['file']}")
                material = service.add_text(course_id, source["title"], source["text"], source["file"], source["pages"])
                existing_materials.append(material)
                by_chunks.setdefault(expected, []).append(material)
            used_ids.add(material["id"])
            materials.append({"id": material["id"], "filename": source["file"], "sha256": source["sha256"]})
        saved.update({"materials": materials, "course_title": COURSE_TITLE})
        if not with_quizzes:
            with service._db() as db:
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (self._key(course_id), json.dumps(saved, ensure_ascii=False)))
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('demo_seed_v1','1')")
            return {"backup": backup, "course_id": course_id, "materials": materials, "asset_count": len(materials), "quizzes_created": 0}
        pdf_id = next(row["id"] for row in materials if row["filename"] == "02_section_2_backprop.pdf")
        page_ids = {}
        for chunk in service.get_material(course_id, pdf_id)["chunks"]:
            page_ids.setdefault(chunk["page"], chunk["id"])
        payloads = quiz_payloads(page_ids)
        existing = service.quizzes.list_quizzes(course_id)
        quizzes = []
        for payload in payloads:
            matching = [quiz for quiz in existing if quiz["title"] == payload["title"]]
            if matching:
                quiz = matching[0]
                if len(matching) != 1 or any(quiz[field] != value for field, value in payload.items()):
                    raise ValueError("동일 제목의 퀴즈가 촬영용 원문과 다릅니다. 직접 검토해 주세요.")
                if not quiz["available"] or quiz["status"] == "archived":
                    raise ValueError("촬영용 퀴즈의 출처 또는 배포 상태를 직접 확인해 주세요.")
            else:
                quiz = service.quizzes.create_draft(course_id, owner, payload)
            quizzes.append({"id": quiz["id"], "title": quiz["title"], "status": quiz["status"], "revision": quiz["revision"], "source_chunk_ids": quiz["source_chunk_ids"]})
        saved.update({"materials": materials, "quizzes": [{"id": quiz["id"], "payload": payload} for quiz, payload in zip(quizzes, payloads)]})
        with service._db() as db:
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)", (self._key(course_id), json.dumps(saved, ensure_ascii=False)))
            db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('demo_seed_v1','1')")
        return {"backup": backup, "course_id": course_id, "materials": materials, "quizzes": quizzes, "approval_required": True, "notice": "새 퀴즈는 초안입니다. 교수자가 문항·정답·출처를 검토하고 웹/MCP에서 직접 승인해야 합니다."}

    def seed_activity(self, course_id: int):
        service, auth, backup = self._begin()
        self._course(service, auth, course_id)
        with service._db() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (self._key(course_id),)).fetchone()
        if not row:
            raise ValueError("먼저 stage로 촬영 자료와 초안을 준비해 주세요.")
        setup = json.loads(row[0])
        quizzes = {quiz["id"]: quiz for quiz in service.quizzes.list_quizzes(course_id)}
        if len(setup.get("quizzes", [])) != 4:
            raise ValueError("촬영용 퀴즈 4개가 필요합니다.")
        prepared = []
        for entry in setup["quizzes"]:
            quiz = quizzes.get(entry["id"])
            if not quiz or quiz["status"] != "published" or not quiz["available"] or any(quiz[field] != value for field, value in entry["payload"].items()):
                raise ValueError("촬영용 퀴즈 4개의 정확한 문항·정답·출처를 교수가 먼저 검토하고 승인해야 합니다.")
            prepared.append(quiz)
        # Per-concept first response accuracy: 1/3, 2/3, 3/3, 1/3.
        answers = [("A", "B", "B"), ("-4", "-4", "4"), ("A", "A", "A"), ("합산", "곱셈", "평균")]
        inserted = 0
        now = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds")
        for index, label in enumerate("ABC"):
            sub = f"classroom-demo-filming-v1:{course_id}:{label}"
            with service._db() as db:
                db.execute("INSERT INTO users(google_sub,email,name) VALUES(?,?,?) ON CONFLICT(google_sub) DO NOTHING", (sub, f"filming-{course_id}-{label.lower()}@example.invalid", f"[데모] 학생 {label}"))
                user = db.execute("SELECT id,email,name FROM users WHERE google_sub=?", (sub,)).fetchone()
                if not user["email"].endswith("@example.invalid") or not user["name"].startswith("[데모]"):
                    raise ValueError("예약된 데모 사용자 ID가 다른 계정과 충돌합니다.")
                student_id = user["id"]
                db.execute("INSERT OR IGNORE INTO course_members VALUES(?,?,'student')", (course_id, student_id))
            for quiz, variants in zip(prepared, answers):
                with service._db() as db:
                    if db.execute("SELECT 1 FROM quiz_attempts WHERE course_id=? AND quiz_id=? AND student_id=? AND is_preview=0", (course_id, quiz["id"], student_id)).fetchone():
                        continue
                    text = f"[데모 합성 질문 · 학생 {label}] {quiz['concepts'][0]} {quiz['question']}"
                    evidence = [dict(source, score=1.0) for source in quiz["sources"]]
                    question_id = db.execute("INSERT INTO questions(course_id,question,topic,created_at,sufficient,is_demo,mode,evidence_json) VALUES(?,?,?,?,1,1,'demo-fixture',?)", (course_id, text, quiz["concepts"][0], now, json.dumps(evidence, ensure_ascii=False))).lastrowid
                deliveries = service.quizzes.related_quizzes(course_id, text, evidence, student_id, question_id)
                delivery = next((item for item in deliveries if item["quiz_id"] == quiz["id"]), None)
                if delivery is None:
                    raise RuntimeError("해당 출처의 승인 퀴즈를 전달하지 못했습니다.")
                service.quizzes.submit_answer(course_id, delivery["delivery_id"], student_id, variants[index])
                inserted += 1
        return {"backup": backup, "course_id": course_id, "inserted_attempts": inserted, "synthetic_students": 3, "synthetic_notice": "[데모] 학생 A/B/C와 합성 질문의 연출용 응답입니다. 실제 학생 학습 결과가 아닙니다. 로그인 세션을 생성하지 않습니다.", "quiz_insights": service.quiz_insights(course_id)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "reset-start", "materials", "stage", "seed-activity"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--course-id", type=int)
    parser.add_argument("--env-file", type=Path, help="Optional private server dotenv file; values are never printed")
    args = parser.parse_args(argv)
    # Preserve caller-relative file arguments before leaving an inaccessible
    # sudo/SSH working directory. MCP's settings may inspect cwd/.env as well.
    for name in ("db", "backup_dir", "assets", "env_file"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.expanduser().resolve())
    os.chdir(PROJECT_ROOT)
    if args.env_file:
        from dotenv import dotenv_values
        os.environ.update({key: value for key, value in dotenv_values(args.env_file).items() if value is not None})
    if args.command in {"materials", "stage", "seed-activity"} and (not args.course_id or args.course_id < 1):
        parser.error("materials/stage/seed-activity에는 --course-id 양의 정수가 필요합니다.")
    if args.command in {"materials", "stage"} and not args.assets:
        parser.error("materials/stage에는 --assets 폴더가 필요합니다.")
    tool = DemoSetup(args.db, args.backup_dir or args.db.parent / "backups")
    try:
        if args.command == "status":
            result = tool.status()
        elif args.command == "reset-start":
            result = tool.reset_start()
        elif args.command == "materials":
            result = tool.materials(args.course_id, args.assets)
        elif args.command == "stage":
            result = tool.stage(args.course_id, args.assets)
        else:
            result = tool.seed_activity(args.course_id)
    except (ValueError, PermissionError, LookupError) as exc:
        parser.exit(1, str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
