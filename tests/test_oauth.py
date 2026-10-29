import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from conftest import authenticate
from classroom.app import create_app
from classroom.auth import digest

BASE = 'http://127.0.0.1:8000'
REDIRECT = 'http://127.0.0.1:39877/callback'
VERIFIER = 'test-verifier-' + 'x' * 48
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('CLASSROOM_MODE', 'demo')
    with TestClient(create_app(str(tmp_path / 'oauth.sqlite')), base_url=BASE) as client:
        yield client


def register(client, **overrides):
    payload = {'client_name':'Test MCP', 'redirect_uris':[REDIRECT], 'token_endpoint_auth_method':'none', 'grant_types':['authorization_code','refresh_token'], 'response_types':['code'], 'scope':'classroom:student classroom:instructor'}
    payload.update(overrides)
    return client.post('/register', json=payload)


def begin(client, client_id, role='student', **overrides):
    params = {'client_id':client_id, 'redirect_uri':REDIRECT, 'response_type':'code', 'code_challenge':CHALLENGE, 'code_challenge_method':'S256', 'resource':BASE+'/mcp/'+role, 'scope':'classroom:'+role, 'state':'client-state'}
    params.update(overrides)
    return client.get('/authorize', params=params, follow_redirects=False)


def authorize(client, identity='teacher@example.test', role='student', registration=None):
    identity_row = authenticate(client, identity)
    registration = registration or register(client).json()
    response = begin(client, registration['client_id'], role)
    assert response.status_code == 302, response.text
    consent = client.get(response.headers['location'])
    assert consent.status_code == 200, consent.text
    ticket = parse_qs(urlparse(response.headers['location']).query)['request'][0]
    csrf = client.get('/api/auth/me').json()['csrf_token']
    response = client.post('/oauth/consent', data={'request':ticket,'csrf':csrf,'decision':'allow'}, follow_redirects=False)
    assert response.status_code == 303, response.text
    query = parse_qs(urlparse(response.headers['location']).query)
    assert query['state'] == ['client-state']
    return registration, query['code'][0], identity_row


def exchange(client, registration, code, role='student', **overrides):
    form = {'grant_type':'authorization_code', 'client_id':registration['client_id'], 'code':code, 'redirect_uri':REDIRECT, 'code_verifier':VERIFIER, 'resource':BASE+'/mcp/'+role}
    if registration.get('client_secret'):
        form['client_secret'] = registration['client_secret']
    form.update(overrides)
    return client.post('/token', data=form)


