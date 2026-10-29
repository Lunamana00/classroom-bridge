import asyncio
from contextlib import contextmanager
import json
import sqlite3
import time

import pytest

from classroom.auth import Auth
from classroom.concepts import ConceptStore
from classroom.service import Service


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CLASSROOM_MODE", "demo")
    service = Service(str(tmp_path / "concepts.sqlite"), seed=False)
    auth = Auth(service)
    teacher = auth.login_user({"sub": "teacher", "email": "teacher@example.test", "email_verified": True, "name": "Teacher"})
    student = auth.login_user({"sub": "student", "email": "student@example.test", "email_verified": True, "name": "Student"})
    course = service.create_course("미분과 학습", owner_id=teacher["id"])
    with service._db() as db:
        db.execute("INSERT INTO course_members VALUES(?,?,'student')", (course["id"], student["id"]))
    store = ConceptStore(service)
    material = service.add_text(course["id"], "연쇄법칙", "# 연쇄법칙\n\nChain rule differentiates composite functions.")
    chunk_id = service.get_material(course["id"], material["id"])["chunks"][0]["id"]
    return {"service": service, "store": store, "course": course["id"], "teacher": teacher["id"], "student": student["id"], "material": material["id"], "source": chunk_id}


def proposal(source, **changes):
    value = {"name": "연쇄법칙", "description": "합성함수의 미분을 각 함수의 미분의 곱으로 계산합니다.", "aliases": ["Chain Rule"], "source_chunk_ids": [source], "merge_target_id": None}
    value.update(changes)
    return value


class Extractor:
    configured = True
    model = "test-concept-model"

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def extract(self, chunks, existing):
        self.calls.append((chunks, existing))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result(chunks, existing) if callable(self.result) else self.result


async def stage(data, result=None, material_id=None):
    store = data["store"]
    material_id = material_id or data["material"]
    store.enqueue(data["course"], material_id)
    extractor = Extractor(result if result is not None else [proposal(data["source"])])
    assert await store.process_next(extractor)
    return store.overview(data["course"]), extractor


def editable(value, **changes):
    result = {k: value[k] for k in ("name", "description", "aliases", "source_chunk_ids", "merge_target_id")}
    result.update(changes)
    return result


