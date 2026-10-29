"""Disposable-account verification of the public deployment; no Google impersonation route."""
import asyncio
import base64
import hashlib
import json
import os
import secrets
from urllib.parse import parse_qs, parse_qsl, urlparse

from dotenv import dotenv_values
os.environ.update({k:v for k,v in dotenv_values('/etc/classroom-bridge.env').items() if v is not None})
import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata
from classroom.service import Service
from classroom.auth import Auth

BASE='https://canigetbus.app'
CALLBACK='http://127.0.0.1:39877/callback'
service=Service(os.environ['CLASSROOM_DB'], seed=False)
auth=Auth(service)
unique=secrets.token_hex(8)
user=auth.login_user({'sub':'deployment-test-'+unique,'email':'deployment-'+unique+'@example.invalid','email_verified':True,'name':'Disposable deployment test'})
session,csrf=auth.new_session(user['id'])
course=service.create_course('[Temporary MCP OAuth verification] '+unique, owner_id=user['id'])
material=service.add_text(course['id'],'Gradient descent','Gradient descent updates parameters in the opposite direction of the loss gradient.')
# Published fixture for student-path verification; human web approval is covered
# by the auth/API tests, not impersonated through the live teacher allowlist.
source_id=service.get_material(course['id'],material['id'])['chunks'][0]['id']
# Internal fixture confined to this disposable course. This does not expose an
# approval route or approve concepts belonging to any existing teacher/course.
with service._db() as db:
    concept_id=db.execute(
        "INSERT INTO course_concepts(course_id,name,description,aliases_json,status,revision,"
        "approved_by,approved_at,reviewed_by,reviewed_at,extraction_model,created_at,updated_at) "
        "VALUES(?,?,?,?,'approved',1,?,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (course['id'],'Gradient descent',
         'Updates parameters in the opposite direction of the loss gradient.',
         json.dumps(['gradient descent','경사하강법'],ensure_ascii=False),
         user['id'],user['id'],'disposable-deployment-fixture'),
    ).lastrowid
    db.execute('INSERT INTO concept_sources(concept_id,chunk_id) VALUES(?,?)',(concept_id,source_id))
quiz=service.quizzes.create_draft(course['id'],user['id'],{
    'title':'Disposable concept practice','concepts':['gradient descent'],
    'question':'Which direction does gradient descent move?',
    'kind':'multiple_choice','options':['Along the gradient','Opposite the gradient'],
    'correct_answer':'B','accepted_answers':[],
    'explanation':'Gradient descent moves opposite the loss gradient.',
    'source_chunk_ids':[source_id],
})
service.quizzes.publish(course['id'],quiz['id'],user['id'],quiz['revision'])
with service._db() as db:
    db.execute("UPDATE course_members SET role='student' WHERE course_id=? AND user_id=?",(course['id'],user['id']))

registered=[]
class Storage:
    tokens=None
    info=None
    async def get_tokens(self): return self.tokens
    async def set_tokens(self,tokens): self.tokens=tokens
    async def get_client_info(self): return self.info
    async def set_client_info(self,info):
        self.info=info
        if info.client_id not in registered: registered.append(info.client_id)