def rpc(client, token, method, params=None, role='student', request_id=1):
    body = {'jsonrpc':'2.0','id':request_id,'method':method}
    if params is not None:
        body['params'] = params
    return client.post('/mcp/'+role, json=body, headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'})


def test_discovery_and_fail_closed(client):
    assert client.get('/api/auth/me').json()['mcp_oauth_ready'] is True
    for role in ('student','instructor'):
        response = client.post('/mcp/'+role, json={})
        assert response.status_code == 401
        assert '/.well-known/oauth-protected-resource/mcp/'+role in response.headers['www-authenticate']
        metadata = client.get('/.well-known/oauth-protected-resource/mcp/'+role).json()
        assert metadata['resource'] == BASE+'/mcp/'+role
        assert metadata['authorization_servers'] == [BASE+'/'] or metadata['authorization_servers'] == [BASE]
    meta = client.get('/.well-known/oauth-authorization-server').json()
    assert 'S256' in meta['code_challenge_methods_supported']
    assert 'none' in meta['token_endpoint_auth_methods_supported']
    assert meta['registration_endpoint'] == BASE+'/register'
    assert 'password' not in meta['grant_types_supported']
    authenticate(client)
    assert client.post('/mcp/instructor', json={}).status_code == 401
    assert rpc(client, 'forged', 'tools/list').status_code == 401


def test_authorization_resumes_google_login_and_rejects_bad_requests(client):
    reg = register(client).json()
    start = begin(client, reg['client_id'])
    consent = client.get(start.headers['location'], follow_redirects=False)
    assert consent.status_code == 303
    assert consent.headers['location'].startswith('/auth/google?next=')
    assert begin(client, reg['client_id'], code_challenge_method='plain').status_code in {302,400}
    bad = begin(client, reg['client_id'], redirect_uri='https://attacker.invalid/callback')
    assert bad.status_code == 400
    bad = begin(client, reg['client_id'], resource='https://attacker.invalid/mcp')
    assert bad.status_code == 400 and bad.json()['error'] == 'invalid_target'
    for uri in ('javascript:alert(1)', 'http://public.invalid/cb', 'https://user:password@example.com/cb', 'https://example.com/cb#fragment'):
        assert register(client, redirect_uris=[uri]).status_code == 400


def test_code_pkce_binding_replay_and_expiry(client):
    reg, code, _ = authorize(client)
    assert exchange(client, reg, code, code_verifier='z'*64).status_code == 400
    assert exchange(client, reg, code, role='instructor').json()['error'] == 'invalid_target'
    assert exchange(client, reg, code, resource='').status_code == 400
    assert exchange(client, reg, code, redirect_uri=REDIRECT+'bad').status_code == 400
    another = register(client).json()
    assert exchange(client, another, code).status_code == 400
    response = exchange(client, reg, code)
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert exchange(client, reg, code).status_code == 400
    token = response.json()['access_token']
    assert rpc(client, token, 'tools/list', role='instructor').status_code == 401
    with client.app.state.service._db() as db:
        db.execute('UPDATE oauth_access SET expires_at=0')
    assert rpc(client, token, 'tools/list').status_code == 401
    reg, code, _ = authorize(client)
    with client.app.state.service._db() as db:
        db.execute('UPDATE oauth_codes SET expires_at=0')
    assert exchange(client, reg, code).status_code == 400


def test_consent_csrf_denial_student_cannot_grant_teacher(client):
    authenticate(client, 'student@example.test')
    reg = register(client).json()
    start = begin(client, reg['client_id'], role='instructor')
    assert client.get(start.headers['location']).status_code == 403
    ticket = parse_qs(urlparse(start.headers['location']).query)['request'][0]
    csrf = client.get('/api/auth/me').json()['csrf_token']
    assert client.post('/oauth/consent', data={'request':ticket,'csrf':csrf,'decision':'allow'}).status_code == 403
    start = begin(client, reg['client_id'])
    ticket = parse_qs(urlparse(start.headers['location']).query)['request'][0]
    assert client.post('/oauth/consent', data={'request':ticket,'csrf':'forged','decision':'allow'}).status_code == 403
    denied = client.post('/oauth/consent', data={'request':ticket,'csrf':csrf,'decision':'deny'}, follow_redirects=False)
    assert 'access_denied' in denied.headers['location'] and 'client-state' in denied.headers['location']
    assert client.get(start.headers['location']).status_code == 400


def test_refresh_rotation_replay_and_disconnect(client):
    reg, code, _ = authorize(client)
    pair = exchange(client, reg, code).json()
    body = {'grant_type':'refresh_token','client_id':reg['client_id'],'refresh_token':pair['refresh_token'],'resource':BASE+'/mcp/student'}
    assert client.post('/token', data={**body,'scope':'classroom:instructor'}).status_code == 400
    assert client.post('/token', data={**body,'resource':BASE+'/mcp/instructor'}).status_code == 400
    refreshed = client.post('/token', data=body)
    assert refreshed.status_code == 200, refreshed.text
    new_pair = refreshed.json()
    assert new_pair['refresh_token'] != pair['refresh_token']
    assert rpc(client, pair['access_token'], 'tools/list').status_code == 401
    assert rpc(client, new_pair['access_token'], 'tools/list').status_code == 200
    assert client.post('/token', data=body).status_code == 400
    assert rpc(client, new_pair['access_token'], 'tools/list').status_code == 401
    reg, code, _ = authorize(client)
    pair = exchange(client, reg, code).json()
    connections = client.get('/api/oauth/connections').json()['connections']
    assert connections
    assert client.delete('/api/oauth/connections/'+connections[0]['id']).status_code == 200
    assert rpc(client, pair['access_token'], 'tools/list').status_code == 401


def test_real_mcp_tools_course_membership_and_revocation(client):
    teacher = authenticate(client)
    invite = client.post('/api/courses/1/invite').json()['url'].split('#join=')[1]
    authenticate(client, 'student@example.test')
    assert client.post('/api/join', json={'token':invite}).status_code == 200
    reg, code, student = authorize(client, 'student@example.test')
    token = exchange(client, reg, code).json()['access_token']
    initialize = rpc(client, token, 'initialize', {'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'Test','version':'1'}})
    assert initialize.status_code == 200, initialize.text
    tools = rpc(client, token, 'tools/list').json()['result']['tools']
    assert {t['name'] for t in tools} == {'list_courses','search_course_materials','submit_quiz_answer','get_my_quiz_progress'}
    courses = rpc(client, token, 'tools/call', {'name':'list_courses','arguments':{}}).json()['result']
    assert not courses.get('isError') and '머신러닝' in str(courses)
    result = rpc(client, token, 'tools/call', {'name':'search_course_materials','arguments':{'course_id':1,'question':'경사하강법이란?'}}).json()['result']
    assert not result.get('isError'), result
    denied = rpc(client, token, 'tools/call', {'name':'search_course_materials','arguments':{'course_id':999,'question':'x'}}).json()['result']
    assert denied['isError']
    authenticate(client)
    client.delete(f'/api/courses/1/members/{student["id"]}')
    denied = rpc(client, token, 'tools/call', {'name':'search_course_materials','arguments':{'course_id':1,'question':'x'}}).json()['result']
    assert denied['isError']
    reg, code, _ = authorize(client, role='instructor')
    token = exchange(client, reg, code, role='instructor').json()['access_token']
    result = rpc(client, token, 'tools/call', {'name':'get_question_insights','arguments':{'course_id':1}}, role='instructor').json()['result']
    assert not result.get('isError'), result
    with client.app.state.service._db() as db:
        db.execute("UPDATE users SET email='no-longer-teacher@example.test' WHERE id=?", (teacher['id'],))
    assert rpc(client, token, 'tools/list', role='instructor').status_code == 401


def test_client_secret_and_rfc7009_revocation(client):
    reg = register(client, token_endpoint_auth_method='client_secret_post').json()
    reg, code, _ = authorize(client, registration=reg)
    assert exchange(client, reg, code, client_secret='wrong').status_code == 401
    pair = exchange(client, reg, code).json()
    revoked = client.post('/revoke', data={'client_id':reg['client_id'],'client_secret':reg['client_secret'],'token':pair['refresh_token'],'token_type_hint':'refresh_token'})
    assert revoked.status_code == 200, revoked.text
    assert rpc(client, pair['access_token'], 'tools/list').status_code == 401
    with client.app.state.service._db() as db:
        persisted = str([tuple(row) for row in db.execute('SELECT * FROM oauth_clients')])
        assert reg['client_secret'] not in persisted
        persisted = str([tuple(row) for row in db.execute('SELECT * FROM oauth_refresh')])
        assert pair['refresh_token'] not in persisted


def test_google_callback_resumes_only_internal_consent(client, monkeypatch):
    from fastapi.responses import RedirectResponse
    auth = client.app.state.auth
    auth.ready = True

    async def google_start(request, redirect_uri, **kwargs):
        return RedirectResponse('https://accounts.google.com/test', status_code=302)

    async def validated_identity(request):
        return {'userinfo': {'sub':'google-test-sub','email':'teacher@example.test','email_verified':True,'name':'Teacher'}}

    monkeypatch.setattr(auth.oauth.google, 'authorize_redirect', google_start)
    monkeypatch.setattr(auth.oauth.google, 'authorize_access_token', validated_identity)
    reg = register(client).json()
    start = begin(client, reg['client_id'])
    next_url = urlparse(start.headers['location']).path+'?'+urlparse(start.headers['location']).query
    client.get('/auth/google', params={'next':next_url}, follow_redirects=False)
    callback = client.get('/auth/google/callback', follow_redirects=False)
    assert callback.headers['location'] == next_url
    assert client.get(callback.headers['location']).status_code == 200
    client.get('/auth/google', params={'next':'https://attacker.invalid/'}, follow_redirects=False)
    assert client.get('/auth/google/callback', follow_redirects=False).headers['location'] == '/'


def test_atomic_code_redemption(client):
    reg, code, _ = authorize(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(exchange, client, reg, code) for _ in range(2)]
        assert sorted(f.result().status_code for f in futures) == [200,400]


@pytest.mark.parametrize('role', ['student','instructor'])
async def test_official_sdk_oauth_and_mcp_round_trip(client, role):
    import httpx
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import OAuthClientMetadata

    authenticate(client)
    callback_data = []

    class Storage:
        tokens = None
        info = None
        async def get_tokens(self): return self.tokens
        async def set_tokens(self, value): self.tokens = value
        async def get_client_info(self): return self.info
        async def set_client_info(self, value): self.info = value

    async def browser_redirect(url):
        start = client.get(url, follow_redirects=False)
        assert start.status_code == 302, start.text
        consent = client.get(start.headers['location'])
        assert consent.status_code == 200, consent.text
        ticket = parse_qs(urlparse(start.headers['location']).query)['request'][0]
        csrf = client.get('/api/auth/me').json()['csrf_token']
        response = client.post('/oauth/consent', data={'request':ticket,'csrf':csrf,'decision':'allow'}, follow_redirects=False)
        values = parse_qs(urlparse(response.headers['location']).query)
        callback_data.append((values['code'][0], values['state'][0]))

    async def callback():
        return callback_data[-1]

    def bridge(request):
        assert str(request.url).startswith(BASE+'/')
        response = client.request(request.method, str(request.url), content=request.content, headers=dict(request.headers), follow_redirects=False)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)

    storage = Storage()
    provider = OAuthClientProvider(
        server_url=BASE+'/mcp/'+role,
        client_metadata=OAuthClientMetadata(client_name='Official SDK integration test', redirect_uris=[REDIRECT], token_endpoint_auth_method='none', grant_types=['authorization_code','refresh_token'], response_types=['code'], scope='classroom:'+role),
        storage=storage, redirect_handler=browser_redirect, callback_handler=callback,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(bridge), auth=provider) as http:
        async with streamable_http_client(BASE+'/mcp/'+role, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert ('get_question_insights' if role == 'instructor' else 'search_course_materials') in {tool.name for tool in tools.tools}
                result = await session.call_tool('list_courses', {})
                assert not result.isError and '머신러닝' in str(result)
    assert storage.tokens and storage.info and callback_data


def test_basic_auth_without_body_client_id_and_public_revoke(client):
    reg = register(client, token_endpoint_auth_method='client_secret_basic').json()
    reg, code, _ = authorize(client, registration=reg)
    credentials = base64.b64encode((reg['client_id']+':'+reg['client_secret']).encode()).decode()
    headers = {'Authorization':'Basic '+credentials}
    response = client.post('/token', data={'grant_type':'authorization_code','code':code,'redirect_uri':REDIRECT,'code_verifier':VERIFIER,'resource':BASE+'/mcp/student'}, headers=headers)
    assert response.status_code == 200, response.text
    token = response.json()['access_token']
    assert client.post('/revoke', data={'token':token}, headers=headers).status_code == 200
    assert rpc(client, token, 'tools/list').status_code == 401
    reg, code, _ = authorize(client)
    token = exchange(client, reg, code).json()['access_token']
    assert client.post('/revoke', data={'client_id':reg['client_id'],'token':token}).status_code == 200
    assert rpc(client, token, 'tools/list').status_code == 401


def test_oauth_tokens_and_client_registration_survive_restart(client):
    reg, code, _ = authorize(client)
    pair = exchange(client, reg, code).json()
    app = create_app(client.app.state.service.db_path, seed=False)
    with TestClient(app, base_url=BASE) as restarted:
        assert rpc(restarted, pair['access_token'], 'tools/list').status_code == 200
        response = restarted.post('/token', data={'grant_type':'refresh_token','client_id':reg['client_id'],'refresh_token':pair['refresh_token'],'resource':BASE+'/mcp/student'})
        assert response.status_code == 200, response.text
        assert rpc(restarted, response.json()['access_token'], 'tools/list').status_code == 200


def test_one_user_cannot_disconnect_another_users_grant(client):
    reg, code, _ = authorize(client)
    pair = exchange(client, reg, code).json()
    grant = client.get('/api/oauth/connections').json()['connections'][0]['id']
    authenticate(client, 'different-user@example.test')
    assert client.get('/api/oauth/connections').json()['connections'] == []
    assert client.delete('/api/oauth/connections/'+grant).status_code == 200
    assert rpc(client, pair['access_token'], 'tools/list').status_code == 200


def test_consent_csp_allows_only_registered_callback_origin(client):
    authenticate(client)
    reg=register(client,redirect_uris=['https://chatgpt.com/connector/oauth/test-callback?client=test']).json()
    start=begin(client,reg['client_id'],redirect_uri=reg['redirect_uris'][0])
    response=client.get(start.headers['location'])
    assert response.status_code==200
    csp=response.headers['content-security-policy']
    assert "form-action 'self' https://chatgpt.com;" in csp
    assert 'test-callback' not in csp and 'client=test' not in csp
    assert "frame-ancestors 'none'" in csp and "default-src 'self'" in csp
    assert '/static/consent.js' in response.text
    for uri in ('https://example.com;evil/callback','https://example.com%3B/callback'):
        assert register(client,redirect_uris=[uri]).status_code==400
