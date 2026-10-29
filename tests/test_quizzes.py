import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from classroom.auth import Auth
from classroom.quizzes import QuizStore
from classroom.service import Service


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv('CLASSROOM_MODE', 'demo')
    service = Service(str(tmp_path / 'quiz.sqlite'))
    auth = Auth(service)
    teacher = auth.login_user({'sub':'teacher','email':'teacher@example.test','email_verified':True,'name':'Teacher'})
    students = [auth.login_user({'sub':f'student{i}','email':f'student{i}@example.test','email_verified':True,'name':f'Student {i}'}) for i in range(2)]
    with service._db() as db:
        for user in students:
            db.execute("INSERT INTO course_members VALUES(?,?,'student')", (1,user['id']))
    store = QuizStore(service)
    source = service.get_material(1, service.list_materials(1)[0]['id'])['chunks'][0]['id']
    return service, store, teacher['id'], [u['id'] for u in students], source


def payload(source, **changes):
    result = {'title':'경사하강법 방향', 'concepts':['경사하강법'], 'question':'경사하강법의 갱신 방향은?', 'kind':'multiple_choice', 'options':['기울기 방향','기울기 반대 방향'], 'correct_answer':'B', 'accepted_answers':[], 'explanation':'손실을 줄이기 위해 기울기의 반대 방향으로 이동합니다.', 'source_chunk_ids':[source]}
    result.update(changes)
    return result


def test_chain_rule_aliases_match_explicit_gradient_terms_without_generic_gradient_match():
    assert QuizStore._matches("chain rule", "upstream gradient와 local gradient를 왜 곱하나요?")
    assert QuizStore._matches("연쇄법칙", "local gradient를 설명해 주세요")
    assert not QuizStore._matches("chain rule", "gradient descent의 학습률을 알려줘")
    assert not QuizStore._matches("chain rule", "upstreamgradientnoise의 이름")


def question(service, course_id=1, text='경사하강법을 설명해 줘'):
    with service._db() as db:
        return db.execute("INSERT INTO questions(course_id,question,topic,created_at,sufficient,is_demo,mode) VALUES(?,?,?,'2026-10-30',0,0,'demo')", (course_id,text,'경사하강법')).lastrowid


def delivery(data, student=None):
    service, store, _, students, _ = data
    user_id = student or students[0]
    return store.related_quizzes(1,'경사하강법을 설명해 줘',[],user_id,question(service))[0]


def publish(data, **changes):
    _, store, teacher, _, source = data
    draft=store.create_draft(1,teacher,payload(source,**changes))
    return store.publish(1,draft['id'],teacher)


def test_draft_needs_teacher_approval_and_public_delivery_hides_key(data):
    service,store,teacher,students,source=data
    with pytest.raises(PermissionError):
        store.create_draft(1,students[0],payload(source))
    with pytest.raises(ValueError):
        store.create_draft(1,teacher,{**payload(source),'status':'published'})
    draft=store.create_draft(1,teacher,payload(source))
    assert draft['status']=='draft' and draft['correct_answer']=='B'
    assert store.related_quizzes(1,'경사하강법?',[],students[0],question(service))==[]
    with pytest.raises(PermissionError):
        store.publish(1,draft['id'],students[0])
    published=store.publish(1,draft['id'],teacher)
    assert published['status']=='published' and published['approved_by']==teacher and published['approved_at']
    public=delivery(data)
    assert public['quiz_id']==draft['id'] and public['options'][1]=={'id':'B','text':'기울기 반대 방향'}
    assert not {'correct_answer','accepted_answers','explanation','created_by','approved_by'} & public.keys()
    assert all('text' not in source for source in public['sources'])
    with pytest.raises(ValueError):
        store.update_draft(1,draft['id'],teacher,payload(source,correct_answer='A'))


def test_draft_can_be_reviewed_before_immutable_publication(data):
    _,store,teacher,_,source=data
    draft=store.create_draft(1,teacher,payload(source))
    updated=store.update_draft(1,draft['id'],teacher,payload(source,title='검토 완료 제목'))
    assert updated['revision']==2 and updated['title']=='검토 완료 제목'
    assert store.publish(1,draft['id'],teacher)['revision']==2


