"""Durable, source-grounded concept drafts and explicit teacher approval.

Extraction proposes changes. It never approves a concept or silently changes an
approved concept. The original material chunks remain the source of truth.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import secrets
import sqlite3
import time
import unicodedata
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
logger = logging.getLogger(__name__)
LEASE_SECONDS = 30 * 60
LEASE_HEARTBEAT_SECONDS = 60
FIELDS = {"name", "description", "aliases", "source_chunk_ids", "merge_target_id"}
FAILURE_MESSAGE = "개념 추출에 실패했습니다. API 설정과 자료를 확인한 뒤 다시 시도해 주세요."
CONFIG_MESSAGE = "개념 추출 API가 설정되지 않았습니다. 서버의 API 키와 개념 추출 설정을 확인해 주세요."


def _now():
    return datetime.now(KST).isoformat(timespec="seconds")


def _identifier(value, name="ID"):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name}은(는) 양의 정수여야 합니다.")
    return value


def _text(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label}을(를) 1~{limit}자 범위로 입력해 주세요.")
    return value.strip()


def _key(value):
    # Symbols distinguish concepts: C/C++/C#, x^2/x2, x-y/xy and x_y/xy
    # must not become the same identity. Only ignore spacing and Unicode/case
    # variants; punctuation variants need explicit aliases or teacher review.
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if not c.isspace())


def _names(name, aliases):
    result, keys = [], set()
    for value in [name, *aliases]:
        key = _key(value)
        if key and key not in keys:
            result.append(value)
            keys.add(key)
    return result


class ConceptStore:
    def __init__(self, service):
        self.service = service
        with service._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS course_concepts (
                    id INTEGER PRIMARY KEY,
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    name TEXT NOT NULL, description TEXT NOT NULL, aliases_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','approved','rejected','merged')),
                    revision INTEGER NOT NULL DEFAULT 1,
                    merge_target_id INTEGER REFERENCES course_concepts(id) ON DELETE SET NULL,
                    merge_target_revision INTEGER,
                    approved_by INTEGER, approved_at TEXT,
                    reviewed_by INTEGER, reviewed_at TEXT,
                    extraction_model TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS concepts_course_status ON course_concepts(course_id,status);
                CREATE TABLE IF NOT EXISTS concept_sources (
                    concept_id INTEGER NOT NULL REFERENCES course_concepts(id) ON DELETE CASCADE,
                    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    PRIMARY KEY(concept_id,chunk_id)
                );
                CREATE INDEX IF NOT EXISTS concept_sources_chunk ON concept_sources(chunk_id);
                CREATE TABLE IF NOT EXISTS material_concept_jobs (
                    material_id INTEGER PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','processing','ready','failed','not_configured')),
                    error TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS concept_jobs_status ON material_concept_jobs(status,lease_until);
                CREATE TRIGGER IF NOT EXISTS concept_source_removed
                AFTER DELETE ON concept_sources BEGIN
                    UPDATE course_concepts SET revision=revision+1,
                        updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE id=OLD.concept_id;
                END;
            """)

    @staticmethod
    def _course(db, course_id):
        _identifier(course_id, "수업 ID")
        if not db.execute("SELECT 1 FROM courses WHERE id=?", (course_id,)).fetchone():
            raise LookupError("수업을 찾을 수 없습니다.")

    @staticmethod
    def _teacher(db, course_id, teacher_id):
        _identifier(teacher_id, "교수 ID")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='course_members'").fetchone():
            raise PermissionError("담당 교수만 개념을 검토할 수 있습니다.")
        if not db.execute("SELECT 1 FROM course_members WHERE course_id=? AND user_id=? AND role='teacher'", (course_id, teacher_id)).fetchone():
            raise PermissionError("담당 교수만 개념을 검토할 수 있습니다.")

    @staticmethod
    def _material(db, course_id, material_id):
        _identifier(material_id, "자료 ID")
        row = db.execute("SELECT * FROM materials WHERE id=? AND course_id=?", (material_id, course_id)).fetchone()
        if not row:
            raise LookupError("이 수업의 자료를 찾을 수 없습니다.")
        return row

    @staticmethod
    def _concept(db, course_id, concept_id):
        _identifier(concept_id, "개념 ID")
        row = db.execute("SELECT * FROM course_concepts WHERE id=? AND course_id=?", (concept_id, course_id)).fetchone()
        if not row:
            raise LookupError("이 수업의 개념을 찾을 수 없습니다.")
        return row

    @staticmethod
    def _chunks(db, course_id, ids=None, material_id=None):
        sql = ("SELECT c.id AS chunk_id,m.id AS material_id,m.title,c.text,c.page,c.section "
               "FROM chunks c JOIN materials m ON m.id=c.material_id WHERE m.course_id=?")
        args = [course_id]
        if ids is not None:
            if not ids:
                return []
            sql += " AND c.id IN (" + ",".join("?" for _ in ids) + ")"
            args.extend(ids)
        if material_id is not None:
            sql += " AND m.id=?"
            args.append(material_id)
        return [dict(row) for row in db.execute(sql + " ORDER BY c.id", args)]

    def _view(self, db, row):
        value = dict(row)
        value["aliases"] = json.loads(value.pop("aliases_json"))
        ids = [r[0] for r in db.execute("SELECT chunk_id FROM concept_sources WHERE concept_id=? ORDER BY chunk_id", (row["id"],))]
        value["sources"] = self._chunks(db, row["course_id"], ids)
        value["source_chunk_ids"] = [r["chunk_id"] for r in value["sources"]]
        value["available"] = bool(value["sources"])
        target = db.execute("SELECT name FROM course_concepts WHERE id=? AND course_id=?", (row["merge_target_id"], row["course_id"])).fetchone()
        value["merge_target_name"] = target[0] if target else None
        return value

    @staticmethod
    def _job_view(row):
        # Lease tokens are internal concurrency controls, not public state.
        return {k: v for k, v in dict(row).items() if k not in {"lease_token", "lease_until"}}

    def _job(self, db, course_id, material_id):
        row = db.execute("SELECT j.*,m.title FROM material_concept_jobs j JOIN materials m ON m.id=j.material_id WHERE j.material_id=? AND j.course_id=?", (material_id, course_id)).fetchone()
        return self._job_view(row) if row else None

    def enqueue(self, course_id, material_id, *, retry=False):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.enqueue_in_transaction(db, course_id, material_id, retry=retry)

    def enqueue_in_transaction(self, db, course_id, material_id, *, retry=False):
        """Stage a job in the caller's existing material-write transaction.

        This helper neither starts nor commits a transaction, so a material and
        its extraction job are committed or rolled back together.
        """
        self._course(db, course_id)
        self._material(db, course_id, material_id)
        job = self._job(db, course_id, material_id)
        if job and (job["status"] in {"pending", "processing"} or not retry):
            return job
        now = _now()
        db.execute("""INSERT INTO material_concept_jobs(material_id,course_id,status,created_at,updated_at)
            VALUES(?,?,'pending',?,?) ON CONFLICT(material_id) DO UPDATE SET
            status='pending',error='',lease_token=NULL,lease_until=NULL,updated_at=excluded.updated_at""",
            (material_id, course_id, now, now))
        return self._job(db, course_id, material_id)

    def overview(self, course_id):
        with self.service._db() as db:
            self._course(db, course_id)
            return {
                "concepts": [self._view(db, row) for row in db.execute("SELECT * FROM course_concepts WHERE course_id=? ORDER BY id", (course_id,)).fetchall()],
                "jobs": [self._job_view(row) for row in db.execute("SELECT j.*,m.title FROM material_concept_jobs j JOIN materials m ON m.id=j.material_id WHERE j.course_id=? ORDER BY material_id", (course_id,)).fetchall()],
            }

    def _retrieval_catalog(self, db, course_id):
        """All approved, source-backed concepts, without private review metadata.

        Accept the caller's transaction so search can revalidate the exact catalog
        before recording a result after an external model call.
        """
        self._course(db, course_id)
        rows = db.execute(
            "SELECT id,name,description,aliases_json,revision FROM course_concepts "
            "WHERE course_id=? AND status='approved' ORDER BY id", (course_id,),
        ).fetchall()
        sources = {}
        for row in db.execute(
            "SELECT s.concept_id,c.id AS chunk_id,m.id AS material_id,m.title,c.text,c.page,c.section "
            "FROM concept_sources s JOIN course_concepts k ON k.id=s.concept_id "
            "JOIN chunks c ON c.id=s.chunk_id JOIN materials m ON m.id=c.material_id "
            "WHERE k.course_id=? AND k.status='approved' AND m.course_id=? "
            "ORDER BY s.concept_id,c.id", (course_id, course_id),
        ):
            source = dict(row)
            sources.setdefault(source.pop("concept_id"), []).append(source)
        return [{
            "id": row["id"], "name": row["name"], "description": row["description"],
            "aliases": json.loads(row["aliases_json"]), "revision": row["revision"],
            "sources": sources[row["id"]],
            "source_chunk_ids": [source["chunk_id"] for source in sources[row["id"]]],
        } for row in rows if sources.get(row["id"])]

    def retrieval_catalog(self, course_id):
        with self.service._db() as db:
            db.execute("BEGIN")
            return self._retrieval_catalog(db, course_id)

    def get(self, course_id, concept_id):
        with self.service._db() as db:
            self._course(db, course_id)
            return self._view(db, self._concept(db, course_id, concept_id))

    def _payload(self, db, course_id, payload, *, allowed_source_ids=None):
        if not isinstance(payload, dict) or set(payload) - FIELDS:
            raise ValueError("개념 입력 항목을 확인해 주세요. 승인 상태는 입력할 수 없습니다.")
        name = _text(payload.get("name"), "개념 이름", 200)
        if not any(c.isalnum() for c in _key(name)):
            raise ValueError("개념 이름에 문자나 숫자를 포함해 주세요.")
        description = _text(payload.get("description"), "개념 설명", 4000)
        aliases = payload.get("aliases", [])
        ids = payload.get("source_chunk_ids")
        if not isinstance(aliases, list) or len(aliases) > 40:
            raise ValueError("동의어는 40개 이하로 입력해 주세요.")
        aliases = [_text(value, "동의어", 200) for value in aliases]
        if any(not any(c.isalnum() for c in _key(value)) for value in aliases):
            raise ValueError("동의어에 문자나 숫자를 포함해 주세요.")
        aliases = _names(name, aliases)[1:]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 1000:
            raise ValueError("개념의 원문 출처를 1~1,000개 지정해 주세요.")
        ids = list(dict.fromkeys(_identifier(value, "출처 ID") for value in ids))
        if allowed_source_ids is not None and not set(ids) <= allowed_source_ids:
            raise ValueError("추출한 개념의 출처가 원래 자료 범위를 벗어났습니다.")
        if len(self._chunks(db, course_id, ids)) != len(ids):
            raise ValueError("개념의 출처가 삭제되었거나 이 수업에 속하지 않습니다.")
        target_id = payload.get("merge_target_id")
        target_revision = None
        if target_id is not None:
            target = self._concept(db, course_id, target_id)
            if target["status"] != "approved" or not self._view(db, target)["available"]:
                raise ValueError("원문 근거가 있는 승인된 개념만 통합 대상으로 지정할 수 있습니다.")
            target_revision = target["revision"]
        return {"name": name, "description": description, "aliases": aliases,
                "source_chunk_ids": ids, "merge_target_id": target_id,
                "merge_target_revision": target_revision}

    @staticmethod
    def _set_sources(db, concept_id, ids):
        current = {row[0] for row in db.execute("SELECT chunk_id FROM concept_sources WHERE concept_id=?", (concept_id,))}
        for source_id in current - set(ids):
            db.execute("DELETE FROM concept_sources WHERE concept_id=? AND chunk_id=?", (concept_id, source_id))
        db.executemany("INSERT OR IGNORE INTO concept_sources VALUES(?,?)", [(concept_id, i) for i in ids])

    @staticmethod
    def _check_revision(row, expected_revision):
        _identifier(expected_revision, "검토 버전")
        if row["revision"] != expected_revision:
            raise ValueError("개념이나 원문 출처가 변경되었습니다. 최신 내용을 다시 검토해 주세요.")
        if row["status"] != "draft":
            raise ValueError("검토 대기 중인 개념만 수정하거나 승인·제외할 수 있습니다.")

    def update_draft(self, course_id, concept_id, teacher_id, payload, expected_revision):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._teacher(db, course_id, teacher_id)
            row = self._concept(db, course_id, concept_id)
            self._check_revision(row, expected_revision)
            value = self._payload(db, course_id, payload)
            if value["merge_target_id"] == concept_id:
                raise ValueError("개념을 자기 자신에 통합할 수 없습니다.")
            self._set_sources(db, concept_id, value["source_chunk_ids"])
            db.execute("""UPDATE course_concepts SET name=?,description=?,aliases_json=?,merge_target_id=?,
                merge_target_revision=?,revision=revision+1,updated_at=? WHERE id=?""",
                (value["name"], value["description"], json.dumps(value["aliases"], ensure_ascii=False),
                 value["merge_target_id"], value["merge_target_revision"], _now(), concept_id))
            return self._view(db, self._concept(db, course_id, concept_id))

    def approve(self, course_id, concept_id, teacher_id, expected_revision):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._teacher(db, course_id, teacher_id)
            row = self._concept(db, course_id, concept_id)
            self._check_revision(row, expected_revision)
            value = self._view(db, row)
            if not value["available"]:
                raise ValueError("원문 출처가 없어 이 개념을 승인할 수 없습니다.")
            now = _now()
            if row["merge_target_id"] is not None:
                target = self._concept(db, course_id, row["merge_target_id"])
                if target["status"] != "approved" or not self._view(db, target)["available"]:
                    raise ValueError("통합 대상이 유효하지 않습니다. 개념을 다시 검토해 주세요.")
                if target["revision"] != row["merge_target_revision"]:
                    raise ValueError("통합 대상 개념이 변경되었습니다. 최신 대상을 확인하고 초안을 다시 저장해 주세요.")
                # Preserve the approved definition; explicitly approving this draft
                # adds only its reviewed aliases and evidence to the canonical concept.
                aliases = _names(target["name"], [*json.loads(target["aliases_json"]), value["name"], *value["aliases"]])[1:]
                keys = {_key(n) for n in [target["name"], *aliases]}
                for existing in db.execute("SELECT * FROM course_concepts WHERE course_id=? AND status='approved' AND id<>?", (course_id, target["id"])).fetchall():
                    if self._view(db, existing)["available"] and keys & {_key(n) for n in [existing["name"], *json.loads(existing["aliases_json"])]}:
                        raise ValueError("통합할 이름이나 동의어가 다른 승인된 개념과 겹칩니다. 개념과 동의어를 다시 검토해 주세요.")
                db.execute("UPDATE course_concepts SET aliases_json=?,revision=revision+1,updated_at=? WHERE id=?",
                           (json.dumps(aliases, ensure_ascii=False), now, target["id"]))
                db.executemany("INSERT OR IGNORE INTO concept_sources VALUES(?,?)", [(target["id"], source_id) for source_id in value["source_chunk_ids"]])
                status = "merged"
            else:
                # No two canonical concepts may share a normalized name/alias.
                keys = {_key(n) for n in [value["name"], *value["aliases"]]}
                for existing in db.execute("SELECT * FROM course_concepts WHERE course_id=? AND status='approved'", (course_id,)).fetchall():
                    if not self._view(db, existing)["available"]:
                        continue
                    if keys & {_key(n) for n in [existing["name"], *json.loads(existing["aliases_json"]) ]}:
                        raise ValueError("같은 이름이나 동의어의 승인된 개념이 있습니다. 통합 대상을 선택해 다시 검토해 주세요.")
                status = "approved"
            db.execute("UPDATE course_concepts SET status=?,approved_by=?,approved_at=?,reviewed_by=?,reviewed_at=?,revision=revision+1,updated_at=? WHERE id=?",
                       (status, teacher_id, now, teacher_id, now, now, concept_id))
            return self._view(db, self._concept(db, course_id, concept_id))

    def reject(self, course_id, concept_id, teacher_id, expected_revision):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._course(db, course_id)
            self._teacher(db, course_id, teacher_id)
            row = self._concept(db, course_id, concept_id)
            self._check_revision(row, expected_revision)
            now = _now()
            db.execute("UPDATE course_concepts SET status='rejected',reviewed_by=?,reviewed_at=?,revision=revision+1,updated_at=? WHERE id=?", (teacher_id, now, now, concept_id))
            return self._view(db, self._concept(db, course_id, concept_id))

    def _claim(self, model):
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM material_concept_jobs WHERE status='pending' OR (status='processing' AND lease_until<=?) ORDER BY material_id LIMIT 1", (int(time.time()),)).fetchone()
            if not row:
                return None
            token = secrets.token_urlsafe(24)
            db.execute("UPDATE material_concept_jobs SET status='processing',error='',model=?,attempts=attempts+1,lease_token=?,lease_until=?,updated_at=? WHERE material_id=?",
                       (model, token, int(time.time()) + LEASE_SECONDS, _now(), row["material_id"]))
            return {**dict(row), "lease_token": token}

    def _finish_failure(self, job, status="failed", message=FAILURE_MESSAGE):
        with self.service._db() as db:
            db.execute("UPDATE material_concept_jobs SET status=?,error=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE material_id=? AND lease_token=?",
                       (status, message, _now(), job["material_id"], job["lease_token"]))

    async def _heartbeat(self, job):
        """Keep a large multi-batch extraction owned by its original worker."""
        while True:
            await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
            try:
                with self.service._db() as db:
                    updated = db.execute("UPDATE material_concept_jobs SET lease_until=? WHERE material_id=? AND status='processing' AND lease_token=?",
                                         (int(time.time()) + LEASE_SECONDS, job["material_id"], job["lease_token"])).rowcount
                if not updated:
                    return
            except sqlite3.Error:
                # A temporary write lock is retried well within the 30min lease.
                logger.warning("Concept extraction lease renewal failed; retrying.")
                continue

    def _complete(self, job, proposals, model):
        if not isinstance(proposals, list) or len(proposals) > 2000:
            raise ValueError("개념 추출 결과 형식이 올바르지 않습니다.")
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT 1 FROM material_concept_jobs WHERE material_id=? AND status='processing' AND lease_token=?", (job["material_id"], job["lease_token"])).fetchone()
            if not active:  # Removed material or a newer worker owns the lease.
                return
            course_id = job["course_id"]
            allowed_ids = {row["chunk_id"] for row in self._chunks(db, course_id, material_id=job["material_id"])}
            # Validate the whole response before staging even one concept.
            values = [self._payload(db, course_id, proposal, allowed_source_ids=allowed_ids) for proposal in proposals]
            now = _now()
            for value in values:
                keys = {_key(n) for n in [value["name"], *value["aliases"]]}
                live = db.execute("SELECT * FROM course_concepts WHERE course_id=? AND status IN ('draft','approved') ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,id", (course_id,)).fetchall()
                approved_match = None
                draft_match = None
                for existing in live:
                    if not self._view(db, existing)["available"]:
                        continue
                    existing_keys = {_key(n) for n in [existing["name"], *json.loads(existing["aliases_json"])]}
                    if keys & existing_keys:
                        if existing["status"] == "approved" and approved_match is None:
                            approved_match = existing
                        elif existing["status"] == "draft" and draft_match is None:
                            draft_match = existing
                if value["merge_target_id"] is None and approved_match is not None:
                    value["merge_target_id"] = approved_match["id"]
                    value["merge_target_revision"] = approved_match["revision"]
                if value["merge_target_id"] is not None:
                    # Coalesce repeated proposals to the same canonical target.
                    draft_match = next((r for r in live if r["status"] == "draft" and r["merge_target_id"] == value["merge_target_id"]), None)
                    target = self._concept(db, course_id, value["merge_target_id"])
                    if set(value["source_chunk_ids"]) <= set(self._view(db, target)["source_chunk_ids"]) and keys <= {_key(n) for n in [target["name"], *json.loads(target["aliases_json"])]}:
                        continue  # A retry contributes nothing new to approved data.
                elif draft_match is not None and draft_match["merge_target_id"] is not None:
                    draft_match = None
                if draft_match is not None:
                    existing_value = self._view(db, draft_match)
                    aliases = _names(draft_match["name"], [*existing_value["aliases"], value["name"], *value["aliases"]])[1:]
                    sources = list(dict.fromkeys([*existing_value["source_chunk_ids"], *value["source_chunk_ids"]]))
                    # Extraction may enrich unreviewed evidence/aliases but does
                    # not overwrite a teacher-edited definition.
                    if aliases != existing_value["aliases"] or set(sources) != set(existing_value["source_chunk_ids"]):
                        self._set_sources(db, draft_match["id"], sources)
                        db.execute("UPDATE course_concepts SET aliases_json=?,revision=revision+1,updated_at=? WHERE id=?",
                                   (json.dumps(aliases, ensure_ascii=False), now, draft_match["id"]))
                    continue
                concept_id = db.execute("""INSERT INTO course_concepts(course_id,name,description,aliases_json,
                    merge_target_id,merge_target_revision,extraction_model,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (course_id, value["name"], value["description"], json.dumps(value["aliases"], ensure_ascii=False), value["merge_target_id"], value["merge_target_revision"], model, now, now)).lastrowid
                self._set_sources(db, concept_id, value["source_chunk_ids"])
            db.execute("UPDATE material_concept_jobs SET status='ready',error='',lease_token=NULL,lease_until=NULL,model=?,updated_at=? WHERE material_id=? AND lease_token=?",
                       (model, now, job["material_id"], job["lease_token"]))

    async def process_next(self, extractor):
        """Process one durable job. Provider errors never leak request data."""
        model = str(extractor.model)
        job = self._claim(model)
        if job is None:
            return False
        if not extractor.configured:
            self._finish_failure(job, "not_configured", getattr(extractor, "configuration_error", None) or CONFIG_MESSAGE)
            return True
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            with self.service._db() as db:
                chunks = self._chunks(db, job["course_id"], material_id=job["material_id"])
                if not chunks:
                    raise ValueError("자료의 원문이 없습니다.")
                existing = [self._view(db, row) for row in db.execute("SELECT * FROM course_concepts WHERE course_id=? AND status IN ('draft','approved') ORDER BY id", (job["course_id"],)).fetchall()]
            proposals = await extractor.extract(chunks, existing)
            self._complete(job, proposals, model)
        except asyncio.CancelledError:
            try:
                self._finish_failure(job, "pending", "")
            except sqlite3.Error:
                # Preserve shutdown cancellation; the expired lease is recoverable.
                logger.warning("Concept extraction job could not be requeued; lease recovery will retry it.")
            raise
        except Exception as exc:
            from .concept_extraction import ConceptExtractionError
            self._finish_failure(job, message=str(exc) if isinstance(exc, ConceptExtractionError) else FAILURE_MESSAGE)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                # Ignore the heartbeat's cancellation, never our own shutdown.
                if asyncio.current_task().cancelling():
                    raise
        return True

    async def worker(self, extractor):
        """Startup drains pending/expired jobs, then notices newly uploaded files."""
        while True:
            try:
                handled = await self.process_next(extractor)
            except sqlite3.Error:
                # SQL exceptions may include source data; log only a fixed message.
                logger.warning("Concept extraction database operation failed; retrying.")
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(0 if handled else 1)
