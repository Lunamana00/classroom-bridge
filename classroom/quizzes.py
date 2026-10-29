"""Approved practice quizzes, deterministic grading, and response-based signals.

This store does not infer unaided ability: first attempts are observations of
answers submitted through a client. Published quiz content is immutable.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
import secrets
import time
import unicodedata
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DELIVERY_TTL = 7 * 24 * 3600
REVIEW_TTL = 10 * 60
APPROVAL_NOTICE = (
    "교수가 문제·정답·해설·출처를 검토한 뒤 배포를 명시적으로 승인해야 합니다. "
    "MCP 클라이언트는 자연어로 배포 여부를 묻고 교수의 다음 실제 답변을 기다리세요. "
    "검토 토큰과 확인 문구는 클라이언트가 내부에서 관리하며 교수에게 기술적인 문자열 입력을 요구하지 마세요. "
    "서버는 도구 호출만으로 실제 사람의 승인을 독립적으로 검증하지 못합니다."
)
NOTICE = (
    "퀴즈 응답에 근거한 참고 신호입니다. 학생·퀴즈별 첫 제출만 추정에 사용하며 재시도와 교수 체험은 제외합니다. "
    "LLM을 통해 제출한 답의 직접 작성 여부나 도움 사용은 확인하지 못하므로 숙달도·성적을 확정하는 값이 아닙니다."
)
FIELDS = {"title", "concepts", "question", "kind", "options", "correct_answer", "accepted_answers", "explanation", "source_chunk_ids"}
ALIASES = {
    "chainrule": ("연쇄법칙", "upstreamgradient", "localgradient"),
    "경사하강법": ("경사하강", "gradientdescent", "기울기", "sgd", "미니배치"),
    "학습률": ("학습률", "learningrate", "발산", "수렴", "스케줄"),
    "과적합": ("과적합", "overfit", "일반화", "generalization", "조기종료", "규제"),
    "정규화표준화": ("정규화", "표준화", "normalization", "standardization", "zscore", "minmax"),
}


def _now():
    return datetime.now(KST).isoformat(timespec="seconds")


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _compact(value):
    return re.sub(r"[^a-z0-9가-힣]", "", _norm(value))


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{name}을(를) 1~{limit}자 범위로 입력해 주세요.")
    return value.strip()


def _identifier(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name}은(는) 양의 정수여야 합니다.")
    return value


class QuizStore:
    def __init__(self, service):
        self.service = service
        with self.service._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS concept_quizzes (
                    id INTEGER PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    title TEXT NOT NULL, concepts_json TEXT NOT NULL, question TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('multiple_choice','short_answer')),
                    options_json TEXT NOT NULL, correct_answer TEXT NOT NULL,
                    accepted_answers_json TEXT NOT NULL, explanation TEXT NOT NULL,
                    source_chunk_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','archived')),
                    created_by INTEGER NOT NULL, approved_by INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, approved_at TEXT,
                    revision INTEGER NOT NULL DEFAULT 1,
                    approval_channel TEXT CHECK(approval_channel IN ('web','mcp'))
                );
                CREATE INDEX IF NOT EXISTS quizzes_course_status ON concept_quizzes(course_id,status);
                CREATE TABLE IF NOT EXISTS quiz_concepts (
                    quiz_id INTEGER NOT NULL REFERENCES concept_quizzes(id) ON DELETE CASCADE,
                    concept_key TEXT NOT NULL, concept TEXT NOT NULL,
                    PRIMARY KEY(quiz_id,concept_key)
                );
                CREATE TABLE IF NOT EXISTS quiz_sources (
                    quiz_id INTEGER NOT NULL REFERENCES concept_quizzes(id) ON DELETE CASCADE,
                    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    PRIMARY KEY(quiz_id,chunk_id)
                );
                CREATE TABLE IF NOT EXISTS quiz_reviews (
                    review_token_hash TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    quiz_id INTEGER NOT NULL REFERENCES concept_quizzes(id) ON DELETE CASCADE,
                    teacher_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                    confirmation_phrase TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at INTEGER NOT NULL,
                    UNIQUE(teacher_id,quiz_id)
                );
                CREATE TABLE IF NOT EXISTS quiz_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    quiz_id INTEGER NOT NULL REFERENCES concept_quizzes(id) ON DELETE CASCADE,
                    student_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
                    quiz_revision INTEGER NOT NULL, is_preview INTEGER NOT NULL,
                    created_at TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deliveries_student_course ON quiz_deliveries(course_id,student_id);
                CREATE TABLE IF NOT EXISTS quiz_attempts (
                    id INTEGER PRIMARY KEY,
                    delivery_id TEXT UNIQUE NOT NULL REFERENCES quiz_deliveries(delivery_id) ON DELETE CASCADE,
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    quiz_id INTEGER NOT NULL REFERENCES concept_quizzes(id) ON DELETE CASCADE,
                    student_id INTEGER NOT NULL, answer TEXT NOT NULL, normalized_answer TEXT NOT NULL,
                    is_correct INTEGER NOT NULL, grading_method TEXT NOT NULL,
                    is_preview INTEGER NOT NULL, is_first_attempt INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL, result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_course_student_quiz ON quiz_attempts(course_id,student_id,quiz_id);
                CREATE INDEX IF NOT EXISTS attempts_first_course ON quiz_attempts(course_id,is_preview,is_first_attempt);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "approval_channel" not in {row["name"] for row in db.execute("PRAGMA table_info(concept_quizzes)")}:
                db.execute("ALTER TABLE concept_quizzes ADD COLUMN approval_channel TEXT CHECK(approval_channel IN ('web','mcp'))")

    @staticmethod
    def _course(db, course_id):
        _identifier(course_id, "수업 ID")
        if not db.execute("SELECT 1 FROM courses WHERE id=?", (course_id,)).fetchone():
            raise LookupError("수업을 찾을 수 없습니다.")

    @staticmethod
    def _member(db, course_id, user_id, teacher=False):
        _identifier(user_id, "사용자 ID")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='course_members'").fetchone():
            raise PermissionError("수업에 접근할 권한이 없습니다.")
        row = db.execute("SELECT role FROM course_members WHERE course_id=? AND user_id=?", (course_id, user_id)).fetchone()
        if not row or teacher and row[0] != "teacher":
            raise PermissionError("수업에 접근할 권한이 없습니다.")
        return row[0]

    @staticmethod
    def _sources(db, course_id, ids, quiz_id=None):
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        links = "JOIN quiz_sources qs ON qs.chunk_id=c.id AND qs.quiz_id=? " if quiz_id is not None else ""
        params = (quiz_id, course_id, *ids) if quiz_id is not None else (course_id, *ids)
        rows = db.execute(
            f"SELECT c.id AS chunk_id,m.id AS material_id,m.title,c.page,c.section,c.text FROM chunks c "
            f"JOIN materials m ON m.id=c.material_id {links}WHERE m.course_id=? AND c.id IN ({placeholders})",
            params,
        ).fetchall()
        found = {row["chunk_id"]: dict(row) for row in rows}
        return [found[i] for i in ids if i in found]

    def _payload(self, db, course_id, payload):
        if not isinstance(payload, dict) or set(payload) - FIELDS:
            raise ValueError("퀴즈 입력 항목을 확인해 주세요. 승인 상태는 입력할 수 없습니다.")
        result = {
            "title": _text(payload.get("title"), "퀴즈 제목", 200),
            "question": _text(payload.get("question"), "문제", 4000),
            "explanation": _text(payload.get("explanation"), "해설", 4000),
            "correct_answer": _text(payload.get("correct_answer"), "정답", 1000),
            "kind": payload.get("kind"),
        }
        concepts = payload.get("concepts")
        if not isinstance(concepts, list) or not 1 <= len(concepts) <= 8:
            raise ValueError("관련 개념은 1~8개를 지정해 주세요.")
        result["concepts"] = [_text(item, "관련 개념", 120) for item in concepts]
        if len({_norm(item) for item in result["concepts"]}) != len(concepts):
            raise ValueError("관련 개념은 중복 없이 지정해 주세요.")
        options = payload.get("options", [])
        aliases = payload.get("accepted_answers", [])
        if not isinstance(options, list) or not isinstance(aliases, list) or len(aliases) > 20:
            raise ValueError("선택지와 허용 답안 형식을 확인해 주세요.")
        if result["kind"] == "multiple_choice":
            if not 2 <= len(options) <= 6 or aliases:
                raise ValueError("객관식은 선택지 2~6개를 입력하고 허용 답안은 비워 주세요.")
            options = [_text(item, "선택지", 1000) for item in options]
            if len({_norm(item) for item in options}) != len(options):
                raise ValueError("선택지는 중복 없이 입력해 주세요.")
            result["correct_answer"] = result["correct_answer"].upper()
            if result["correct_answer"] not in [chr(65+i) for i in range(len(options))]:
                raise ValueError("객관식 정답은 선택지 ID(A~F)로 지정해 주세요.")
            result["accepted_answers"] = []
        elif result["kind"] == "short_answer":
            if options:
                raise ValueError("단답형에는 선택지를 지정할 수 없습니다.")
            aliases = [_text(item, "허용 답안", 1000) for item in aliases]
            result["accepted_answers"] = list(dict.fromkeys(aliases))
        else:
            raise ValueError("객관식(multiple_choice) 또는 단답형(short_answer)을 선택해 주세요.")
        result["options"] = options
        ids = payload.get("source_chunk_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("출처 구절 ID는 중복 없는 양의 정수 1~8개여야 합니다.")
        if len(self._sources(db, course_id, ids)) != len(ids):
            raise ValueError("출처는 현재 수업에 등록된 유효한 구절만 지정할 수 있습니다.")
        result["source_chunk_ids"] = ids
        return result

    @staticmethod
    def _quiz(db, course_id, quiz_id):
        _identifier(quiz_id, "퀴즈 ID")
        row = db.execute("SELECT * FROM concept_quizzes WHERE id=? AND course_id=?", (quiz_id, course_id)).fetchone()
        if not row:
            raise LookupError("이 수업의 퀴즈를 찾을 수 없습니다.")
        return row

    def _private(self, db, row):
        result = dict(row)
        for field in ("concepts", "options", "accepted_answers", "source_chunk_ids"):
            result[field] = json.loads(result.pop(field+"_json"))
        result["quiz_id"] = result["id"]
        result["sources"] = self._sources(db, row["course_id"], result["source_chunk_ids"], quiz_id=row["id"])
        result["available"] = len(result["sources"]) == len(result["source_chunk_ids"])
        return result

    def create_draft(self, course_id, teacher_id, payload):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._member(db, course_id, teacher_id, teacher=True)
            value = self._payload(db, course_id, payload)
            now = _now()
            quiz_id = db.execute(
                "INSERT INTO concept_quizzes(course_id,title,concepts_json,question,kind,options_json,correct_answer,accepted_answers_json,explanation,source_chunk_ids_json,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (course_id, value["title"], json.dumps(value["concepts"], ensure_ascii=False), value["question"], value["kind"], json.dumps(value["options"], ensure_ascii=False), value["correct_answer"], json.dumps(value["accepted_answers"], ensure_ascii=False), value["explanation"], json.dumps(value["source_chunk_ids"]), teacher_id, now, now),
            ).lastrowid
            self._concepts(db, quiz_id, value["concepts"])
            self._source_links(db, quiz_id, value["source_chunk_ids"])
            return self._private(db, self._quiz(db, course_id, quiz_id))

    @staticmethod
    def _concepts(db, quiz_id, concepts):
        db.execute("DELETE FROM quiz_concepts WHERE quiz_id=?", (quiz_id,))
        db.executemany("INSERT INTO quiz_concepts VALUES(?,?,?)", [(quiz_id, _norm(c), c) for c in concepts])

    @staticmethod
    def _source_links(db, quiz_id, source_ids):
        # A deleted chunk removes its link permanently, even if SQLite later
        # reuses the numeric chunk ID. Only an explicit draft edit can rebind it.
        db.execute("DELETE FROM quiz_sources WHERE quiz_id=?", (quiz_id,))
        db.executemany("INSERT INTO quiz_sources VALUES(?,?)", [(quiz_id, chunk_id) for chunk_id in source_ids])

    def update_draft(self, course_id, quiz_id, teacher_id, payload):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._member(db, course_id, teacher_id, teacher=True)
            old = self._quiz(db, course_id, quiz_id)
            if old["status"] != "draft":
                raise ValueError("승인된 퀴즈는 수정할 수 없습니다. 새 초안을 만들어 검토해 주세요.")
            value = self._payload(db, course_id, payload)
            db.execute("UPDATE concept_quizzes SET title=?,concepts_json=?,question=?,kind=?,options_json=?,correct_answer=?,accepted_answers_json=?,explanation=?,source_chunk_ids_json=?,updated_at=?,revision=revision+1 WHERE id=?", (value["title"], json.dumps(value["concepts"], ensure_ascii=False), value["question"], value["kind"], json.dumps(value["options"], ensure_ascii=False), value["correct_answer"], json.dumps(value["accepted_answers"], ensure_ascii=False), value["explanation"], json.dumps(value["source_chunk_ids"]), _now(), quiz_id))
            self._concepts(db, quiz_id, value["concepts"])
            self._source_links(db, quiz_id, value["source_chunk_ids"])
            return self._private(db, self._quiz(db, course_id, quiz_id))

    def list_quizzes(self, course_id):
        with self.service._db() as db:
            self._course(db, course_id)
            return [self._private(db, row) for row in db.execute("SELECT * FROM concept_quizzes WHERE course_id=? ORDER BY id DESC LIMIT 500", (course_id,)).fetchall()]

    def publish(self, course_id, quiz_id, teacher_id, expected_revision=None):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._member(db, course_id, teacher_id, teacher=True)
            row = self._quiz(db, course_id, quiz_id)
            if expected_revision is not None:
                _identifier(expected_revision, "검토한 퀴즈 버전")
                if row["revision"] != expected_revision:
                    raise ValueError("검토 후 퀴즈가 변경되었습니다. 최신 초안을 다시 검토하고 승인해 주세요.")
            return self._publish_locked(db, row, teacher_id, "web")

    def _publish_locked(self, db, row, teacher_id, channel):
        if row["status"] == "archived":
            raise ValueError("배포를 중단한 퀴즈는 다시 승인할 수 없습니다. 새 초안을 만들어 주세요.")
        if not self._private(db, row)["available"]:
            raise ValueError("출처가 삭제된 퀴즈는 승인할 수 없습니다.")
        if row["status"] == "draft":
            now = _now()
            db.execute("UPDATE concept_quizzes SET status='published',approved_by=?,approved_at=?,updated_at=?,approval_channel=? WHERE id=?", (teacher_id, now, now, channel, row["id"]))
            db.execute("DELETE FROM quiz_reviews WHERE quiz_id=?", (row["id"],))
        return self._private(db, self._quiz(db, row["course_id"], row["id"]))

    def review(self, course_id, quiz_id, teacher_id):
        """Return a private draft and a short-lived challenge for explicit approval.

        MCP callers must show the complete draft and wait for the professor's
        next explicit confirmation; a tool call cannot prove human intent.
        """
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._member(db, course_id, teacher_id, teacher=True)
            quiz = self._private(db, self._quiz(db, course_id, quiz_id))
            if quiz["status"] != "draft":
                raise ValueError("배포 전 초안만 검토 승인할 수 있습니다.")
            if not quiz["available"]:
                raise ValueError("출처가 삭제된 퀴즈는 검토 승인할 수 없습니다.")
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            expires_at = int(time.time()) + REVIEW_TTL
            phrase = f"퀴즈 #{quiz_id} v{quiz['revision']} 배포 승인"
            db.execute("DELETE FROM quiz_reviews WHERE expires_at<=? OR (teacher_id=? AND quiz_id=?)", (int(time.time()), teacher_id, quiz_id))
            db.execute("INSERT INTO quiz_reviews VALUES(?,?,?,?,?,?,?,?)", (token_hash, course_id, quiz_id, teacher_id, quiz["revision"], phrase, _now(), expires_at))
            return {**quiz, "review_token": token, "expires_at": expires_at, "confirmation_phrase": phrase, "approval_notice": APPROVAL_NOTICE}

    def approve_review(self, course_id, quiz_id, teacher_id, review_token, confirmation):
        """Consume the actor-bound review challenge and publish the reviewed version."""
        if not isinstance(review_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", review_token):
            raise ValueError("검토 토큰을 확인해 주세요. 퀴즈를 다시 검토해 주세요.")
        if not isinstance(confirmation, str):
            raise ValueError("검토 확인값이 필요합니다. 교수의 배포 승인 뒤 같은 검토의 확인값을 전달해 주세요.")
        token_hash = hashlib.sha256(review_token.encode("ascii")).hexdigest()
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._member(db, course_id, teacher_id, teacher=True)
            quiz = self._quiz(db, course_id, quiz_id)
            review = db.execute("SELECT * FROM quiz_reviews WHERE review_token_hash=?", (token_hash,)).fetchone()
            if not review or review["expires_at"] <= int(time.time()):
                raise ValueError("검토 요청이 만료되었거나 이미 사용되었습니다. 퀴즈를 다시 검토해 주세요.")
            if review["course_id"] != course_id or review["quiz_id"] != quiz_id or review["teacher_id"] != teacher_id:
                raise PermissionError("본인이 이 수업에서 검토한 퀴즈에만 승인할 수 있습니다.")
            if quiz["status"] != "draft" or review["revision"] != quiz["revision"]:
                raise ValueError("검토 후 퀴즈가 변경되었습니다. 최신 초안을 다시 검토하고 승인해 주세요.")
            if confirmation != review["confirmation_phrase"]:
                raise ValueError("검토 확인값이 일치하지 않습니다. 같은 검토에서 받은 확인값을 전달해 주세요. 아직 배포하지 않았습니다.")
            return self._publish_locked(db, quiz, teacher_id, "mcp")

    def archive(self, course_id, quiz_id, teacher_id):
        with self.service._db() as db:
            self._course(db, course_id)
            self._member(db, course_id, teacher_id, teacher=True)
            self._quiz(db, course_id, quiz_id)
            db.execute("UPDATE concept_quizzes SET status='archived',updated_at=? WHERE id=?", (_now(), quiz_id))
            return self._private(db, self._quiz(db, course_id, quiz_id))

    @staticmethod
    def _matches(concept, text):
        concept_key, body = _compact(concept), _compact(text)
        normalized_text = _norm(text)

        def contains(term):
            key = _compact(term)
            if not key:
                return False
            if key.isascii():
                pattern = r"(?<![a-z0-9])" + r"[\W_]*".join(re.escape(char) for char in key) + r"(?![a-z0-9])"
                return bool(re.search(pattern, normalized_text))
            return key in body

        if contains(concept):
            return True
        for canonical, aliases in ALIASES.items():
            if concept_key == canonical or concept_key in aliases:
                if any(contains(alias) for alias in (canonical, *aliases)):
                    return True
        terms = {w for w in re.findall(r"[a-z0-9]+|[가-힣]+", _norm(concept)) if len(w) >= 2}
        return bool(terms and all(contains(word) for word in terms))

    def related_quizzes(self, course_id, question, evidence, student_id, question_id):
        question = _text(question, "질문", 2000)
        _identifier(question_id, "질문 ID")
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            role = self._member(db, course_id, student_id)
            if not db.execute("SELECT 1 FROM questions WHERE id=? AND course_id=?", (question_id, course_id)).fetchone():
                raise ValueError("이 수업의 질문에만 퀴즈를 연결할 수 있습니다.")
            evidence_ids = {row.get("chunk_id") for row in evidence if isinstance(row, dict)}
            ranked = []
            for row in db.execute("SELECT * FROM concept_quizzes WHERE course_id=? AND status='published' ORDER BY id", (course_id,)).fetchall():
                value = self._private(db, row)
                if not value["available"]:
                    continue
                direct = sum(self._matches(c, question) for c in value["concepts"])
                overlap = bool(set(value["source_chunk_ids"]) & evidence_ids)
                evidence_concept = any(self._matches(c, str(e.get("title", ""))+" "+str(e.get("section", ""))) for c in value["concepts"] for e in evidence if isinstance(e, dict) and e.get("chunk_id") in value["source_chunk_ids"])
                if not direct and not (overlap and evidence_concept):
                    continue
                score = min(1.0, 0.7 * direct / len(value["concepts"]) + 0.3 * overlap) if direct else 0.3
                ranked.append((score, value))
            ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
            result = []
            now = _now()
            expiry = int(time.time()) + DELIVERY_TTL
            for score, quiz in ranked[:3]:
                delivery_id = secrets.token_urlsafe(32)
                db.execute("INSERT INTO quiz_deliveries VALUES(?,?,?,?,?,?,?,?,?)", (delivery_id, course_id, quiz["id"], student_id, question_id, quiz["revision"], int(role == "teacher"), now, expiry))
                result.append({"quiz_id": quiz["id"], "quiz_version": quiz["revision"], "delivery_id": delivery_id, "title": quiz["title"], "concepts": quiz["concepts"], "question": quiz["question"], "kind": quiz["kind"], "options": [{"id":chr(65+i), "text":text} for i, text in enumerate(quiz["options"])], "sources": [{k:v for k,v in source.items() if k != "text"} for source in quiz["sources"]], "relevance": {"method":"concept_and_source_overlap", "score":round(score, 4), "is_heuristic":True}, "is_preview": role == "teacher", "expires_at":expiry})
            return result

    @staticmethod
    def _grade(quiz, answer):
        if quiz["kind"] == "multiple_choice":
            normalized = _norm(answer)
            options = quiz["options"]
            candidate = normalized.upper()
            ids = [chr(65+i) for i in range(len(options))]
            if candidate not in ids:
                if normalized.isascii() and normalized.isdigit() and 1 <= int(normalized) <= len(options):
                    candidate = ids[int(normalized)-1]
                else:
                    matches = [ids[i] for i, text in enumerate(options) if _norm(text) == normalized]
                    if len(matches) != 1:
                        raise ValueError("선택지 ID(A~F), 번호 또는 선택지 원문으로 답해 주세요. 이 입력은 채점·기록하지 않았습니다.")
                    candidate = matches[0]
            return candidate == quiz["correct_answer"], candidate, "choice_exact"
        normalized = _norm(answer)
        accepted = {_norm(quiz["correct_answer"]), *(_norm(v) for v in quiz["accepted_answers"])}
        return normalized in accepted, normalized, "normalized_exact_match"

    def submit_answer(self, course_id, delivery_id, student_id, answer):
        if not isinstance(delivery_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", delivery_id):
            raise ValueError("퀴즈 전달 ID를 확인해 주세요.")
        _text(answer, "답안", 4000)
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            role = self._member(db, course_id, student_id)
            delivery = db.execute("SELECT * FROM quiz_deliveries WHERE delivery_id=? AND course_id=?", (delivery_id, course_id)).fetchone()
            if not delivery:
                raise LookupError("이 수업에서 전달한 퀴즈를 찾을 수 없습니다.")
            if delivery["student_id"] != student_id:
                raise PermissionError("본인에게 전달된 퀴즈에만 답할 수 있습니다.")
            quiz = self._private(db, self._quiz(db, course_id, delivery["quiz_id"]))
            if quiz["status"] != "published" or not quiz["available"] or delivery["quiz_revision"] != quiz["revision"] or delivery["expires_at"] <= int(time.time()):
                raise LookupError("이 퀴즈는 만료되었거나 배포가 중단되었습니다. 새 질문으로 다시 확인해 주세요.")
            previous = db.execute("SELECT answer,result_json FROM quiz_attempts WHERE delivery_id=?", (delivery_id,)).fetchone()
            if previous:
                if previous["answer"] != answer:
                    raise ValueError("이미 답한 퀴즈입니다. 재시도하려면 새로 전달받은 퀴즈를 사용해 주세요.")
                return {**json.loads(previous["result_json"]), "replayed":True}
            correct, normalized, method = self._grade(quiz, answer)
            preview = bool(delivery["is_preview"] or role == "teacher")
            first = not preview and not db.execute("SELECT 1 FROM quiz_attempts WHERE course_id=? AND quiz_id=? AND student_id=? AND is_preview=0 LIMIT 1", (course_id, quiz["id"], student_id)).fetchone()
            now = _now()
            result = {"quiz_id":quiz["id"], "delivery_id":delivery_id, "concepts":quiz["concepts"], "answer":answer, "is_correct":correct, "correct_answer":quiz["correct_answer"], "explanation":quiz["explanation"], "grading_method":method, "is_preview":preview, "is_first_attempt":bool(first), "recorded_at":now, "replayed":False, "notice":NOTICE}
            attempt_id = db.execute("INSERT INTO quiz_attempts(delivery_id,course_id,quiz_id,student_id,answer,normalized_answer,is_correct,grading_method,is_preview,is_first_attempt,recorded_at,result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (delivery_id, course_id, quiz["id"], student_id, answer, normalized, int(correct), method, int(preview), int(first), now, "{}")).lastrowid
            result["attempt_id"] = attempt_id
            db.execute("UPDATE quiz_attempts SET result_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), attempt_id))
            return result

    @staticmethod
    def _metric(row):
        count, correct = row["first_attempt_count"], row["correct_count"]
        accuracy = correct / count if count else None
        return {"concept":row["concept"], "first_attempt_count":count, "correct_count":correct, "incorrect_count":count-correct, "accuracy":round(accuracy, 4) if accuracy is not None else None, "estimated_correctness":round((correct+1)/(count+2), 4) if count else None, "status":"insufficient_data" if count < 3 else "needs_review" if accuracy < 0.6 else "developing" if accuracy < 0.8 else "consistent_correct", "quiz_count":row["quiz_count"], "student_count":row["student_count"], "method":"beta_1_1_first_attempt_response_ratio"}

    def _summary(self, db, course_id, student_id=None):
        where, params = "a.course_id=?", [course_id]
        if student_id is not None:
            where += " AND a.student_id=?"
            params.append(student_id)
        counts = db.execute(f"SELECT COUNT(*) AS total, COALESCE(SUM(is_preview=0),0) AS attempts, COALESCE(SUM(is_preview=1),0) AS previews, COALESCE(SUM(is_preview=0 AND is_first_attempt=1),0) AS firsts FROM quiz_attempts a WHERE {where}", params).fetchone()
        rows = db.execute(f"SELECT MIN(c.concept) AS concept,COUNT(*) AS first_attempt_count,SUM(a.is_correct) AS correct_count,COUNT(DISTINCT a.quiz_id) AS quiz_count,COUNT(DISTINCT a.student_id) AS student_count FROM quiz_attempts a JOIN quiz_concepts c ON c.quiz_id=a.quiz_id WHERE {where} AND a.is_preview=0 AND a.is_first_attempt=1 GROUP BY c.concept_key ORDER BY first_attempt_count DESC,c.concept_key", params).fetchall()
        return {"attempt_count":counts["attempts"], "first_attempt_count":counts["firsts"], "retake_count":counts["attempts"]-counts["firsts"], "preview_attempt_count":counts["previews"], "concepts":[self._metric(row) for row in rows], "notice":NOTICE}

    def student_progress(self, course_id, student_id):
        with self.service._db() as db:
            self._course(db, course_id)
            self._member(db, course_id, student_id)
            result = self._summary(db, course_id, student_id)
            rows = db.execute("SELECT a.id AS attempt_id,a.quiz_id,q.title,a.answer,a.is_correct,a.grading_method,a.is_preview,a.is_first_attempt,a.recorded_at FROM quiz_attempts a JOIN concept_quizzes q ON q.id=a.quiz_id WHERE a.course_id=? AND a.student_id=? ORDER BY a.id DESC LIMIT 20", (course_id, student_id)).fetchall()
            result["recent_attempts"] = [{**dict(row), "is_correct":bool(row["is_correct"]), "is_preview":bool(row["is_preview"]), "is_first_attempt":bool(row["is_first_attempt"])} for row in rows]
            return result

    def insights(self, course_id):
        with self.service._db() as db:
            self._course(db, course_id)
            result = self._summary(db, course_id)
            total = db.execute("SELECT COUNT(DISTINCT student_id) FROM quiz_attempts WHERE course_id=? AND is_preview=0", (course_id,)).fetchone()[0]
            ids = [row[0] for row in db.execute("SELECT DISTINCT student_id FROM quiz_attempts WHERE course_id=? AND is_preview=0 ORDER BY student_id LIMIT 500", (course_id,))]
            result["students"] = [{"student_id":user_id, **self._summary(db, course_id, user_id)} for user_id in ids]
            result["student_count"] = total
            result["students_truncated"] = total > len(ids)
            result["total_attempts"] = result["attempt_count"]
            return result
