# Prototype contract

All UI text Korean; product name **수업 사이 / CLASSROOM BRIDGE**. Local prototype with a real SQLite store, MCP transports, and interchangeable Codex CLI / Jev API evidence selection. No generated LLM answers claimed: display source excerpts and a copyable grounded-answer prompt for the external LLM. Course-scoped data. Prototype local instructor UI is not authentication.

Provider selection from server environment, read on startup:
- `CLASSROOM_MODE=auto` (default): nonempty `TYPESAFE_API_KEY` selects Jev; otherwise an installed Codex CLI selects Codex; otherwise lexical demo.
- `CLASSROOM_MODE=jev`: Jev only; a nonempty `TYPESAFE_API_KEY` is required.
- `CLASSROOM_MODE=codex`: Codex CLI only, even when a Jev key is present.
- `CLASSROOM_MODE=demo`: lexical retrieval, with no Codex or Jev calls.
- `JEV_MODEL=jev-latest`; `CODEX_BIN=codex`; empty `CODEX_MODEL` uses the CLI default, ignoring personal Codex configuration.
- Provider failures are surfaced without falling back to another provider. Missing required provider configuration is reported without exposing credentials. `.env` is gitignored and loaded by server entrypoints.

JSON endpoints:
- GET /api/config -> {mode: "demo"|"codex"|"jev", codex_configured: bool, codex_available: bool, jev_configured: bool, model: string, student_mcp: "/mcp/student", instructor_mcp: "/mcp/instructor"}. `codex_available` reports executable availability; `jev_configured` reports only whether a nonempty key is present, not whether it is valid. Never return API keys.
- GET /api/courses -> {courses: [{id:int,title,description,is_demo:bool}]}
- POST /api/courses {title,description?} -> course
- GET /api/courses/{id}/materials -> {materials: [{id,title,filename,chunk_count,created_at,preview}]}
- POST /api/courses/{id}/materials (multipart file, optional title) -> material; TXT/MD/PDF, 10MB max
- POST /api/courses/{id}/materials/text {title,text} -> material
- GET /api/courses/{id}/materials/{material_id} -> {id,title,filename,chunks:[{id,text,page,section}]}
- DELETE /api/courses/{id}/materials/{material_id} -> {deleted:true}
- POST /api/ask {course_id:int,question:string} -> {question_id:int,question,mode:"demo"|"codex"|"jev",sufficient:bool,evidence:[{chunk_id:int,material_id:int,title,text,page:int|null,section:string,score:number}],answer:string,prompt:string,topic:string}. answer is explicitly an extractive preview, NOT generated text. Codex/Jev errors return 502 without silent fallback. No evidence means abstain.
- GET /api/courses/{id}/analytics -> {total_questions:int,real_questions:int,demo_questions:int,material_count:int,grounded_questions:int,insufficient_questions:int,topics:[{topic,count}],daily:[{date,count}],recent_questions:[{id,question,topic,created_at,sufficient,is_demo,mode}],notice:string}
- GET /api/courses/{id}/teaching-brief -> {summary:string,focus_topics:[{topic,count,suggestion}],prompt:string}
- POST /api/courses/{id}/exam-plan {total_questions:int=10} -> {total_questions,distribution:[{difficulty,count}],topics:[{topic,count}],prompt:string,notice:string}. This is a grounded exam-generation handoff, never a claim of guaranteed score distribution or generated exam.

Errors JSON {detail:string}. No authentication credentials in browser config, error responses, or application logs. Source contents are escaped untrusted text.

Python Service methods: Service(db_path: str, seed:bool=True); config(); list_courses(); create_course(title,description=""); list_materials(course_id); add_text(course_id,title,text,filename="",pages: list[tuple[int,str]]|None=None); get_material(course_id,material_id); delete_material(course_id,material_id); async ask(course_id,question); analytics(course_id); teaching_brief(course_id); exam_plan(course_id,total_questions=10). Methods return data objects above (list_courses/list_materials return lists). ValueError for invalid inputs, LookupError missing IDs; JudgeError for provider failure. db_path from CLASSROOM_DB default data/classroom.sqlite; relative DB paths resolve from the project directory. Provider configuration comes from environment variables; `.env` is loaded only by entrypoints.

MCP student tools: list_courses(), search_course_materials(course_id,question) (records question once per invocation). Instructor tools: list_courses(), get_question_insights(course_id), get_teaching_brief(course_id), prepare_exam_plan(course_id,total_questions), add_course_material(course_id,title,text). HTTP mounted /mcp/student and /mcp/instructor (no extra /mcp suffix). Also stdio: python -m classroom.mcp_server --role student|instructor. Local trusted demonstration; no remote hosting/auth claimed.

Verification scope: HTTP and stdio MCP SDK tests; authenticated Codex CLI evidence selection and a student MCP round trip were verified. Jev request/response behavior is tested with mocked provider responses; no successful live Jev credential check is claimed. ChatGPT/Gemini product integrations are not claimed as verified.