@pytest.mark.asyncio
async def test_upload_extraction_stages_draft_then_explicit_teacher_approval(data):
    state, extractor = await stage(data)
    draft = state["concepts"][0]
    assert state["jobs"][0]["status"] == "ready"
    assert draft["status"] == "draft" and draft["approved_by"] is None
    assert draft["extraction_model"] == extractor.model
    assert draft["sources"][0]["text"] == "# 연쇄법칙\n\nChain rule differentiates composite functions."
    assert draft["sources"][0]["material_id"] == data["material"]
    assert extractor.calls[0][0][0]["chunk_id"] == data["source"]
    approved = data["store"].approve(data["course"], draft["id"], data["teacher"], draft["revision"])
    assert approved["status"] == "approved"
    assert approved["approved_by"] == data["teacher"] and approved["approved_at"]
    with pytest.raises(ValueError):
        data["store"].update_draft(data["course"], approved["id"], data["teacher"], editable(approved), approved["revision"])
    assert ConceptStore(data["service"]).get(data["course"], draft["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_extraction_coalesces_aliases_and_sources_without_conflating_different_concepts(data):
    result = [proposal(data["source"]), proposal(data["source"], name="chain-rule", aliases=["연쇄 법칙", "합성함수 미분"]), proposal(data["source"], name="합성함수", aliases=[])]
    state, _ = await stage(data, result)
    assert len(state["concepts"]) == 2
    chain = next(c for c in state["concepts"] if c["name"] == "연쇄법칙")
    assert "합성함수 미분" in chain["aliases"]
    assert chain["source_chunk_ids"] == [data["source"]]
    assert all(c["status"] == "draft" for c in state["concepts"])


@pytest.mark.asyncio
async def test_later_upload_updates_only_unapproved_draft_and_invalidates_old_review(data):
    initial, _ = await stage(data)
    old = initial["concepts"][0]
    material = data["service"].add_text(data["course"], "미분 복습", "Chain Rule also describes gradients in neural networks.")
    source = data["service"].get_material(data["course"], material["id"])["chunks"][0]["id"]
    state, _ = await stage(data, [proposal(source, name="Chain Rule", aliases=["연쇄법칙", "chain differentiation"])], material["id"])
    assert len(state["concepts"]) == 1
    current = state["concepts"][0]
    assert set(current["source_chunk_ids"]) == {data["source"], source}
    assert current["revision"] > old["revision"]
    with pytest.raises(ValueError, match="변경"):
        data["store"].approve(data["course"], current["id"], data["teacher"], old["revision"])


@pytest.mark.asyncio
async def test_approved_concept_changes_only_after_merge_draft_approval(data):
    state, _ = await stage(data)
    draft = state["concepts"][0]
    canonical = data["store"].approve(data["course"], draft["id"], data["teacher"], draft["revision"])
    material = data["service"].add_text(data["course"], "역전파", "Backpropagation uses the chain rule to combine local gradients.")
    source = data["service"].get_material(data["course"], material["id"])["chunks"][0]["id"]
    state, _ = await stage(data, [proposal(source, name="chain rule", aliases=["연쇄법칙", "chain differentiation"], description="검토 전 설명")], material["id"])
    before = data["store"].get(data["course"], canonical["id"])
    assert before == canonical
    merge = next(c for c in state["concepts"] if c["status"] == "draft")
    assert merge["merge_target_id"] == canonical["id"]
    approved = data["store"].approve(data["course"], merge["id"], data["teacher"], merge["revision"])
    assert approved["status"] == "merged"
    target = data["store"].get(data["course"], canonical["id"])
    assert target["description"] == canonical["description"]
    assert "chain differentiation" in target["aliases"]
    assert set(target["source_chunk_ids"]) == {data["source"], source}
    # Reprocessing the same source does not create redundant merge drafts.
    data["store"].enqueue(data["course"], material["id"], retry=True)
    await data["store"].process_next(Extractor([proposal(source, name="chain rule", aliases=["연쇄법칙", "chain differentiation"])]))
    assert len(data["store"].overview(data["course"])["concepts"]) == 2


@pytest.mark.asyncio
async def test_teacher_can_edit_reject_and_choose_explicit_merge_target(data):
    state, _ = await stage(data, [proposal(data["source"]), proposal(data["source"], name="검토용 표기", aliases=[])])
    first, second = state["concepts"]
    canonical = data["store"].approve(data["course"], first["id"], data["teacher"], first["revision"])
    edited = data["store"].update_draft(data["course"], second["id"], data["teacher"], editable(second, merge_target_id=canonical["id"], description="교수가 확인한 정의"), second["revision"])
    assert edited["merge_target_name"] == canonical["name"]
    rejected = data["store"].reject(data["course"], edited["id"], data["teacher"], edited["revision"])
    assert rejected["status"] == "rejected"
    assert rejected["reviewed_by"] == data["teacher"]
    assert data["store"].get(data["course"], canonical["id"])["aliases"] == canonical["aliases"]


@pytest.mark.asyncio
async def test_student_and_foreign_course_cannot_review_or_reference_other_sources(data):
    state, _ = await stage(data)
    draft = state["concepts"][0]
    for action in ("approve", "reject"):
        with pytest.raises(PermissionError):
            getattr(data["store"], action)(data["course"], draft["id"], data["student"], draft["revision"])
    with pytest.raises(PermissionError):
        data["store"].update_draft(data["course"], draft["id"], data["student"], editable(draft), draft["revision"])
    other = data["service"].create_course("다른 수업", owner_id=data["teacher"])["id"]
    other_material = data["service"].add_text(other, "다른 자료", "Foreign course evidence.")
    foreign_source = data["service"].get_material(other, other_material["id"])["chunks"][0]["id"]
    with pytest.raises(LookupError):
        data["store"].get(other, draft["id"])
    with pytest.raises(LookupError):
        data["store"].enqueue(other, data["material"])
    with pytest.raises(ValueError, match="출처"):
        data["store"].update_draft(data["course"], draft["id"], data["teacher"], editable(draft, source_chunk_ids=[foreign_source]), draft["revision"])


@pytest.mark.asyncio
async def test_source_deletion_invalidates_review_and_removes_empty_concepts_from_availability(data):
    state, _ = await stage(data)
    draft = state["concepts"][0]
    data["service"].delete_material(data["course"], data["material"])
    current = data["store"].get(data["course"], draft["id"])
    assert not current["available"] and current["source_chunk_ids"] == []
    assert current["revision"] > draft["revision"]
    assert data["store"].overview(data["course"])["jobs"] == []
    with pytest.raises(ValueError, match="변경"):
        data["store"].approve(data["course"], draft["id"], data["teacher"], draft["revision"])
    with pytest.raises(ValueError, match="출처"):
        data["store"].approve(data["course"], draft["id"], data["teacher"], current["revision"])


@pytest.mark.asyncio
async def test_merge_target_revision_must_still_match_teacher_review(data):
    state, _ = await stage(data)
    first = state["concepts"][0]
    target = data["store"].approve(data["course"], first["id"], data["teacher"], first["revision"])
    material = data["service"].add_text(data["course"], "새 근거", "The chain rule has another explanation.")
    source = data["service"].get_material(data["course"], material["id"])["chunks"][0]["id"]
    state, _ = await stage(data, [proposal(source)], material["id"])
    merge = next(c for c in state["concepts"] if c["status"] == "draft")
    with data["service"]._db() as db:
        db.execute("UPDATE course_concepts SET revision=revision+1 WHERE id=?", (target["id"],))
    with pytest.raises(ValueError, match="통합 대상.*변경"):
        data["store"].approve(data["course"], merge["id"], data["teacher"], merge["revision"])
    refreshed = data["store"].update_draft(data["course"], merge["id"], data["teacher"], editable(merge), merge["revision"])
    assert data["store"].approve(data["course"], merge["id"], data["teacher"], refreshed["revision"])["status"] == "merged"


@pytest.mark.asyncio
async def test_missing_configuration_is_explicit_and_retryable(data):
    extractor = Extractor([proposal(data["source"])])
    extractor.configured = False
    data["store"].enqueue(data["course"], data["material"])
    assert await data["store"].process_next(extractor)
    state = data["store"].overview(data["course"])
    assert state["jobs"][0]["status"] == "not_configured" and state["jobs"][0]["error"]
    assert not state["concepts"] and not extractor.calls
    data["store"].enqueue(data["course"], data["material"], retry=True)
    extractor.configured = True
    assert await data["store"].process_next(extractor)
    assert data["store"].overview(data["course"])["jobs"][0]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [RuntimeError("PRIVATE_API_KEY credentials secret source text"), [{"name": "invalid"}], [proposal(999999)]])
async def test_failed_provider_or_malformed_data_leaves_no_partial_concepts_or_secret_errors(data, result):
    state, _ = await stage(data, [proposal(data["source"]), *result] if isinstance(result, list) else result)
    assert not state["concepts"]
    assert state["jobs"][0]["status"] == "failed"
    assert "PRIVATE_API_KEY" not in json.dumps(state)
    assert state["jobs"][0]["error"]


@pytest.mark.asyncio
async def test_extractor_cannot_cite_another_material_even_in_same_course(data):
    material = data["service"].add_text(data["course"], "다른 자료", "Same course but not supplied to the model.")
    foreign_source = data["service"].get_material(data["course"], material["id"])["chunks"][0]["id"]
    state, _ = await stage(data, [proposal(foreign_source)])
    job = next(j for j in state["jobs"] if j["material_id"] == data["material"])
    assert job["status"] == "failed" and not state["concepts"]


@pytest.mark.asyncio
async def test_concurrent_workers_claim_a_job_once_and_cancel_requeues_it(data):
    started = asyncio.Event()
    release = asyncio.Event()

    class Waiting(Extractor):
        async def extract(self, chunks, existing):
            started.set()
            await release.wait()
            return self.result

    data["store"].enqueue(data["course"], data["material"])
    task = asyncio.create_task(data["store"].process_next(Waiting([proposal(data["source"])])))
    await started.wait()
    assert not await ConceptStore(data["service"]).process_next(Extractor([]))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert data["store"].overview(data["course"])["jobs"][0]["status"] == "pending"
    assert await data["store"].process_next(Extractor([proposal(data["source"])]))
    assert len(data["store"].overview(data["course"])["concepts"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, sqlite3.DatabaseError])
async def test_worker_retries_database_errors_without_leaking_details(data, monkeypatch, caplog, error_type):
    store = data["store"]
    original_claim = store._claim
    attempts = 0
    extracted = asyncio.Event()

    def claim(model):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error_type("PRIVATE_SQL_SOURCE_TEXT")
        return original_claim(model)

    def result(chunks, existing):
        extracted.set()
        return [proposal(data["source"])]

    monkeypatch.setattr(store, "_claim", claim)
    task = asyncio.create_task(store.worker(Extractor(result)))
    try:
        await asyncio.wait_for(extracted.wait(), timeout=3)
        assert store.overview(data["course"])["jobs"][0]["status"] == "ready"
        assert attempts >= 2 and not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "database operation failed; retrying" in caplog.text
    assert "PRIVATE_SQL_SOURCE_TEXT" not in caplog.text


@pytest.mark.asyncio
async def test_worker_does_not_hide_programmer_errors(data, monkeypatch):
    async def broken_process(extractor):
        raise RuntimeError("unexpected worker bug")

    monkeypatch.setattr(data["store"], "process_next", broken_process)
    with pytest.raises(RuntimeError, match="unexpected worker bug"):
        await data["store"].worker(Extractor([]))


@pytest.mark.asyncio
async def test_cancellation_survives_database_error_during_requeue(data, monkeypatch, caplog):
    started = asyncio.Event()

    class Waiting(Extractor):
        async def extract(self, chunks, existing):
            started.set()
            await asyncio.Event().wait()

    def fail_requeue(*args, **kwargs):
        raise sqlite3.OperationalError("PRIVATE_SQL_SOURCE_TEXT")

    monkeypatch.setattr(data["store"], "_finish_failure", fail_requeue)
    task = asyncio.create_task(data["store"].worker(Waiting([])))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    job = data["store"].overview(data["course"])["jobs"][0]
    assert job["status"] == "processing"  # Its lease allows a later worker to recover.
    assert "could not be requeued" in caplog.text
    assert "PRIVATE_SQL_SOURCE_TEXT" not in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_recovers_database_error_and_finishes_cleanly(data, monkeypatch, caplog):
    import classroom.concepts as concepts
    monkeypatch.setattr(concepts, "LEASE_HEARTBEAT_SECONDS", 0.005)
    started, recovered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Waiting(Extractor):
        async def extract(self, chunks, existing):
            started.set()
            await release.wait()
            return self.result

    task = asyncio.create_task(data["store"].process_next(Waiting([proposal(data["source"])])))
    await asyncio.wait_for(started.wait(), timeout=1)
    original_db = data["service"]._db
    calls = 0

    @contextmanager
    def flaky_db():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("PRIVATE_SQL_SOURCE_TEXT")
        with original_db() as db:
            yield db
        recovered.set()

    monkeypatch.setattr(data["service"], "_db", flaky_db)
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        release.set()
        assert await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert data["store"].overview(data["course"])["jobs"][0]["status"] == "ready"
    assert "lease renewal failed; retrying" in caplog.text
    assert "PRIVATE_SQL_SOURCE_TEXT" not in caplog.text


@pytest.mark.asyncio
async def test_expired_lease_recovers_after_restart_and_old_result_cannot_overwrite(data):
    data["store"].enqueue(data["course"], data["material"])
    abandoned = data["store"]._claim("old")
    with data["service"]._db() as db:
        db.execute("UPDATE material_concept_jobs SET lease_until=?", (int(time.time()) - 1,))
    restarted = ConceptStore(data["service"])
    assert await restarted.process_next(Extractor([proposal(data["source"])]))
    restarted._complete(abandoned, [proposal(data["source"], name="obsolete", aliases=[])], "old")
    state = restarted.overview(data["course"])
    assert [c["name"] for c in state["concepts"]] == ["연쇄법칙"]
    assert state["jobs"][0]["attempts"] == 2
    assert "lease_token" not in state["jobs"][0]


@pytest.mark.asyncio
async def test_reupload_can_approve_same_name_after_old_approved_sources_are_deleted(data):
    state, _ = await stage(data)
    first = state["concepts"][0]
    old = data["store"].approve(data["course"], first["id"], data["teacher"], first["revision"])
    data["service"].delete_material(data["course"], data["material"])
    material = data["service"].add_text(data["course"], "다시 올린 연쇄법칙", "The chain rule differentiates a composition.")
    source = data["service"].get_material(data["course"], material["id"])["chunks"][0]["id"]
    state, _ = await stage(data, [proposal(source)], material["id"])
    new = next(c for c in state["concepts"] if c["status"] == "draft")
    assert new["merge_target_id"] is None
    assert data["store"].approve(data["course"], new["id"], data["teacher"], new["revision"])["status"] == "approved"
    assert not data["store"].get(data["course"], old["id"])["available"]


@pytest.mark.asyncio
async def test_merge_approval_cannot_introduce_alias_collision_between_canonical_concepts(data):
    state, _ = await stage(data, [proposal(data["source"]), proposal(data["source"], name="합성함수", aliases=["Composite Function"]), proposal(data["source"], name="추가 표기", aliases=[])])
    chain, composite, extra = state["concepts"]
    chain = data["store"].approve(data["course"], chain["id"], data["teacher"], chain["revision"])
    data["store"].approve(data["course"], composite["id"], data["teacher"], composite["revision"])
    edited = data["store"].update_draft(data["course"], extra["id"], data["teacher"], editable(extra, aliases=["Composite Function"], merge_target_id=chain["id"]), extra["revision"])
    with pytest.raises(ValueError, match="다른 승인된 개념"):
        data["store"].approve(data["course"], extra["id"], data["teacher"], edited["revision"])
    assert data["store"].get(data["course"], chain["id"]) == chain
    assert data["store"].get(data["course"], extra["id"])["status"] == "draft"


@pytest.mark.asyncio
async def test_long_extraction_renews_lease_so_other_workers_do_not_duplicate_cost(data, monkeypatch):
    import classroom.concepts as concepts
    monkeypatch.setattr(concepts, "LEASE_HEARTBEAT_SECONDS", 0.005)
    started, release = asyncio.Event(), asyncio.Event()

    class Waiting(Extractor):
        async def extract(self, chunks, existing):
            started.set()
            await release.wait()
            return self.result

    data["store"].enqueue(data["course"], data["material"])
    task = asyncio.create_task(data["store"].process_next(Waiting([proposal(data["source"])])))
    await started.wait()
    with data["service"]._db() as db:
        db.execute("UPDATE material_concept_jobs SET lease_until=?", (int(time.time()) - 1,))
    await asyncio.sleep(0.03)
    assert not await ConceptStore(data["service"]).process_next(Extractor([]))
    release.set()
    await task
    assert data["store"].overview(data["course"])["jobs"][0]["attempts"] == 1


@pytest.mark.asyncio
async def test_safe_extraction_error_is_visible_and_configuration_reason_is_preserved(data):
    from classroom.concept_extraction import ConceptExtractionError
    state, _ = await stage(data, ConceptExtractionError("개념 추출 API 응답 시간이 초과되었습니다. 다시 시도해 주세요."))
    assert "시간이 초과" in state["jobs"][0]["error"]
    extractor = Extractor([])
    extractor.configured = False
    extractor.configuration_error = "개념 자동 추출이 비활성화되어 있습니다."
    data["store"].enqueue(data["course"], data["material"], retry=True)
    await data["store"].process_next(extractor)
    assert data["store"].overview(data["course"])["jobs"][0]["error"] == extractor.configuration_error


@pytest.mark.asyncio
async def test_symbol_distinct_concepts_can_be_approved_as_separate_canonical_entries(data):
    names = ["C", "C++", "C#", "x^2", "x2", "x-y", "xy", "x_y"]
    state, _ = await stage(data, [proposal(data["source"], name=name, aliases=[]) for name in names])
    assert len(state["concepts"]) == len(names)
    for draft in state["concepts"]:
        approved = data["store"].approve(data["course"], draft["id"], data["teacher"], draft["revision"])
        assert approved["status"] == "approved"
    assert {c["name"] for c in data["store"].overview(data["course"])["concepts"]} == set(names)


def test_material_and_job_can_commit_atomically_in_caller_transaction(data):
    service, store = data["service"], data["store"]
    with service._db() as db:
        db.execute("BEGIN IMMEDIATE")
        material_id = service._insert_material(db, data["course"], "원자적 등록", "Source and job commit together.", "", None)
        job = store.enqueue_in_transaction(db, data["course"], material_id)
        assert job["status"] == "pending"
        # Repeated enqueue within the same transaction is idempotent.
        assert store.enqueue_in_transaction(db, data["course"], material_id) == job
    assert service.get_material(data["course"], material_id)["chunks"]
    assert any(j["material_id"] == material_id for j in store.overview(data["course"])["jobs"])
    with pytest.raises(RuntimeError):
        with service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rolled_back_id = service._insert_material(db, data["course"], "롤백 자료", "This must not be persisted.", "", None)
            store.enqueue_in_transaction(db, data["course"], rolled_back_id)
            raise RuntimeError("abort both writes")
    with service._db() as db:
        assert db.execute("SELECT 1 FROM materials WHERE id=?", (rolled_back_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM material_concept_jobs WHERE material_id=?", (rolled_back_id,)).fetchone() is None
