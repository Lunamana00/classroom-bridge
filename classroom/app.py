"""Local instructor workspace and HTTP MCP endpoints."""

import asyncio
import io
import os
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware

from classroom.config import load_environment
from classroom.mcp_server import ROOT, create_mcp, database_path
from classroom.oauth import MCPOAuth, SCOPES
from mcp.server.auth.routes import create_protected_resource_routes
from pydantic import AnyHttpUrl
from starlette.routing import Route
from classroom.service import Service
from classroom.judge import JudgeError
from classroom.auth import Auth

MAX_UPLOAD = 10 * 1024 * 1024
MAX_TEXT = 1_000_000


class CourseInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)


class TextInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class AskInput(BaseModel):
    course_id: int = Field(gt=0)
    question: str = Field(min_length=1, max_length=2000)


class ExamInput(BaseModel):
    total_questions: int = Field(default=10, ge=1, le=50)
class QuizDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    concepts: list[str] = Field(min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=4000)
    kind: Literal["multiple_choice", "short_answer"]
    options: list[str] = Field(default_factory=list, max_length=6)
    correct_answer: str = Field(min_length=1, max_length=1000)
    accepted_answers: list[str] = Field(default_factory=list, max_length=20)
    explanation: str = Field(min_length=1, max_length=4000)
    source_chunk_ids: list[int] = Field(min_length=1, max_length=8)


class QuizAnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delivery_id: str = Field(min_length=1, max_length=128)
    answer: str = Field(min_length=1, max_length=4000)


class QuizApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)


class ConceptDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(gt=0, strict=True)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=40)
    source_chunk_ids: list[Annotated[int, Field(gt=0, strict=True)]] = Field(min_length=1, max_length=1000)
    merge_target_id: int | None = Field(default=None, gt=0, strict=True)


def parse_document(filename: str, content: bytes) -> tuple[str, list[tuple[int, str]] | None]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("cp949")
            except UnicodeDecodeError as exc:
                raise ValueError("텍스트 파일을 UTF-8 형식으로 저장한 뒤 다시 올려주세요.") from exc
        pages = None
    elif suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise ValueError("암호를 해제한 PDF를 올려주세요.")
            if len(reader.pages) > 500:
                raise ValueError("PDF는 500쪽 이하로 나누어 올려주세요.")
            pages = []
            length = 0
            for number, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                length += len(page_text)
                if length > MAX_TEXT:
                    raise ValueError("자료의 텍스트가 너무 깁니다. 여러 파일로 나누어 올려주세요.")
                if page_text.strip():
                    pages.append((number, page_text))
            text = "\n\n".join(page_text for _, page_text in pages)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("이 PDF를 읽지 못했습니다. 정상적인 텍스트 PDF인지 확인해주세요.") from exc
    else:
        raise ValueError("PDF, TXT, MD 파일을 올려주세요.")
    if not text.strip():
        raise ValueError("읽을 수 있는 텍스트가 없습니다. 스캔 PDF는 문자 인식 후 올려주세요.")
    if len(text) > MAX_TEXT:
        raise ValueError("자료의 텍스트가 너무 깁니다. 여러 파일로 나누어 올려주세요.")
    return text, pages