def test_approval_rejects_a_revision_changed_after_teacher_review(data):
    _,store,teacher,_,source=data
    reviewed=store.create_draft(1,teacher,payload(source))
    updated=store.update_draft(1,reviewed['id'],teacher,payload(source,correct_answer='A'))
    with pytest.raises(ValueError,match='변경'):
        store.publish(1,reviewed['id'],teacher,expected_revision=reviewed['revision'])
    assert store.list_quizzes(1)[0]['status']=='draft'
    published=store.publish(1,reviewed['id'],teacher,expected_revision=updated['revision'])
    assert published['correct_answer']=='A' and published['revision']==updated['revision']


def test_mcp_review_requires_exact_confirmation_and_consumes_hashed_token(data):
    service,store,teacher,_,source=data
    draft=store.create_draft(1,teacher,payload(source))
    reviewed=store.review(1,draft['id'],teacher)
    assert reviewed['correct_answer']=='B' and reviewed['status']=='draft'
    assert reviewed['confirmation_phrase']==f"퀴즈 #{draft['id']} v1 배포 승인"
    assert 590 <= reviewed['expires_at']-int(time.time()) <= 600
    with service._db() as db:
        saved=dict(db.execute('SELECT * FROM quiz_reviews').fetchone())
    assert len(saved['review_token_hash'])==64
    assert reviewed['review_token'] not in saved.values()
    for confirmation in [True,False,'', '승인',reviewed['confirmation_phrase']+' ']:
        with pytest.raises(ValueError):
            store.approve_review(1,draft['id'],teacher,reviewed['review_token'],confirmation)
        assert store.list_quizzes(1)[0]['status']=='draft'
    approved=store.approve_review(1,draft['id'],teacher,reviewed['review_token'],reviewed['confirmation_phrase'])
    assert approved['status']=='published' and approved['approved_by']==teacher
    assert approved['approval_channel']=='mcp' and approved['approved_at']
    with pytest.raises(ValueError):
        store.approve_review(1,draft['id'],teacher,reviewed['review_token'],reviewed['confirmation_phrase'])
    with service._db() as db:
        assert db.execute('SELECT COUNT(*) FROM quiz_reviews').fetchone()[0]==0


def test_review_is_bound_to_course_quiz_and_authorized_teacher(data):
    service,store,teacher,students,source=data
    draft=store.create_draft(1,teacher,payload(source))
    with pytest.raises(PermissionError):
        store.review(1,draft['id'],students[0])
    reviewed=store.review(1,draft['id'],teacher)
    other=store.create_draft(1,teacher,payload(source,title='별도 초안'))
    with pytest.raises(PermissionError):
        store.approve_review(1,other['id'],teacher,reviewed['review_token'],reviewed['confirmation_phrase'])
    co_teacher=Auth(service).login_user({'sub':'co-teacher','email':'co-teacher@example.test','email_verified':True,'name':'Co Teacher'})
    with service._db() as db:
        db.execute("INSERT INTO course_members VALUES(?,?,'teacher')", (1,co_teacher['id']))
    with pytest.raises(PermissionError):
        store.approve_review(1,draft['id'],co_teacher['id'],reviewed['review_token'],reviewed['confirmation_phrase'])
    course=service.create_course('별도 수업',owner_id=teacher)
    material=service.add_text(course['id'],'별도 자료','별도 수업의 출처')
    foreign_source=service.get_material(course['id'],material['id'])['chunks'][0]['id']
    foreign=store.create_draft(course['id'],teacher,payload(foreign_source))
    with pytest.raises(PermissionError):
        store.approve_review(course['id'],foreign['id'],teacher,reviewed['review_token'],reviewed['confirmation_phrase'])
    assert store.approve_review(1,draft['id'],teacher,reviewed['review_token'],reviewed['confirmation_phrase'])['status']=='published'


def test_review_expiration_and_replacement_require_a_fresh_challenge(data):
    service,store,teacher,_,source=data
    draft=store.create_draft(1,teacher,payload(source))
    old=store.review(1,draft['id'],teacher)
    current=store.review(1,draft['id'],teacher)
    assert old['review_token']!=current['review_token']
    with pytest.raises(ValueError):
        store.approve_review(1,draft['id'],teacher,old['review_token'],old['confirmation_phrase'])
    with service._db() as db:
        assert db.execute('SELECT COUNT(*) FROM quiz_reviews').fetchone()[0]==1
        db.execute('UPDATE quiz_reviews SET expires_at=0')
    with pytest.raises(ValueError):
        store.approve_review(1,draft['id'],teacher,current['review_token'],current['confirmation_phrase'])
    fresh=store.review(1,draft['id'],teacher)
    assert store.approve_review(1,draft['id'],teacher,fresh['review_token'],fresh['confirmation_phrase'])['status']=='published'


