"""Browser/session CSRF checks must not reject independently authenticated protocols."""
import pytest
from urllib.parse import parse_qs, urlparse

from conftest import authenticate
from test_oauth import BASE, REDIRECT, CHALLENGE, authorize, exchange, register
from test_oauth import client


@pytest.mark.parametrize('origin',['https://chatgpt.com','null'])
def test_oauth_authorization_post_accepts_client_origin(client, origin):
    reg=register(client).json()
    response=client.post('/authorize', data={'client_id':reg['client_id'],'redirect_uri':REDIRECT,'response_type':'code','code_challenge':CHALLENGE,'code_challenge_method':'S256','resource':BASE+'/mcp/student','scope':'classroom:student','state':'test-state'},headers={'Origin':origin},follow_redirects=False)
    assert response.status_code==302, response.text
    assert '/oauth/consent?' in response.headers['location']


def test_null_origin_consent_still_requires_session_and_csrf(client):
    authenticate(client)
    reg=register(client).json()
    from test_oauth import begin
    start=begin(client,reg['client_id'])
    ticket=parse_qs(urlparse(start.headers['location']).query)['request'][0]
    csrf=client.get('/api/auth/me').json()['csrf_token']
    form={'request':ticket,'csrf':csrf,'decision':'allow'}
    assert client.post('/oauth/consent',data={**form,'csrf':'wrong'},headers={'Origin':'null'}).status_code==403
    assert client.post('/oauth/consent',data=form,headers={'Origin':'https://unrelated.example'}).status_code==403
    result=client.post('/oauth/consent',data=form,headers={'Origin':'null'},follow_redirects=False)
    assert result.status_code==303, result.text
    assert 'code=' in result.headers['location']


@pytest.mark.parametrize('origin',['https://chatgpt.com','https://chat.openai.com'])
def test_mcp_chatgpt_origin_uses_bearer_auth_not_web_cookie(client, origin):
    reg,code,_=authorize(client)
    token=exchange(client,reg,code).json()['access_token']
    payload={'jsonrpc':'2.0','id':1,'method':'tools/list'}
    headers={'Origin':origin,'Accept':'application/json, text/event-stream'}
    denied=client.post('/mcp/student',json=payload,headers=headers)
    assert denied.status_code==401
    assert 'resource_metadata' in denied.headers['www-authenticate']
    allowed=client.post('/mcp/student',json=payload,headers={**headers,'Authorization':'Bearer '+token})
    assert allowed.status_code==200, allowed.text
    assert allowed.headers['access-control-allow-origin']==origin
    assert client.post('/mcp/instructor',json=payload,headers={**headers,'Authorization':'Bearer '+token}).status_code==401
    assert client.post('/mcp/student',json=payload,headers={**headers,'Origin':'https://unrelated.example','Authorization':'Bearer '+token}).status_code==403


def test_mcp_preflight_allows_explicit_client_origin_only(client):
    headers={'Origin':'https://chatgpt.com','Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Authorization,Content-Type,MCP-Protocol-Version'}
    response=client.options('/mcp/student',headers=headers)
    assert response.status_code==200, response.text
    assert response.headers['access-control-allow-origin']=='https://chatgpt.com'
    assert response.headers.get('access-control-allow-credentials')!='true'
    assert client.options('/mcp/student',headers={**headers,'Origin':'https://unrelated.example'}).status_code==400


@pytest.mark.parametrize('origin',['null','https://chatgpt.com','https://unrelated.example'])
@pytest.mark.parametrize('path',['/api/courses','/api/join','/auth/logout'])
def test_cookie_mutations_reject_other_origins_even_with_valid_csrf(client, origin, path):
    authenticate(client)
    response=client.post(path,json={'title':'must not create','token':'invalid'},headers={'Origin':origin})
    assert response.status_code==403
    assert response.json()['detail']=='같은 주소의 수업 화면에서 요청해주세요.'