def create_app(db_path: str | None = None, seed: bool = True) -> FastAPI:
    load_environment()
    service = Service(db_path or database_path(), seed=seed)
    auth = Auth(service)
    async def course_permission(request: Request):
        if not request.url.path.startswith("/api/"):
            return
        user = request.state.user
        if request.url.path == "/api/courses" and request.method == "POST":
            if user["email"] not in auth.teacher_emails:
                raise HTTPException(403, "수업 생성은 등록된 교수만 가능합니다.")
        course_id = request.path_params.get("course_id")
        if request.url.path == "/api/ask":
            data = await request.json()
            course_id = data.get("course_id") if isinstance(data, dict) else None
            if not isinstance(course_id, int) or isinstance(course_id, bool) or course_id <= 0:
                raise HTTPException(422, "수업 ID를 확인해 주세요.")
        if course_id is not None:
            member_route = request.url.path == "/api/ask" or request.url.path.endswith(("/quiz-progress", "/quiz-answers")) or (request.method == "GET" and "/materials" in request.url.path and "/extract-concepts" not in request.url.path)
            teacher = not member_route
            auth.require_course(user, course_id, teacher=teacher)

    app = FastAPI(title="KHU조교 · KHUsistant", dependencies=[Depends(course_permission)])
    app.state.service = service
    app.state.auth = auth
    auth.install(app)
    oauth = MCPOAuth(auth)
    app.state.mcp_oauth = oauth
    oauth.install(app)
    servers = []
    for role in ("student", "instructor"):
        server = create_mcp(service, role, oauth)
        servers.append(server)
        # Forward to the complete SDK ASGI app, retaining its authentication
        # middleware instead of copying unwrapped transport routes.
        transport = CORSMiddleware(
            server.streamable_http_app(), allow_origins=auth.mcp_origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version", "Mcp-Session-Id", "Last-Event-ID"],
            expose_headers=["WWW-Authenticate", "Mcp-Session-Id", "MCP-Protocol-Version"],
            allow_credentials=False,
        )
        app.router.routes.append(Route(f"/mcp/{role}", transport, methods=["GET", "POST", "DELETE", "OPTIONS"]))
        app.router.routes.extend(create_protected_resource_routes(
            resource_url=AnyHttpUrl(auth.base_url + "/mcp/" + role),
            authorization_servers=[AnyHttpUrl(auth.base_url)], scopes_supported=[SCOPES[role]],
        ))

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            worker = asyncio.create_task(service.concepts.worker(service.concept_extractor))
            try:
                yield
            finally:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
    app.router.lifespan_context = lifespan
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver", auth.hostname])

    @app.middleware("http")
    async def browser_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        path = request.url.path.rstrip("/")
        browser_mutation = path.startswith("/api/") or path == "/auth/logout"
        # Redirect chains can produce an opaque Origin on the consent form.
        # That route independently requires the signed-in user's CSRF token.
        consent_with_origin = path == "/oauth/consent" and origin != "null"
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"} and (browser_mutation or consent_with_origin):
            if origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "같은 주소의 수업 화면에서 요청해주세요."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(PermissionError)
    async def denied(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(JudgeError)
    async def provider_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "입력값의 길이와 형식을 확인해주세요."}, status_code=422)

    @app.get("/api/config")
    def config():
        return {
            **service.config(),
            "student_stdio": None,
            "instructor_stdio": None,
        }

    @app.get("/api/courses")
    def courses(request: Request):
        return {"courses": auth.courses(request.state.user)}

    @app.post("/api/courses", status_code=201)
    def add_course(data: CourseInput, request: Request):
        return service.create_course(data.title, data.description, owner_id=request.state.user["id"])

    @app.get("/api/courses/{course_id}/materials")
    def materials(course_id: int):
        return {"materials": service.list_materials(course_id)}

    @app.post("/api/courses/{course_id}/materials/text", status_code=201)
    def add_text(course_id: int, data: TextInput):
        return service.add_text(course_id, data.title, data.text)

    @app.post("/api/courses/{course_id}/materials", status_code=201)
    async def upload(course_id: int, file: UploadFile = File(...), title: str = Form("")):
        content = await file.read(MAX_UPLOAD + 1)
        await file.close()
        if len(content) > MAX_UPLOAD:
            return JSONResponse({"detail": "파일은 10MB 이하로 올려주세요."}, status_code=413)
        filename = Path(file.filename or "자료").name
        text, pages = await run_in_threadpool(parse_document, filename, content)
        return await run_in_threadpool(service.add_text, course_id, title.strip() or Path(filename).stem, text, filename, pages)

    @app.get("/api/courses/{course_id}/materials/{material_id}")
    def material(course_id: int, material_id: int):
        return service.get_material(course_id, material_id)

    @app.delete("/api/courses/{course_id}/materials/{material_id}")
    def delete(course_id: int, material_id: int):
        return service.delete_material(course_id, material_id)

    @app.post("/api/courses/{course_id}/materials/{material_id}/extract-concepts", status_code=202)
    def extract_concepts(course_id: int, material_id: int):
        return service.concepts.enqueue(course_id, material_id, retry=True)

    @app.get("/api/courses/{course_id}/concepts")
    def concepts(course_id: int):
        return service.concepts.overview(course_id)

    @app.get("/api/courses/{course_id}/concepts/{concept_id}")
    def concept(course_id: int, concept_id: int):
        return service.concepts.get(course_id, concept_id)

    @app.put("/api/courses/{course_id}/concepts/{concept_id}")
    def edit_concept(course_id: int, concept_id: int, data: ConceptDraftInput, request: Request):
        return service.concepts.update_draft(
            course_id, concept_id, request.state.user["id"],
            data.model_dump(exclude={"expected_revision"}), data.expected_revision,
        )

    @app.post("/api/courses/{course_id}/concepts/{concept_id}/approve")
    def approve_concept(course_id: int, concept_id: int, data: QuizApprovalInput, request: Request):
        return service.concepts.approve(course_id, concept_id, request.state.user["id"], data.expected_revision)

    @app.post("/api/courses/{course_id}/concepts/{concept_id}/reject")
    def reject_concept(course_id: int, concept_id: int, data: QuizApprovalInput, request: Request):
        return service.concepts.reject(course_id, concept_id, request.state.user["id"], data.expected_revision)

    @app.post("/api/ask")
    async def ask(data: AskInput, request: Request):
        return await service.ask(data.course_id, data.question, student_id=request.state.user["id"])

    @app.get("/api/courses/{course_id}/analytics")
    def analytics(course_id: int):
        return service.analytics(course_id)

    @app.get("/api/courses/{course_id}/question-statistics")
    def question_statistics(course_id: int, topic: str | None = None,
                            offset: int = Query(default=0, ge=0),
                            limit: int = Query(default=20, ge=1, le=100)):
        return service.question_statistics(course_id, topic=topic, offset=offset, limit=limit)

    @app.get("/api/courses/{course_id}/teaching-brief")
    def teaching_brief(course_id: int):
        return service.teaching_brief(course_id)

    @app.post("/api/courses/{course_id}/exam-plan")
    def exam_plan(course_id: int, data: ExamInput):
        return service.exam_plan(course_id, data.total_questions)

    @app.get("/api/courses/{course_id}/quizzes")
    def quizzes(course_id: int):
        return {"quizzes": service.quizzes.list_quizzes(course_id)}

    @app.post("/api/courses/{course_id}/quizzes", status_code=201)
    def create_quiz(course_id: int, data: QuizDraftInput, request: Request):
        return service.quizzes.create_draft(course_id, request.state.user["id"], data.model_dump())

    @app.put("/api/courses/{course_id}/quizzes/{quiz_id}")
    def edit_quiz(course_id: int, quiz_id: int, data: QuizDraftInput, request: Request):
        return service.quizzes.update_draft(course_id, quiz_id, request.state.user["id"], data.model_dump())

    @app.post("/api/courses/{course_id}/quizzes/{quiz_id}/publish")
    def publish_quiz(course_id: int, quiz_id: int, data: QuizApprovalInput, request: Request):
        # Web approval verifies the browser session, CSRF and reviewed revision.
        return service.quizzes.publish(course_id, quiz_id, request.state.user["id"], data.expected_revision)

    @app.post("/api/courses/{course_id}/quizzes/{quiz_id}/archive")
    def archive_quiz(course_id: int, quiz_id: int, request: Request):
        return service.quizzes.archive(course_id, quiz_id, request.state.user["id"])

    @app.get("/api/courses/{course_id}/quiz-insights")
    def quiz_insights(course_id: int):
        return service.quiz_insights(course_id)

    @app.get("/api/courses/{course_id}/quiz-progress")
    def quiz_progress(course_id: int, request: Request):
        return service.quizzes.student_progress(course_id, request.state.user["id"])

    @app.post("/api/courses/{course_id}/quiz-answers")
    def quiz_answer(course_id: int, data: QuizAnswerInput, request: Request):
        return service.quizzes.submit_answer(course_id, data.delivery_id, request.state.user["id"], data.answer)

    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    return app
