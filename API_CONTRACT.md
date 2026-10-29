# Prototype contract

All UI text Korean; product name **수업 사이 / CLASSROOM BRIDGE**. Local prototype with a real SQLite store, MCP transports, and FactChat gateway evidence selection through the official OpenAI SDK. No generated LLM answers claimed: display source excerpts and a copyable grounded-answer prompt for the external LLM. Course-scoped data. Google OIDC web login and course-scoped memberships protect the web API. HTTP MCP uses OAuth; stdio is unsupported.

Provider selection from server environment, read on startup:
- `CLASSROOM_MODE=auto` (default): nonempty `OPEN_API_KEY` (or `OPENAI_API_KEY`) selects codex mode; otherwise lexical demo.
- `CLASSROOM_MODE=codex`: OpenAI Responses API with `CODEX_MODEL=gpt-6-luna`.
- `CLASSROOM_MODE=jev`: FactChat Decisions API with `JEV_MODEL=jev-1.14.0`; SDK `post("/decisions/", cast_to=dict, body=...)` uses the gateway state/questions (noul) schema. No direct TypeSafe calls.
- `CLASSROOM_MODE=demo`: lexical retrieval, with no API calls.
- Explicit API modes require a key. Failures are surfaced without fallback. Both modes use official OpenAI Python SDK with configurable `OPENAI_BASE_URL` (default `https://factchat-cloud.mindlogic.ai/v1/gateway`) and `max_retries=0`.
- GPT uses `responses.create`, `store=false`, no tools, and strict JSON Schema, with ID and score validation. Jev uses `/decisions/`, bounds concurrency to two, filters relevance/evidence/injection scores, preserves source text, selects up to three passages, then checks combined sufficiency. Jev's ranking score is a heuristic, not a calibrated correctness probability. The Responses-only `store` parameter is not sent to Jev.
- Entrypoints load environment in this precedence: process variables, `.env.local`, `.env`. Both files are gitignored. `OPEN_API_KEY` takes precedence over the standard alias.

JSON endpoints:
- GET /api/config -> {mode: "demo"|"codex"|"jev", codex_configured: bool, codex_available: bool, jev_configured: bool, model: string, student_mcp: "/mcp/student", instructor_mcp: "/mcp/instructor"}. `codex_available` and `jev_configured` report whether the shared OpenAI key is nonempty, not whether authentication or model access works. Legacy field names are retained for compatibility. Never return API keys.
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

Python Service methods: Service(db_path: str, seed:bool=True); config(); list_courses(); create_course(title,description=""); list_materials(course_id); add_text(course_id,title,text,filename="",pages: list[tuple[int,str]]|None=None); get_material(course_id,material_id); delete_material(course_id,material_id); async ask(course_id,question); analytics(course_id); teaching_brief(course_id); exam_plan(course_id,total_questions=10). Methods return data objects above (list_courses/list_materials return lists). ValueError for invalid inputs, LookupError missing IDs; JudgeError for provider failure. db_path from CLASSROOM_DB default data/classroom.sqlite; relative DB paths resolve from the project directory. Provider configuration comes from environment variables; `.env.local` and `.env` are loaded only by entrypoints.

MCP student tools: list_courses(), search_course_materials(course_id,question) (records question once per invocation). Instructor tools: list_courses(), get_question_insights(course_id), get_teaching_brief(course_id), prepare_exam_plan(course_id,total_questions), add_course_material(course_id,title,text). HTTP mounted /mcp/student and /mcp/instructor (no extra /mcp suffix). stdio is unsupported; use the HTTPS OAuth endpoints. The deployed HTTPS endpoints require OAuth bearer tokens and enforce course memberships.

Verification scope: HTTP MCP SDK tests, browser callback verification, and explicit rejection of unsupported stdio; both gateway modes are tested using mocked SDK transports, including source preservation, validation, errors, credential isolation, and no fallback. Live evidence selection with the local key succeeded for gpt-6-luna via Responses and jev-1.14.0 via Decisions. ChatGPT/Gemini product integrations are not claimed as verified.

