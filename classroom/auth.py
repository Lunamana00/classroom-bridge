"""Google OIDC login, revocable sessions, and course membership authorization."""

import hashlib
import os
import re
import secrets
import time
from urllib.parse import urlparse

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import ROOT, mcp_allowed_origins

COOKIE = "classroom_session"
TTL = 7 * 24 * 3600


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Auth:
    def __init__(self, service):
        self.service = service
        self.base_url = os.environ.get("CLASSROOM_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("CLASSROOM_BASE_URL에는 경로 없는 서버 주소를 설정해 주세요.")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("외부 서버의 CLASSROOM_BASE_URL은 HTTPS여야 합니다.")
        self.hostname = parsed.hostname
        self.mcp_origins = mcp_allowed_origins(self.base_url)
        self.secure = parsed.scheme == "https"
        self.teacher_emails = {v.strip().lower() for v in os.environ.get("CLASSROOM_TEACHER_EMAILS", "").split(",") if v.strip()}
        self.ready = bool(os.environ.get("GOOGLE_CLIENT_ID", "").strip() and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip())
        self.oauth = OAuth()
        self.oauth.register(
            "google", client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
            client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
        )
        with service._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, google_sub TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL, name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL, csrf TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS course_members (
                    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('teacher','student')),
                    PRIMARY KEY(course_id,user_id)
                );
                CREATE TABLE IF NOT EXISTS course_invites (
                    token_hash TEXT PRIMARY KEY, course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
            """)

    def login_user(self, claims):
        # Authlib validates issuer, audience, signature, state and nonce before here.
        sub, email = claims.get("sub"), claims.get("email")
        if not isinstance(sub, str) or not sub or not isinstance(email, str) or claims.get("email_verified") is not True:
            raise HTTPException(403, "이메일이 인증된 Google 계정이 필요합니다.")
        email = email.lower()
        name = str(claims.get("name") or email)[:200]
        with self.service._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO users(google_sub,email,name) VALUES(?,?,?) ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email,name=excluded.name", (sub, email, name))
            user = dict(db.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone())
            # One-time migration of pre-login courses to the first configured teacher.
            if email in self.teacher_emails:
                db.execute("INSERT INTO course_members(course_id,user_id,role) SELECT id,?,'teacher' FROM courses WHERE NOT EXISTS(SELECT 1 FROM course_members WHERE course_members.course_id=courses.id AND role='teacher')", (user["id"],))
        return user

    def new_session(self, user_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.service._db() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (int(time.time()),))
            db.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (digest(token), user_id, int(time.time()) + TTL, csrf))
        return token, csrf

    def user(self, request):
        token = request.cookies.get(COOKIE, "")
        if not token:
            return None
        with self.service._db() as db:
            row = db.execute("SELECT u.id,u.email,u.name,s.csrf FROM users u JOIN auth_sessions s ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (digest(token), int(time.time()))).fetchone()
        return dict(row) if row else None

    def role(self, user, course_id):
        with self.service._db() as db:
            row = db.execute("SELECT role FROM course_members WHERE course_id=? AND user_id=?", (course_id, user["id"])).fetchone()
        return row[0] if row else None

    def require_course(self, user, course_id, teacher=False):
        role = self.role(user, course_id)
        if not role or (teacher and (role != "teacher" or user["email"] not in self.teacher_emails)):
            raise HTTPException(403, "이 수업에 접근할 권한이 없습니다.")
        return role

    def courses(self, user):
        with self.service._db() as db:
            rows = db.execute("SELECT c.*,m.role FROM courses c JOIN course_members m ON c.id=m.course_id WHERE m.user_id=? ORDER BY c.id", (user["id"],)).fetchall()
        return [{**dict(row), "is_demo": bool(row["is_demo"])} for row in rows]

    def install(self, app):
        auth = self
        # Only OAuth state/nonce is stored in this signed, short-lived cookie.
        # Login sessions themselves are opaque, hashed in SQLite and revocable.
        secret = os.environ.get("CLASSROOM_SESSION_SECRET", "").strip()
        if not secret:
            with self.service._db() as db:
                db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES('oauth_cookie_secret',?)", (secrets.token_urlsafe(48),))
                secret = db.execute("SELECT value FROM metadata WHERE key='oauth_cookie_secret'").fetchone()[0]
        self.signing_secret = secret
        app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="classroom_oauth", max_age=600, same_site="lax", https_only=self.secure)

        @app.middleware("http")
        async def access_control(request: Request, call_next):
            path = request.url.path
            user = auth.user(request)
            request.state.user = user
            if path == "/" and not user:
                return FileResponse(ROOT / "classroom/static/login.html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            if path.startswith("/api/") and path != "/api/auth/me":
                if not user:
                    return JSONResponse({"detail": "Google 로그인이 필요합니다."}, status_code=401)
            if (path.startswith("/api/") or path == "/auth/logout") and request.method not in {"GET", "HEAD", "OPTIONS"}:
                if not user or not secrets.compare_digest(request.headers.get("x-csrf-token", ""), user["csrf"]):
                    return JSONResponse({"detail": "로그인 상태를 새로 확인해 주세요."}, status_code=403)
            response = await call_next(request)
            if path.startswith(("/api/", "/auth/", "/oauth/")) or path in {"/", "/authorize", "/token", "/register", "/revoke"}:
                response.headers["Cache-Control"] = "no-store"
                response.headers["Referrer-Policy"] = "no-referrer"
            return response

        @app.get("/api/auth/me")
        def me(request: Request):
            user = request.state.user
            return {"user": {k: user[k] for k in ("id", "email", "name")} if user else None,
                    "csrf_token": user["csrf"] if user else None,
                    "can_create_course": bool(user and user["email"] in auth.teacher_emails),
                    "google_configured": auth.ready, "mcp_oauth_ready": hasattr(app.state, "mcp_oauth")}

        @app.get("/auth/google")
        async def login(request: Request):
            if not auth.ready:
                raise HTTPException(503, "Google 로그인 설정을 준비 중입니다.")
            if str(request.base_url).rstrip("/") != auth.base_url:
                return RedirectResponse(auth.base_url + "/auth/google", status_code=303)
            target = request.query_params.get("next", "")
            if re.fullmatch(r"/oauth/consent\?request=[A-Za-z0-9_-]{43}", target):
                request.session["login_next"] = target
            else:
                request.session.pop("login_next", None)
            return await auth.oauth.google.authorize_redirect(request, auth.base_url + "/auth/google/callback", prompt="select_account")

        @app.get("/auth/google/callback")
        async def callback(request: Request):
            try:
                token = await auth.oauth.google.authorize_access_token(request)
                user = auth.login_user(token.get("userinfo") or {})
            except HTTPException:
                raise
            except Exception:
                # Provider errors can contain authorization codes and tokens.
                request.session.clear()
                return RedirectResponse("/?login_error=1", status_code=303)
            with auth.service._db() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(request.cookies.get(COOKIE, "")),))
            session, _ = auth.new_session(user["id"])
            target = request.session.get("login_next", "")
            target = target if isinstance(target, str) and re.fullmatch(r"/oauth/consent\?request=[A-Za-z0-9_-]{43}", target) else "/"
            request.session.clear()
            response = RedirectResponse(target, status_code=303)
            response.set_cookie(COOKIE, session, httponly=True, secure=auth.secure, samesite="lax", max_age=TTL, path="/")
            return response

        @app.post("/auth/logout")
        def logout(request: Request):
            with auth.service._db() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (digest(request.cookies.get(COOKIE, "")),))
            response = JSONResponse({"logged_out": True})
            response.delete_cookie(COOKIE, path="/")
            return response

        @app.post("/api/courses/{course_id}/invite")
        def invite(course_id: int, request: Request):
            auth.require_course(request.state.user, course_id, teacher=True)
            token = secrets.token_urlsafe(32)
            with auth.service._db() as db:
                db.execute("DELETE FROM course_invites WHERE course_id=?", (course_id,))
                db.execute("INSERT INTO course_invites VALUES(?,?,?)", (digest(token), course_id, int(time.time()) + TTL))
            return {"url": auth.base_url + "/#join=" + token, "expires_in": TTL}

        @app.delete("/api/courses/{course_id}/invite")
        def revoke_invite(course_id: int, request: Request):
            auth.require_course(request.state.user, course_id, teacher=True)
            with auth.service._db() as db:
                db.execute("DELETE FROM course_invites WHERE course_id=?", (course_id,))
            return {"revoked": True}

        @app.post("/api/join")
        async def join(request: Request):
            data = await request.json()
            token = data.get("token", "") if isinstance(data, dict) else ""
            if not isinstance(token, str) or len(token) > 256:
                raise HTTPException(400, "초대 링크를 확인해 주세요.")
            with auth.service._db() as db:
                db.execute("BEGIN IMMEDIATE")
                invite = db.execute("SELECT course_id FROM course_invites WHERE token_hash=? AND expires_at>?", (digest(token), int(time.time()))).fetchone()
                if not invite:
                    raise HTTPException(400, "만료되었거나 폐기된 초대 링크입니다.")
                db.execute("INSERT OR IGNORE INTO course_members VALUES(?,?,'student')", (invite[0], request.state.user["id"]))
            return {"course_id": invite[0]}

        @app.get("/api/courses/{course_id}/members")
        def members(course_id: int, request: Request):
            auth.require_course(request.state.user, course_id, teacher=True)
            with auth.service._db() as db:
                rows = db.execute("SELECT u.id,u.name,u.email,m.role FROM users u JOIN course_members m ON u.id=m.user_id WHERE m.course_id=? ORDER BY u.id", (course_id,)).fetchall()
            return {"members": [dict(row) for row in rows]}

        @app.delete("/api/courses/{course_id}/members/{user_id}")
        def remove_member(course_id: int, user_id: int, request: Request):
            auth.require_course(request.state.user, course_id, teacher=True)
            with auth.service._db() as db:
                removed = db.execute("DELETE FROM course_members WHERE course_id=? AND user_id=? AND role='student'", (course_id, user_id)).rowcount
                if removed:
                    db.execute("DELETE FROM course_invites WHERE course_id=?", (course_id,))
            return {"removed": True}