async def main():
    storage=Storage()
    callbacks=[]
    with httpx.Client(base_url=BASE, cookies={'classroom_session':session}, headers={'X-CSRF-Token':csrf,'Origin':BASE}, timeout=30, follow_redirects=False) as browser:
        async def open_browser(url):
            parsed=urlparse(url)
            response=browser.post(parsed.path,data=dict(parse_qsl(parsed.query)),headers={'Origin':'https://chatgpt.com'})
            assert response.status_code==302, 'authorization redirect failed'
            consent=browser.get(response.headers['location'])
            assert consent.status_code==200, 'consent page failed'
            ticket=parse_qs(urlparse(response.headers['location']).query)['request'][0]
            denied=browser.post('/oauth/consent',data={'request':ticket,'csrf':'invalid','decision':'allow'},headers={'Origin':'null'})
            assert denied.status_code==403, 'consent CSRF check failed'
            response=browser.post('/oauth/consent',data={'request':ticket,'csrf':csrf,'decision':'allow'},headers={'Origin':'null'})
            assert response.status_code==303, 'consent approval failed'
            values=parse_qs(urlparse(response.headers['location']).query)
            callbacks.append((values['code'][0],values['state'][0]))
        async def callback(): return callbacks[-1]
        provider=OAuthClientProvider(server_url=BASE+'/mcp/student',client_metadata=OAuthClientMetadata(client_name='Disposable production verification',redirect_uris=[CALLBACK],token_endpoint_auth_method='none',grant_types=['authorization_code','refresh_token'],response_types=['code'],scope='classroom:student'),storage=storage,redirect_handler=open_browser,callback_handler=callback)
        async with httpx.AsyncClient(auth=provider,timeout=60,headers={'Origin':'https://chatgpt.com'}) as http:
            async with streamable_http_client(BASE+'/mcp/student',http_client=http) as (read,write,_):
                async with ClientSession(read,write) as mcp:
                    await mcp.initialize()
                    tools=await mcp.list_tools()
                    assert {tool.name for tool in tools.tools}=={'list_courses','search_course_materials','submit_quiz_answer','get_my_quiz_progress'}
                    result=await mcp.call_tool('list_courses',{})
                    assert not result.isError and unique in str(result)
                    result=await mcp.call_tool('search_course_materials',{'course_id':course['id'],'question':'Which direction does gradient descent update parameters?'})
                    assert not result.isError, 'source search tool failed'
                    payload=result.structuredContent
                    if payload is None: payload=json.loads(result.content[0].text)
                    assert payload['mode']=='codex', 'GPT provider is not active'
                    assert payload['retrieval_method']=='gpt_all_concepts', 'full-catalog GPT search is not active'
                    assert payload['concept_catalog_size']==1, 'disposable approved concept catalog is incomplete'
                    assert [concept['id'] for concept in payload['concepts']]==[concept_id], 'expected course concept was not selected'
                    assert payload['concepts'][0]['reason'].strip(), 'concept selection reason is missing'
                    assert payload['concepts'][0]['source_chunk_ids']==[source_id], 'concept source link was not preserved'
                    assert payload['sufficient'] and payload['evidence'], 'source grounding failed'
                    assert {source['chunk_id'] for source in payload['evidence']}=={source_id}, 'selected evidence escaped the disposable concept'
                    print('PASS: official SDK discovery, registration, PKCE, consent, token exchange, initialize, list_tools, course-scoped search',flush=True)
                    print('PASS: GPT full approved concept catalog, selected concept/reason, linked original evidence and sufficiency',flush=True)
                    assert len(payload['quizzes'])==1, 'related approved quiz missing'
                    delivery=payload['quizzes'][0]
                    assert not {'correct_answer','accepted_answers','explanation'} & delivery.keys(), 'student answer key leak'
                    graded=await mcp.call_tool('submit_quiz_answer',{'course_id':course['id'],'delivery_id':delivery['delivery_id'],'answer':'A'})
                    assert not graded.isError
                    grade=graded.structuredContent or json.loads(graded.content[0].text)
                    assert grade['is_correct'] is False and grade['is_first_attempt']
                    repeated=await mcp.call_tool('submit_quiz_answer',{'course_id':course['id'],'delivery_id':delivery['delivery_id'],'answer':'A'})
                    repeated_data=repeated.structuredContent or json.loads(repeated.content[0].text)
                    assert repeated_data['replayed'] and repeated_data['attempt_id']==grade['attempt_id']
                    progress=await mcp.call_tool('get_my_quiz_progress',{'course_id':course['id']})
                    progress_data=progress.structuredContent or json.loads(progress.content[0].text)
                    assert progress_data['first_attempt_count']==1 and progress_data['concepts'][0]['incorrect_count']==1
                    assert service.exam_plan(course['id'])['quiz_insights']['concepts'][0]['incorrect_count']==1
                    print('PASS: approved related quiz, hidden answer key, server grading, idempotent replay, own progress and exam evidence',flush=True)
                    denied=await mcp.call_tool('search_course_materials',{'course_id':2147483647,'question':'unrelated course'})
                    assert denied.isError, 'cross-course access was not blocked'
                    print('PASS: cross-course permission denial',flush=True)
        assert browser.post('/api/courses',json={'title':'must not create'},headers={'Origin':'https://chatgpt.com'}).status_code==403
        pair=storage.tokens
        async with httpx.AsyncClient(base_url=BASE,timeout=30,headers={'Origin':'https://chatgpt.com'}) as plain:
            preflight=await plain.options('/mcp/student',headers={'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'Authorization,Content-Type,MCP-Protocol-Version'})
            assert preflight.status_code==200 and preflight.headers.get('access-control-allow-origin')=='https://chatgpt.com'
            print('PASS: ChatGPT-origin authorization/MCP/preflight and null-origin consent; web mutation and CSRF checks retained',flush=True)
            headers={'Authorization':'Bearer '+pair.access_token,'Accept':'application/json, text/event-stream'}
            assert (await plain.post('/mcp/instructor',json={'jsonrpc':'2.0','id':1,'method':'tools/list'},headers=headers)).status_code==401
            body={'grant_type':'refresh_token','client_id':storage.info.client_id,'refresh_token':pair.refresh_token,'resource':BASE+'/mcp/student'}
            response=await plain.post('/token',data=body)
            assert response.status_code==200, 'refresh failed'
            rotated=response.json()
            assert rotated['refresh_token']!=pair.refresh_token
            request={'jsonrpc':'2.0','id':2,'method':'tools/list'}
            assert (await plain.post('/mcp/student',json=request,headers=headers)).status_code==401
            headers['Authorization']='Bearer '+rotated['access_token']
            assert (await plain.post('/mcp/student',json=request,headers=headers)).status_code==200
            print('PASS: resource binding and refresh rotation',flush=True)
            connections=browser.get('/api/oauth/connections').json()['connections']
            grant=next(item for item in connections if item['client_id']==storage.info.client_id)
            assert browser.delete('/api/oauth/connections/'+grant['id']).status_code==200
            assert (await plain.post('/mcp/student',json=request,headers=headers)).status_code==401
            assert (await plain.post('/token',data={**body,'refresh_token':rotated['refresh_token']})).status_code==400
            print('PASS: disconnect immediately revokes access and refresh tokens',flush=True)
        # Registration/approval for instructor scope must still reject this student.
        reg=browser.post('/register',json={'client_name':'Disposable instructor-denial test','redirect_uris':[CALLBACK],'token_endpoint_auth_method':'none','grant_types':['authorization_code','refresh_token'],'response_types':['code'],'scope':'classroom:instructor'})
        assert reg.status_code==201
        info=reg.json(); registered.append(info['client_id'])
        verifier=secrets.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        start=browser.get('/authorize',params={'response_type':'code','client_id':info['client_id'],'redirect_uri':CALLBACK,'scope':'classroom:instructor','resource':BASE+'/mcp/instructor','code_challenge':challenge,'code_challenge_method':'S256'})
        assert start.status_code==302
        assert browser.get(start.headers['location']).status_code==403
        print('PASS: student cannot approve instructor scope',flush=True)

try:
    asyncio.run(main())
finally:
    with service._db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM oauth_access WHERE grant_id IN (SELECT id FROM oauth_grants WHERE user_id=?)',(user['id'],))
        db.execute('DELETE FROM oauth_refresh WHERE grant_id IN (SELECT id FROM oauth_grants WHERE user_id=?)',(user['id'],))
        db.execute('DELETE FROM oauth_grants WHERE user_id=?',(user['id'],))
        db.execute('DELETE FROM oauth_codes WHERE user_id=?',(user['id'],))
        for client_id in registered:
            db.execute('DELETE FROM oauth_pending WHERE client_id=?',(client_id,))
            db.execute('DELETE FROM oauth_clients WHERE client_id=?',(client_id,))
        db.execute('DELETE FROM courses WHERE id=?',(course['id'],))
        db.execute('DELETE FROM auth_sessions WHERE user_id=?',(user['id'],))
        db.execute('DELETE FROM course_members WHERE user_id=?',(user['id'],))
        db.execute('DELETE FROM users WHERE id=?',(user['id'],))
    print('Temporary test user, course, sessions and OAuth grants removed.',flush=True)
