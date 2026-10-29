"""Persistent MCP OAuth provider; Google establishes identity, our server issues tokens."""

import base64
from contextvars import ContextVar
import hashlib
from html import escape
import json
import re
import secrets
import time
from urllib.parse import unquote, urlencode, urlparse

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    RefreshToken, RegistrationError, TokenError, construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes, cors_middleware, build_metadata
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.routing import request_response
from starlette.datastructures import FormData
from starlette.middleware.cors import CORSMiddleware
from mcp.server.transport_security import RequestBodyLimitMiddleware

from .auth import digest

SCOPES = {'student': 'classroom:student', 'instructor': 'classroom:instructor'}
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
requested_resource = ContextVar('oauth_requested_resource', default=None)


def callback_origin(uri) -> str:
    """A CSP source for an already registered callback, without CSP delimiters."""
    parsed = urlparse(str(uri))
    if parsed.scheme not in {'http', 'https'} or not re.fullmatch(r'[A-Za-z0-9.\-:\[\]]+', parsed.netloc):
        raise ValueError('Invalid callback origin')
    return parsed.scheme + '://' + parsed.netloc


class FormRequest:
    """Normalize SDK 1.30's body-only client_id and required nullable secret fields."""
    def __init__(self, request, form):
        self.request, self.values = request, form

    def __getattr__(self, name):
        return getattr(self.request, name)

    async def form(self):
        return self.values


async def normalized_client_request(request):
    form = await request.form()
    values = list(form.multi_items())
    header = request.headers.get('authorization', '')
    if not form.get('client_id') and header.startswith('Basic '):
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
            client_id, _ = decoded.split(':', 1)
            values.append(('client_id', unquote(client_id)))
        except (ValueError, UnicodeDecodeError):
            pass
    if 'client_secret' not in form:
        values.append(('client_secret', None))
    return FormRequest(request, FormData(values))


class BoundAuthorizationHandler(AuthorizationHandler):
    async def handle(self, request):
        params = request.query_params if request.method == 'GET' else await request.form()
        if len(params.getlist('resource')) != 1 or params.get('resource') not in self.provider.resources:
            return JSONResponse({'error':'invalid_target'}, status_code=400, headers={'Cache-Control':'no-store'})
        return await super().handle(request)


class BoundTokenHandler(TokenHandler):
    async def handle(self, request):
        request = await normalized_client_request(request)
        form = await request.form()
        if len(form.getlist('resource')) != 1:
            return JSONResponse({'error': 'invalid_target'}, status_code=400, headers={'Cache-Control': 'no-store'})
        verifier = form.get('code_verifier', '')
        if form.get('grant_type') == 'authorization_code' and (not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier)):
            return JSONResponse({'error': 'invalid_grant'}, status_code=400, headers={'Cache-Control': 'no-store'})
        if form.get('resource') not in self.provider.resources:
            return JSONResponse({'error':'invalid_target'}, status_code=400, headers={'Cache-Control':'no-store'})
        try:
            client = await self.client_authenticator.authenticate_request(request)
        except AuthenticationError:
            return await super().handle(request)
        original = None
        if form.get('grant_type') == 'authorization_code':
            original = await self.provider.load_authorization_code(client, str(form.get('code', '')))
        elif form.get('grant_type') == 'refresh_token':
            original = await self.provider.load_refresh_token(client, str(form.get('refresh_token', '')))
        if original and original.resource != form.get('resource'):
            return JSONResponse({'error':'invalid_target'}, status_code=400, headers={'Cache-Control':'no-store'})
        mark = requested_resource.set(form.get('resource'))
        try:
            return await super().handle(request)
        finally:
            requested_resource.reset(mark)


