"""Local instructor workspace and HTTP MCP endpoints."""

import io
import os
import shlex
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pypdf import PdfReader
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from classroom.mcp_server import ROOT, create_mcp, database_path
from classroom.service import Service
from classroom.judge import JudgeError

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
    load_dotenv(ROOT / ".env")
    service = Service(db_path or database_path(), seed=seed)
    student = create_mcp(service, "student")
    instructor = create_mcp(service, "instructor")
    student_http = student.streamable_http_app()
    instructor_http = instructor.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(student.session_manager.run())
            await stack.enter_async_context(instructor.session_manager.run())
            yield

    app = FastAPI(title="수업 사이 · Classroom Bridge", lifespan=lifespan)
    app.state.service = service
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        # This prototype is a loopback-only instructor workspace.
        origin = request.headers.get("origin")
        if origin and request.method not in {"GET", "HEAD", "OPTIONS"}:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "같은 주소의 수업 화면에서 요청해주세요."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

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
            "student_stdio": shlex.join([sys.executable, "-m", "classroom.mcp_server", "--role", "student"]),
            "instructor_stdio": shlex.join([sys.executable, "-m", "classroom.mcp_server", "--role", "instructor"]),
        }

    @app.get("/api/courses")
    def courses():
        return {"courses": service.list_courses()}

    @app.post("/api/courses", status_code=201)
    def add_course(data: CourseInput):
        return service.create_course(data.title, data.description)

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

    @app.post("/api/ask")
    async def ask(data: AskInput):
        return await service.ask(data.course_id, data.question)

    @app.get("/api/courses/{course_id}/analytics")
    def analytics(course_id: int):
        return service.analytics(course_id)

    @app.get("/api/courses/{course_id}/teaching-brief")
    def teaching_brief(course_id: int):
        return service.teaching_brief(course_id)

    @app.post("/api/courses/{course_id}/exam-plan")
    def exam_plan(course_id: int, data: ExamInput):
        return service.exam_plan(course_id, data.total_questions)

    app.mount("/mcp", Starlette(routes=student_http.routes + instructor_http.routes))
    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(static / "index.html")

    return app
