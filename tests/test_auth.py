import time

import pytest
from fastapi.testclient import TestClient
from conftest import authenticate

from classroom.app import create_app
from classroom.auth import digest


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('CLASSROOM_MODE', 'demo')
    return create_app(str(tmp_path / 'auth.sqlite'))


def test_anonymous_cannot_access_data_or_mutations(app):
    with TestClient(app) as client:
        assert 'Google로 로그인' in client.get('/').text
        assert client.get('/api/auth/me').json()['user'] is None
        assert client.get('/api/courses').status_code == 401
        assert client.get('/api/config').status_code == 401
        assert client.get('/api/courses/1/materials').status_code == 401
        assert client.post('/api/courses', json={'title':'no'}).status_code == 401
        assert client.post('/mcp/student', json={}).status_code == 401
        assert client.get('/auth/google').status_code == 503


def test_verified_email_and_sub_required(app):
    for claims in ({'sub':'x','email':'teacher@example.test','email_verified':False}, {'email':'teacher@example.test','email_verified':True}):
        with pytest.raises(Exception) as error:
            app.state.auth.login_user(claims)
        assert error.value.status_code == 403


def test_course_scoped_roles_and_invite_lifecycle(app):
    with TestClient(app) as teacher:
        student, other = TestClient(app), TestClient(app)
        authenticate(teacher)
        user = authenticate(student, 'student@example.test')
        authenticate(other, 'other-teacher@example.test')
        assert student.get('/api/courses').json()['courses'] == []
        assert other.get('/api/courses').json()['courses'] == []
        assert student.post('/api/courses', json={'title':'no'}).status_code == 403
        assert student.get('/api/courses/1/materials').status_code == 403
        invite = teacher.post('/api/courses/1/invite').json()['url'].split('#join=')[1]
        assert student.post('/api/join', json={'token':invite}).status_code == 200
        assert student.get('/api/courses').json()['courses'][0]['role'] == 'student'
        assert student.get('/api/courses/1/materials').status_code == 200
        assert student.post('/api/ask', json={'course_id':1,'question':'경사하강법이란?'}).status_code == 200
        assert student.get('/api/courses/1/analytics').status_code == 403
        assert student.get('/api/courses/1/teaching-brief').status_code == 403
        assert student.post('/api/courses/1/exam-plan', json={}).status_code == 403
        assert student.post('/api/courses/1/materials/text', json={'title':'x','text':'x'}).status_code == 403
        assert student.delete('/api/courses/1/materials/1').status_code == 403
        assert student.post('/api/courses/1/invite').status_code == 403
        assert student.get('/api/courses/1/members').status_code == 403
        assert other.get('/api/courses/1/analytics').status_code == 403
        new_course = other.post('/api/courses', json={'title':'다른 교수 수업'}).json()['id']
        assert teacher.get(f'/api/courses/{new_course}/analytics').status_code == 403
        assert other.get(f'/api/courses/{new_course}/analytics').status_code == 200
        replacement = teacher.post('/api/courses/1/invite').json()['url'].split('#join=')[1]
        assert student.post('/api/join', json={'token':invite}).status_code == 400
        assert teacher.delete('/api/courses/1/invite').status_code == 200
        assert student.post('/api/join', json={'token':replacement}).status_code == 400
        assert teacher.delete(f'/api/courses/1/members/{user["id"]}').status_code == 200
        assert student.get('/api/courses/1/materials').status_code == 403
        assert student.post('/api/ask', json={'course_id':1,'question':'x'}).status_code == 403


def test_csrf_expiry_logout_and_hash_storage(app):
    with TestClient(app) as client:
        authenticate(client)
        token = client.cookies.get('classroom_session')
        with app.state.service._db() as db:
            row = db.execute('SELECT token_hash FROM auth_sessions').fetchone()
            assert row[0] == digest(token) and row[0] != token
        assert client.post('/api/courses', json={'title':'bad'}, headers={'X-CSRF-Token':'bad'}).status_code == 403
        assert client.post('/auth/logout', headers={'X-CSRF-Token':'bad'}).status_code == 403
        assert client.post('/auth/logout').status_code == 200
        client.cookies.set('classroom_session', token)
        assert client.get('/api/courses').status_code == 401
        authenticate(client)
        with app.state.service._db() as db:
            db.execute('UPDATE auth_sessions SET expires_at=?', (int(time.time())-1,))
        assert client.get('/api/courses').status_code == 401


def test_forged_cookie_and_expired_invite(app):
    with TestClient(app) as teacher:
        student = TestClient(app)
        authenticate(teacher)
        authenticate(student, 'student@example.test')
        token = teacher.post('/api/courses/1/invite').json()['url'].split('#join=')[1]
        with app.state.service._db() as db:
            assert db.execute('SELECT token_hash FROM course_invites').fetchone()[0] == digest(token)
            db.execute('UPDATE course_invites SET expires_at=0')
        assert student.post('/api/join', json={'token':token}).status_code == 400
        student.cookies.set('classroom_session', 'forged')
        assert student.get('/api/courses').status_code == 401


def test_google_callback_does_not_expose_provider_errors(app, monkeypatch):
    async def failed(request):
        raise ValueError('provider-secret-do-not-expose')
    monkeypatch.setattr(app.state.auth.oauth.google, 'authorize_access_token', failed)
    with TestClient(app) as client:
        result = client.get('/auth/google/callback?code=secret&state=bad', follow_redirects=False)
        assert result.status_code == 303
        assert result.headers['location'] == '/?login_error=1'
        assert 'secret' not in result.text
        assert client.get('/api/auth/me').json()['user'] is None


def test_production_host_and_secure_login_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv('CLASSROOM_MODE', 'demo')
    monkeypatch.setenv('CLASSROOM_BASE_URL', 'https://canigetbus.app')
    app = create_app(str(tmp_path / 'production.sqlite'), seed=False)

    async def validated_google_token(request):
        return {'userinfo': {'sub': 'production-teacher', 'email': 'teacher@example.test', 'email_verified': True, 'name': 'Teacher'}}

    monkeypatch.setattr(app.state.auth.oauth.google, 'authorize_access_token', validated_google_token)
    with TestClient(app, base_url='https://canigetbus.app') as client:
        callback = client.get('/auth/google/callback', follow_redirects=False)
        assert callback.status_code == 303
        cookie = callback.headers.get_list('set-cookie')[0]
        assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie
        me = client.get('/api/auth/me').json()
        assert me['can_create_course']
        client.headers['X-CSRF-Token'] = me['csrf_token']
        assert client.post('/api/courses', json={'title':'클라우드 수업'}, headers={'Origin':'https://canigetbus.app'}).status_code == 201
        assert client.get('/', headers={'Host':'untrusted.invalid'}).status_code == 400