class MCPOAuth:
    def __init__(self, auth):
        self.auth, self.service = auth, auth.service
        self.resources = {auth.base_url + '/mcp/' + role: scope for role, scope in SCOPES.items()}
        key = base64.urlsafe_b64encode(hashlib.sha256(auth.signing_secret.encode()).digest())
        self.cipher = Fernet(key)
        with self.service._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY, encrypted_metadata BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_pending (
                    ticket_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL,
                    params TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_grants (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), client_id TEXT NOT NULL,
                    resource TEXT NOT NULL, scopes TEXT NOT NULL, created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_access (
                    token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES oauth_grants(id), expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh (
                    token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES oauth_grants(id),
                    scopes TEXT NOT NULL, expires_at INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
            ''')

    async def get_client(self, client_id):
        with self.service._db() as db:
            row = db.execute('SELECT encrypted_metadata FROM oauth_clients WHERE client_id=?', (client_id,)).fetchone()
        if not row:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(self.cipher.decrypt(row[0]))
        except (InvalidToken, ValueError):
            return None

    async def register_client(self, client_info):
        if not client_info.redirect_uris or len(client_info.redirect_uris) > 10:
            raise RegistrationError('invalid_redirect_uri', 'Register between one and ten redirect URIs.')
        for uri in client_info.redirect_uris:
            try:
                callback_origin(uri)
            except ValueError:
                raise RegistrationError('invalid_redirect_uri', 'Invalid callback origin.') from None
            parsed = urlparse(str(uri))
            if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1', '::1'})) or parsed.fragment or parsed.username or parsed.password or '*' in str(uri):
                raise RegistrationError('invalid_redirect_uri', 'Use HTTPS or an HTTP loopback callback, without fragments or credentials.')
        if client_info.token_endpoint_auth_method not in {'none', 'client_secret_post', 'client_secret_basic'}:
            raise RegistrationError('invalid_client_metadata', 'Unsupported client authentication method.')
        if len(client_info.model_dump_json().encode()) > 16384:
            raise RegistrationError('invalid_client_metadata', 'Client metadata is too large.')
        with self.service._db() as db:
            if db.execute('SELECT COUNT(*) FROM oauth_clients').fetchone()[0] >= 10000:
                raise RegistrationError('invalid_client_metadata', 'Client registration capacity reached.')
            db.execute('INSERT INTO oauth_clients VALUES(?,?)', (client_info.client_id, self.cipher.encrypt(client_info.model_dump_json().encode())))

    async def authorize(self, client, params):
        expected = self.resources.get(params.resource)
        if not expected:
            raise AuthorizeError('invalid_request', 'An exact Classroom MCP resource URI is required.')
        if expected not in (params.scopes or []):
            raise AuthorizeError('invalid_scope', 'Request the scope advertised by this MCP resource.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.code_challenge):
            raise AuthorizeError('invalid_request', 'An S256 PKCE challenge is required.')
        params.scopes = [expected]
        ticket = secrets.token_urlsafe(32)
        with self.service._db() as db:
            db.execute('DELETE FROM oauth_pending WHERE expires_at<=?', (int(time.time()),))
            db.execute('DELETE FROM oauth_codes WHERE expires_at<=?', (int(time.time()),))
            db.execute('INSERT INTO oauth_pending VALUES(?,?,?,?)', (digest(ticket), client.client_id, params.model_dump_json(), int(time.time())+600))
        return self.auth.base_url + '/oauth/consent?' + urlencode({'request': ticket})

    def pending(self, ticket):
        with self.service._db() as db:
            row = db.execute('SELECT * FROM oauth_pending WHERE ticket_hash=? AND expires_at>?', (digest(ticket), int(time.time()))).fetchone()
        if not row:
            raise HTTPException(400, '연결 요청이 만료되었거나 이미 처리되었습니다. MCP 클라이언트에서 다시 연결해 주세요.')
        return dict(row), AuthorizationParams.model_validate_json(row['params'])

    def check_user_scope(self, user, scopes):
        if SCOPES['instructor'] in scopes and user['email'] not in self.auth.teacher_emails:
            raise HTTPException(403, '교수용 MCP 연결은 등록된 교수 계정만 승인할 수 있습니다.')

    async def load_authorization_code(self, client, authorization_code):
        with self.service._db() as db:
            row = db.execute('SELECT payload FROM oauth_codes WHERE code_hash=? AND expires_at>?', (digest(authorization_code), int(time.time()))).fetchone()
        if not row:
            return None
        result = AuthorizationCode.model_validate_json(row[0])
        if result.client_id != client.client_id:
            return None
        return result.model_copy(update={'code': authorization_code})

    def _mint(self, db, grant_id, scopes, grant_expiry):
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        now = int(time.time())
        db.execute('INSERT INTO oauth_access VALUES(?,?,?)', (digest(access), grant_id, now+ACCESS_TTL))
        db.execute('INSERT INTO oauth_refresh VALUES(?,?,?,?,0)', (digest(refresh), grant_id, json.dumps(scopes), grant_expiry))
        return OAuthToken(access_token=access, token_type='Bearer', expires_in=ACCESS_TTL, refresh_token=refresh, scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        if requested_resource.get() != authorization_code.resource:
            raise TokenError('invalid_request', 'The resource must match the authorization request.')
        now = int(time.time())
        with self.service._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT user_id FROM oauth_codes WHERE code_hash=? AND expires_at>?', (digest(authorization_code.code), now)).fetchone()
            if not row:
                raise TokenError('invalid_grant', 'Authorization code was already used or expired.')
            db.execute('DELETE FROM oauth_codes WHERE code_hash=?', (digest(authorization_code.code),))
            grant_id = secrets.token_urlsafe(24)
            db.execute('INSERT INTO oauth_grants VALUES(?,?,?,?,?,?,?,0)', (grant_id, row[0], client.client_id, authorization_code.resource, json.dumps(authorization_code.scopes), now, now+REFRESH_TTL))
            return self._mint(db, grant_id, authorization_code.scopes, now+REFRESH_TTL)

    async def load_refresh_token(self, client, refresh_token):
        with self.service._db() as db:
            row = db.execute('SELECT r.*,g.client_id,g.resource,g.user_id FROM oauth_refresh r JOIN oauth_grants g ON g.id=r.grant_id WHERE r.token_hash=? AND g.revoked=0 AND r.expires_at>?', (digest(refresh_token), int(time.time()))).fetchone()
        if not row or row['client_id'] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row['client_id'], scopes=json.loads(row['scopes']), expires_at=row['expires_at'], resource=row['resource'], subject=str(row['user_id']))

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if requested_resource.get() != refresh_token.resource:
            raise TokenError('invalid_request', 'The resource must match the original grant.')
        replay = False
        with self.service._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT r.*,g.revoked,g.expires_at AS grant_expiry FROM oauth_refresh r JOIN oauth_grants g ON g.id=r.grant_id WHERE r.token_hash=?', (digest(refresh_token.token),)).fetchone()
            if not row or row['revoked'] or row['expires_at'] <= int(time.time()):
                raise TokenError('invalid_grant', 'Refresh token expired or was revoked.')
            if row['used']:
                db.execute('UPDATE oauth_grants SET revoked=1 WHERE id=?', (row['grant_id'],))
                replay = True
            else:
                db.execute('UPDATE oauth_refresh SET used=1 WHERE token_hash=?', (digest(refresh_token.token),))
                db.execute('DELETE FROM oauth_access WHERE grant_id=?', (row['grant_id'],))
                db.execute('UPDATE oauth_grants SET scopes=? WHERE id=?', (json.dumps(scopes), row['grant_id']))
                result = self._mint(db, row['grant_id'], scopes, row['grant_expiry'])
        if replay:
            raise TokenError('invalid_grant', 'Refresh token replay detected; reconnect the client.')
        return result

    async def load_access_token(self, token):
        with self.service._db() as db:
            row = db.execute('SELECT a.expires_at,g.client_id,g.scopes,g.resource,g.user_id,u.email FROM oauth_access a JOIN oauth_grants g ON g.id=a.grant_id JOIN users u ON u.id=g.user_id WHERE a.token_hash=? AND a.expires_at>? AND g.expires_at>? AND g.revoked=0', (digest(token), int(time.time()), int(time.time()))).fetchone()
        if not row:
            return None
        scopes = json.loads(row['scopes'])
        if SCOPES['instructor'] in scopes and row['email'] not in self.auth.teacher_emails:
            return None
        return AccessToken(token=token, client_id=row['client_id'], scopes=scopes, expires_at=row['expires_at'], resource=row['resource'], subject=str(row['user_id']), claims={'iss':self.auth.base_url})

    async def revoke_token(self, token):
        table = 'oauth_refresh' if isinstance(token, RefreshToken) else 'oauth_access'
        with self.service._db() as db:
            db.execute(f'UPDATE oauth_grants SET revoked=1 WHERE id IN (SELECT grant_id FROM {table} WHERE token_hash=?)', (digest(token.token),))

    def user_from_token(self, token):
        if not token or not token.subject:
            raise HTTPException(401, 'MCP 인증이 필요합니다.')
        with self.service._db() as db:
            row = db.execute('SELECT id,email,name FROM users WHERE id=?', (token.subject,)).fetchone()
        if not row:
            raise HTTPException(401, '계정을 찾을 수 없습니다.')
        return dict(row)

    def install(self, app):
        oauth = self
        options = ClientRegistrationOptions(enabled=True, valid_scopes=list(SCOPES.values()), default_scopes=list(SCOPES.values()))
        routes = create_auth_routes(self, AnyHttpUrl(self.auth.base_url), client_registration_options=options, revocation_options=RevocationOptions(enabled=True))
        # Keep the SDK's PKCE/client/redirect validation and add resource binding
        # on token exchange, which SDK 1.30 does not enforce itself.
        handler = BoundTokenHandler(self, ClientAuthenticator(self))
        metadata = build_metadata(AnyHttpUrl(self.auth.base_url), None, options, RevocationOptions(enabled=True))
        metadata.token_endpoint_auth_methods_supported = ['none', 'client_secret_post', 'client_secret_basic']
        metadata.revocation_endpoint_auth_methods_supported = ['none', 'client_secret_post', 'client_secret_basic']

        async def discovery(request):
            return JSONResponse(metadata.model_dump(mode='json', exclude_none=True))

        async def revoke(request):
            return await RevocationHandler(self, ClientAuthenticator(self)).handle(await normalized_client_request(request))

        for route in routes:
            if route.path == '/token':
                route.app = CORSMiddleware(RequestBodyLimitMiddleware(request_response(handler.handle), 65536), allow_origins=['*'], allow_methods=['POST', 'OPTIONS'], allow_headers=['Authorization', 'Content-Type', 'MCP-Protocol-Version'])
            elif route.path == '/revoke':
                route.app = CORSMiddleware(RequestBodyLimitMiddleware(request_response(revoke), 65536), allow_origins=['*'], allow_methods=['POST', 'OPTIONS'], allow_headers=['Authorization', 'Content-Type'])
            elif route.path == '/authorize':
                route.app = RequestBodyLimitMiddleware(request_response(BoundAuthorizationHandler(self).handle), 65536)
            elif route.path == '/.well-known/oauth-authorization-server':
                route.app = cors_middleware(discovery, ['GET', 'OPTIONS'])
        app.router.routes.extend(routes)

        @app.get('/oauth/consent')
        async def consent(request: Request):
            ticket = request.query_params.get('request', '')
            row, params = oauth.pending(ticket)
            user = request.state.user
            if not user:
                target = '/oauth/consent?' + urlencode({'request':ticket})
                return RedirectResponse('/auth/google?' + urlencode({'next':target}), status_code=303)
            oauth.check_user_scope(user, params.scopes)
            client = await oauth.get_client(row['client_id'])
            if not client:
                raise HTTPException(400, '클라이언트를 찾을 수 없습니다.')
            role = '교수' if SCOPES['instructor'] in params.scopes else '학생'
            body = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MCP 연결 승인 · 수업 사이</title><link rel="stylesheet" href="/static/style.css"><script defer src="/static/consent.js"></script></head><body><main class="login-layout"><article class="panel login-card"><div class="eyebrow">MCP CONNECTION</div><h1>AI 연결 승인</h1><p><strong>{escape(client.client_name or 'MCP 클라이언트')}</strong>가 {escape(user['email'])} 계정의 {role} 도구 사용을 요청합니다.</p><p>현재 계정에 허용된 수업의 {'자료 등록·질문 통계·수업 준비 도구' if role == '교수' else '자료 검색·질문 기록 도구'}를 사용할 수 있습니다. 각 호출에서 수업 권한을 다시 확인합니다.</p><small>클라이언트 등록 정보는 자체 신고 정보입니다.<br>돌아갈 주소: {escape(str(params.redirect_uri))}</small><form id="oauth-consent" method="post" action="/oauth/consent"><input type="hidden" name="request" value="{escape(ticket, quote=True)}"><input type="hidden" name="csrf" value="{escape(user['csrf'], quote=True)}"><button class="button button-dark" name="decision" value="allow">연결 허용</button><button class="button" name="decision" value="deny">거절</button></form><small>연결은 워크스페이스의 AI 연결 관리에서 해제할 수 있습니다.</small></article></main></body></html>'''
            return HTMLResponse(body, headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':f"default-src 'self'; form-action 'self' {callback_origin(params.redirect_uri)}; frame-ancestors 'none'; base-uri 'none'"})

        @app.post('/oauth/consent')
        async def approve(request: Request):
            user = request.state.user
            if not user:
                raise HTTPException(401, 'Google 로그인이 필요합니다.')
            form = await request.form()
            if not secrets.compare_digest(str(form.get('csrf', '')), user['csrf']):
                raise HTTPException(403, '승인 화면을 새로 열어 주세요.')
            ticket = str(form.get('request', ''))
            row, params = oauth.pending(ticket)
            oauth.check_user_scope(user, params.scopes)
            decision = form.get('decision')
            if decision not in {'allow','deny'}:
                raise HTTPException(400, '승인 여부를 선택해 주세요.')
            code = secrets.token_urlsafe(48)
            payload = AuthorizationCode(code='', scopes=params.scopes, expires_at=time.time()+120, client_id=row['client_id'], code_challenge=params.code_challenge, redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource, subject=str(user['id']))
            with oauth.service._db() as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('DELETE FROM oauth_pending WHERE ticket_hash=? AND expires_at>?', (digest(ticket), int(time.time()))).rowcount != 1:
                    raise HTTPException(400, '이미 처리되었거나 만료된 요청입니다.')
                if decision == 'allow':
                    db.execute('INSERT INTO oauth_codes VALUES(?,?,?,?)', (digest(code), payload.model_dump_json(), user['id'], int(payload.expires_at)))
            fields = {'state': params.state}
            fields.update({'code':code} if decision == 'allow' else {'error':'access_denied'})
            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), **{k:v for k,v in fields.items() if v is not None}), status_code=303, headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

        @app.get('/api/oauth/connections')
        async def connections(request: Request):
            with oauth.service._db() as db:
                rows = db.execute('SELECT id,client_id,resource,created_at,expires_at FROM oauth_grants WHERE user_id=? AND revoked=0 AND expires_at>? ORDER BY created_at DESC', (request.state.user['id'], int(time.time()))).fetchall()
            result = []
            for row in rows:
                client = await oauth.get_client(row['client_id'])
                result.append({**dict(row), 'client_name':client.client_name if client else 'MCP 클라이언트'})
            return {'connections':result}

        @app.delete('/api/oauth/connections/{grant_id}')
        def disconnect(grant_id: str, request: Request):
            with oauth.service._db() as db:
                db.execute('UPDATE oauth_grants SET revoked=1 WHERE id=? AND user_id=?', (grant_id, request.state.user['id']))
            return {'revoked':True}