def test_review_rejects_changed_revision_and_deleted_source(data):
    service,store,teacher,_,source=data
    draft=store.create_draft(1,teacher,payload(source))
    old=store.review(1,draft['id'],teacher)
    store.update_draft(1,draft['id'],teacher,payload(source,correct_answer='A'))
    with pytest.raises(ValueError,match='변경'):
        store.approve_review(1,draft['id'],teacher,old['review_token'],old['confirmation_phrase'])
    current=store.review(1,draft['id'],teacher)
    assert 'v2' in current['confirmation_phrase']
    with service._db() as db:
        db.execute('DELETE FROM chunks WHERE id=?',(source,))
    with pytest.raises(ValueError,match='출처'):
        store.approve_review(1,draft['id'],teacher,current['review_token'],current['confirmation_phrase'])
    with pytest.raises(ValueError,match='출처'):
        store.review(1,draft['id'],teacher)
    assert store.list_quizzes(1)[0]['status']=='draft'


def test_web_publication_invalidates_mcp_review_without_changing_audit(data):
    _,store,teacher,_,source=data
    draft=store.create_draft(1,teacher,payload(source))
    reviewed=store.review(1,draft['id'],teacher)
    published=store.publish(1,draft['id'],teacher,draft['revision'])
    assert published['approval_channel']=='web'
    with pytest.raises(ValueError):
        store.approve_review(1,draft['id'],teacher,reviewed['review_token'],reviewed['confirmation_phrase'])
    with pytest.raises(ValueError):
        store.review(1,draft['id'],teacher)
    assert store.publish(1,draft['id'],teacher,draft['revision'])['approval_channel']=='web'


def test_concurrent_mcp_approval_consumes_review_once(data):
    _,store,teacher,_,source=data
    draft=store.create_draft(1,teacher,payload(source))
    reviewed=store.review(1,draft['id'],teacher)
    def approve():
        try:
            return store.approve_review(1,draft['id'],teacher,reviewed['review_token'],reviewed['confirmation_phrase'])['status']
        except ValueError:
            return 'consumed'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:approve(),range(2)))
    assert sorted(results)==['consumed','published']


@pytest.mark.parametrize('bad', [
    {'kind':'free_form'}, {'correct_answer':'C'}, {'concepts':[]}, {'concepts':['학습률',' 학습률 ']},
    {'options':['같음','같음']}, {'accepted_answers':['추가 정답']}, {'source_chunk_ids':[True]},
    {'source_chunk_ids':[99999]}, {'question':''}, {'explanation':''},
])
def test_invalid_keys_or_unsupported_grading_never_enter_bank(data,bad):
    _,store,teacher,_,source=data
    with pytest.raises(ValueError):
        store.create_draft(1,teacher,payload(source,**bad))
    assert store.list_quizzes(1)==[]


def test_sources_are_scoped_and_deleted_sources_disable_delivery_and_grading(data):
    service,store,teacher,students,source=data
    other=service.create_course('다른 수업',owner_id=teacher)
    m=service.add_text(other['id'],'별도','다른 수업의 본문')
    foreign=service.get_material(other['id'],m['id'])['chunks'][0]['id']
    with pytest.raises(ValueError):
        store.create_draft(1,teacher,payload(foreign))
    quiz=publish(data)
    served=delivery(data)
    material=next(m for m in service.list_materials(1) if any(c['id']==source for c in service.get_material(1,m['id'])['chunks']))
    service.delete_material(1,material['id'])
    assert not store.list_quizzes(1)[0]['available']
    assert store.related_quizzes(1,'경사하강법?',[],students[0],question(service))==[]
    with pytest.raises(LookupError):
        store.submit_answer(1,served['delivery_id'],students[0],'B')
    with pytest.raises(ValueError):
        store.publish(1,quiz['id'],teacher)


