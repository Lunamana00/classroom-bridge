import pytest


@pytest.fixture(autouse=True)
def isolate_provider_credentials(monkeypatch):
    # Prevent developer .env.local credentials from entering tests or real requests.
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("CLASSROOM_TEACHER_EMAILS", "teacher@example.test,other-teacher@example.test")
    monkeypatch.setenv("CLASSROOM_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("CLASSROOM_MCP_ALLOWED_ORIGINS", "")
    monkeypatch.setenv("OPEN_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://factchat-cloud.mindlogic.ai/v1/gateway")
    monkeypatch.setenv("CONCEPT_EXTRACTION_MODE", "disabled")
    # Existing fixtures exercise the explicitly supported legacy retrieval path.
    # Concept retrieval tests remove this override to cover the product default.
    monkeypatch.setenv("CLASSROOM_RETRIEVAL", "lexical")


def authenticate(client, email='teacher@example.test', sub=None):
    auth = client.app.state.auth
    user = auth.login_user({'sub': sub or email, 'email': email, 'email_verified': True, 'name': email.split('@')[0]})
    token, csrf = auth.new_session(user['id'])
    client.cookies.set('classroom_session', token)
    client.headers['X-CSRF-Token'] = csrf
    return user


def authenticated_client(app, **kwargs):
    from fastapi.testclient import TestClient
    client = TestClient(app, **kwargs)
    authenticate(client)
    return client