Web authentication:
- GET /api/auth/me is public and returns user summary, csrf_token, can_create_course, google_configured, mcp_oauth_ready=true.
- GET /auth/google and /auth/google/callback implement Google OIDC through Authlib (state/nonce/PKCE and ID-token validation).
- POST /auth/logout revokes the hashed server-side session and clears the HttpOnly cookie.
- All other /api/* endpoints require login. Mutations require X-CSRF-Token. Course operations enforce course membership; only GET materials and POST ask are available to students. All management and analytics require the course teacher who is also in CLASSROOM_TEACHER_EMAILS.
- GET /api/courses returns only memberships, with role per course. POST assigns its creator as teacher.
- POST /api/courses/{id}/invite rotates a seven-day link; DELETE revokes it.
- POST /api/join {token} enrolls the authenticated user as student without changing an existing teacher role.
- GET /api/courses/{id}/members lists participants for the teacher. DELETE /api/courses/{id}/members/{user_id} removes a student and revokes existing invitations.
- Pre-auth courses are assigned to the first allowlisted teacher who logs in. Unknown Google users are students with no course access.
- HTTP MCP requires a bearer token independent of browser cookies; stdio exits without exposing tools.
- No production OAuth callback or Google account login is claimed verified before real client credentials and user interaction are supplied. Local permission tests use synthetic identities exclusively through internal test helpers.

MCP OAuth authorization:
- GET /.well-known/oauth-authorization-server advertises /authorize, /token, /register, /revoke and PKCE S256.
- GET /.well-known/oauth-protected-resource/mcp/{student|instructor} describes each resource and its required classroom:student or classroom:instructor scope.
- Anonymous MCP requests return 401 with WWW-Authenticate resource_metadata. Valid tokens with missing scope return 403. Tokens for another resource return 401.
- POST /register supports DCR with none, client_secret_post and client_secret_basic. HTTPS callbacks or HTTP loopback callbacks only; no fragments, credentials or wildcards. Client secrets are encrypted with a key derived from the server signing secret.
- GET/POST /authorize requires an exact supported resource and PKCE S256, then redirects to a ten-minute opaque /oauth/consent request. Anonymous browsers sign into Google and return to the same consent request.
- POST /oauth/consent requires a web session and CSRF form token. Approval creates a 120-second one-use code bound to user, client, redirect, scopes, resource and PKCE challenge. Denial returns access_denied with the original client state.
- POST /token requires the same resource on code exchange and refresh. Code redemption is atomic. Access tokens last one hour; refresh grants last at most 30 days, rotate on use, and reject scope expansion. Reusing a refresh token revokes the grant family.
- POST /revoke and DELETE /api/oauth/connections/{grant_id} revoke the whole token family. GET /api/oauth/connections lists only the signed-in user's connections; these API routes inherit session and CSRF controls.
- Access tokens, refresh tokens, authorization codes and consent tickets are stored as hashes. OAuth state and user/course data survive service restarts in SQLite. Tokens are never exposed in /api/config or the connections UI.
- The official MCP SDK handles core client/redirect/PKCE validation and bearer auth. Compatibility wrappers enforce RFC 8707 resource binding, advertise public clients, and normalize Basic client_id / nullable revocation fields for SDK 1.30.
- Bearer auth runs in the full SDK ASGI app; route extraction must not drop that middleware. Instructor membership and current teacher allowlist, or student course membership, are checked again inside every tool/prompt.

Browser origin policy:
- Same-origin checks apply to cookie-authenticated /api/* mutations and /auth/logout. /oauth/consent additionally rejects explicit foreign origins, while Origin:null is accepted only through the existing authenticated-session plus CSRF form-token check.
- /authorize, /register, /token and /revoke use OAuth validation instead of a web-page origin match. MCP endpoints use bearer tokens independently of web cookies.
- MCP SDK Host/DNS-rebinding checks remain enabled. Exact app, https://chatgpt.com and https://chat.openai.com origins are supported. Optional CLASSROOM_MCP_ALLOWED_ORIGINS adds exact browser origins, without wildcard matching.
- MCP CORS preflight and auth response headers are available for these origins; allow_credentials is false. Unknown origins do not receive CORS permission and authenticated MCP requests from them are rejected by SDK origin validation.

Consent redirect browser policy:
- The consent page CSP form-action permits self plus only the validated origin of the registered callback. Keeping self alone blocks external OAuth callback redirects in Chromium/WebKit even after the server issues the code.
- Callback origins are restricted to URL host/port characters before becoming CSP sources; path/query and directive delimiters are never interpolated into CSP.
- consent.js disables repeat submission while preserving the clicked allow/deny decision in form data. Authorization codes remain one-use; the consent TTL is unchanged.
- Browser verification uses an isolated local fixture: old self-only CSP blocks the first-click redirect, whereas the corrected policy reaches a distinct callback origin and exchanges the code on one click. HTTP client tests alone do not evaluate CSP.