def test_deleted_sources_cannot_be_resurrected_by_sqlite_id_reuse_or_restart(data):
    service,store,teacher,students,_=data
    original=service.add_text(1,'삭제할 출처','경사하강법은 손실을 줄입니다.')
    source=service.get_material(1,original['id'])['chunks'][0]['id']
    published=publish(data,source_chunk_ids=[source])
    draft=store.create_draft(1,teacher,payload(source,title='삭제된 출처의 초안'))
    served=delivery(data)
    service.delete_material(1,original['id'])
    replacement=service.add_text(1,'새로운 별개 출처','전혀 다른 강의자료입니다.')
    reused=service.get_material(1,replacement['id'])['chunks'][0]['id']
    assert reused==source
    restarted=QuizStore(service)
    assert all(not quiz['available'] and quiz['sources']==[] for quiz in restarted.list_quizzes(1))
    assert restarted.related_quizzes(1,'경사하강법?',[],students[0],question(service))==[]
    with pytest.raises(LookupError):
        restarted.submit_answer(1,served['delivery_id'],students[0],'B')
    for quiz in [published,draft]:
        with pytest.raises(ValueError):
            restarted.publish(1,quiz['id'],teacher)
    # A teacher may deliberately bind a reviewed draft to the replacement.
    updated=restarted.update_draft(1,draft['id'],teacher,payload(reused))
    assert updated['available'] and updated['sources'][0]['title']=='새로운 별개 출처'
    assert restarted.publish(1,draft['id'],teacher,updated['revision'])['status']=='published'


def test_grade_binds_delivery_to_student_and_preserves_server_decision(data):
    service,store,teacher,students,_=data
    quiz=publish(data)
    served=delivery(data)
    with pytest.raises(PermissionError):
        store.submit_answer(1,served['delivery_id'],students[1],'B')
    with pytest.raises(ValueError):
        store.submit_answer(1,served['delivery_id'],students[0],'둘 중 아무거나')
    assert store.student_progress(1,students[0])['attempt_count']==0
    result=store.submit_answer(1,served['delivery_id'],students[0],'A')
    assert result['is_correct'] is False and result['correct_answer']=='B'
    assert result['is_first_attempt'] and result['grading_method']=='choice_exact'
    replay=store.submit_answer(1,served['delivery_id'],students[0],'A')
    assert replay['replayed'] and replay['attempt_id']==result['attempt_id']
    with pytest.raises(ValueError):
        store.submit_answer(1,served['delivery_id'],students[0],'B')
    assert store.student_progress(1,students[0])['attempt_count']==1
    store.archive(1,quiz['id'],teacher)
    with pytest.raises(LookupError):
        store.submit_answer(1,served['delivery_id'],students[0],'A')


@pytest.mark.parametrize('answer',['B','b','2','기울기 반대 방향'])
def test_choice_ids_numbers_and_exact_text_are_unambiguous(data,answer):
    _,store,_,students,_=data
    publish(data)
    served=delivery(data)
    result=store.submit_answer(1,served['delivery_id'],students[0],answer)
    assert result['is_correct']
    with store.service._db() as db:
        assert db.execute('SELECT answer FROM quiz_attempts').fetchone()[0]==answer


def test_short_answer_aliases_use_documented_normalization_not_semantics(data):
    _,store,_,students,_=data
    publish(data,kind='short_answer',options=[],correct_answer='gradient descent',accepted_answers=['경사하강법'])
    served=delivery(data)
    result=store.submit_answer(1,served['delivery_id'],students[0],'  ＧＲＡＤＩＥＮＴ   DESCENT  ')
    assert result['is_correct'] and result['grading_method']=='normalized_exact_match'
    served=delivery(data)
    assert not store.submit_answer(1,served['delivery_id'],students[0],'기울기를 사용하는 알고리즘')['is_correct']


def test_retries_and_teacher_preview_do_not_inflate_estimate(data):
    _,store,teacher,students,_=data
    publish(data)
    first=delivery(data)
    store.submit_answer(1,first['delivery_id'],students[0],'A')
    retake=delivery(data)
    grade=store.submit_answer(1,retake['delivery_id'],students[0],'B')
    assert not grade['is_first_attempt']
    preview=delivery(data,teacher)
    result=store.submit_answer(1,preview['delivery_id'],teacher,'B')
    assert result['is_preview'] and not result['is_first_attempt']
    progress=store.student_progress(1,students[0])
    metric=progress['concepts'][0]
    assert progress['attempt_count']==2 and progress['first_attempt_count']==1 and progress['retake_count']==1
    assert metric['correct_count']==0 and metric['accuracy']==0 and metric['status']=='insufficient_data'
    assert metric['estimated_correctness']==pytest.approx(1/3,abs=.0001)
    assert store.student_progress(1,teacher)['preview_attempt_count']==1
    insights=store.insights(1)
    assert insights['attempt_count']==2 and insights['student_count']==1 and insights['preview_attempt_count']==1
    assert insights['students'][0]['student_id']==students[0]
    assert '성적' in insights['notice'] and '첫 제출' in insights['notice']


def test_relevance_limits_and_question_binding(data):
    service,store,teacher,students,source=data
    for i in range(5):
        publish(data,title=f'방향 {i}')
    matches=store.related_quizzes(1,'경사하강법?',[],students[0],question(service))
    assert len(matches)==3
    assert store.related_quizzes(1,'내일 서울 날씨?',[],students[0],question(service,text='내일 서울 날씨?'))==[]
    other=service.create_course('다른 수업',owner_id=teacher)
    with pytest.raises(ValueError):
        store.related_quizzes(1,'경사하강법?',[],students[0],question(service,other['id']))
    with service._db() as db:
        db.execute('UPDATE quiz_deliveries SET expires_at=0')
    with pytest.raises(LookupError):
        store.submit_answer(1,matches[0]['delivery_id'],students[0],'B')


def test_short_english_concept_is_not_matched_inside_an_unrelated_word(data):
    service,store,_,students,_=data
    publish(data,title='AI 확인',concepts=['AI'])
    unrelated='What causes rain?'
    assert store.related_quizzes(1,unrelated,[],students[0],question(service,text=unrelated))==[]
    related='AI의 의미를 설명해 줘'
    assert len(store.related_quizzes(1,related,[],students[0],question(service,text=related)))==1


def test_canonical_alias_matches_separated_english_words(data):
    service,store,_,students,_=data
    publish(data)
    text='How does gradient descent work?'
    assert len(store.related_quizzes(1,text,[],students[0],question(service,text=text)))==1


def test_concurrent_retries_record_once(data):
    _,store,_,students,_=data
    publish(data)
    served=delivery(data)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(store.submit_answer,1,served['delivery_id'],students[0],'B') for _ in range(2)]
        results=[f.result() for f in futures]
    assert len({r['attempt_id'] for r in results})==1
    assert sorted(r['replayed'] for r in results)==[False,True]
    assert store.student_progress(1,students[0])['attempt_count']==1


def test_concept_estimate_uses_distinct_quizzes_and_first_observed_answers(data):
    _,store,_,students,_=data
    for i in range(3):
        publish(data,title=f'방향 확인 {i}')
    served=store.related_quizzes(1,'경사하강법?',[],students[0],question(store.service))
    for quiz,answer in zip(served,['B','A','B']):
        store.submit_answer(1,quiz['delivery_id'],students[0],answer)
    progress=store.student_progress(1,students[0])
    metric=progress['concepts'][0]
    assert metric['first_attempt_count']==3 and metric['quiz_count']==3 and metric['student_count']==1
    assert metric['correct_count']==2 and metric['incorrect_count']==1
    assert metric['accuracy']==pytest.approx(2/3,abs=.0001)
    assert metric['estimated_correctness']==.6 and metric['status']=='developing'
    served=store.related_quizzes(1,'경사하강법?',[],students[1],question(store.service))
    store.submit_answer(1,served[0]['delivery_id'],students[1],'B')
    metric=store.insights(1)['concepts'][0]
    assert metric['first_attempt_count']==4 and metric['quiz_count']==3 and metric['student_count']==2


def test_empty_aggregate_does_not_require_users_table(tmp_path,monkeypatch):
    monkeypatch.setenv('CLASSROOM_MODE','demo')
    service=Service(str(tmp_path/'standalone.sqlite'))
    store=QuizStore(service)
    result=store.insights(1)
    assert result['attempt_count']==0 and result['concepts']==[] and result['students']==[]
