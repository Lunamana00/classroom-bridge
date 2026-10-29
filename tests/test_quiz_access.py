"""Course and actor boundaries for approved concept quizzes over web and OAuth MCP."""
import json

from conftest import authenticate
from test_oauth import authorize, exchange, rpc
from test_oauth import client


def quiz_payload(client, course_id=1):
    materials = client.get(f'/api/courses/{course_id}/materials').json()['materials']
    source = client.get(f'/api/courses/{course_id}/materials/{materials[0]["id"]}').json()['chunks'][0]
    return {
        'title': '경사하강법 방향 확인', 'concepts': ['경사하강법'],
        'question': '경사하강법은 손실 기울기에 대해 어느 방향으로 이동하나요?',
        'kind': 'multiple_choice', 'options': ['기울기의 반대 방향', '기울기와 같은 방향'],
        'correct_answer': 'A', 'accepted_answers': [],
        'explanation': 'PRIVATE-RUBRIC-SENTINEL: 손실을 줄이는 방향을 확인합니다.',
        'source_chunk_ids': [source['id']],
    }


def draft(client, course_id=1, publish=False):
    authenticate(client)
    response = client.post(f'/api/courses/{course_id}/quizzes', json=quiz_payload(client, course_id))
    assert response.status_code in {200, 201}, response.text
    quiz = response.json()
    if publish:
        response = client.post(f'/api/courses/{course_id}/quizzes/{quiz["id"]}/publish', json={'expected_revision': quiz['revision']})
        assert response.status_code == 200, response.text
        quiz = response.json()
    return quiz


def enroll(client, email='student@example.test', course_id=1):
    authenticate(client)
    response = client.post(f'/api/courses/{course_id}/invite')
    token = response.json()['url'].split('#join=')[1]
    user = authenticate(client, email)
    assert client.post('/api/join', json={'token': token}).status_code == 200
    return user


def ask_for_quiz(client, course_id=1):
    response = client.post('/api/ask', json={'course_id': course_id, 'question': '경사하강법은 어느 방향으로 이동하나요?'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['quizzes'], body
    return body, body['quizzes'][0]


def answer(client, delivery, value, course_id=1):
    return client.post(f'/api/courses/{course_id}/quiz-answers', json={'delivery_id': delivery['delivery_id'], 'answer': value})


def no_keys(value):
    """No private grading fields may be hidden in nested student response objects."""
    if isinstance(value, dict):
        assert not {'correct_answer', 'accepted_answers', 'explanation'} & value.keys()
        for item in value.values():
            no_keys(item)
    elif isinstance(value, list):
        for item in value:
            no_keys(item)


def tool_payload(response):
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert not result.get('isError'), result
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def prompt_data(prompt):
    return json.loads(prompt[prompt.index('{'):])


def no_personal_rows(value):
    if isinstance(value, dict):
        assert not {'students', 'student_id', 'student_name'} & value.keys()
        for item in value.values():
            no_personal_rows(item)
    elif isinstance(value, list):
        for item in value:
            no_personal_rows(item)


def test_only_course_teacher_can_review_and_publish(client):
    quiz = draft(client)
    assert quiz['status'] == 'draft'
    assert quiz['correct_answer'] == 'A'
    enroll(client)
    for method, path in [
        ('get', '/api/courses/1/quizzes'),
        ('post', f'/api/courses/1/quizzes/{quiz["id"]}/publish'),
        ('post', f'/api/courses/1/quizzes/{quiz["id"]}/archive'),
        ('get', '/api/courses/1/quiz-insights'),
    ]:
        kwargs = {'json': {'expected_revision': quiz['revision']}} if path.endswith('/publish') else {}
        assert getattr(client, method)(path, **kwargs).status_code == 403
    response = client.post('/api/ask', json={'course_id': 1, 'question': '경사하강법이란?'})
    assert response.status_code == 200 and response.json()['quizzes'] == []
    authenticate(client, 'other-teacher@example.test')
    assert client.get('/api/courses/1/quizzes').status_code == 403
    assert client.post(f'/api/courses/1/quizzes/{quiz["id"]}/publish', json={'expected_revision': quiz['revision']}).status_code == 403
    authenticate(client)
    assert client.post(f'/api/courses/1/quizzes/{quiz["id"]}/publish').status_code == 422
    edited = client.put(f'/api/courses/1/quizzes/{quiz["id"]}', json=quiz_payload(client))
    assert edited.status_code == 200 and edited.json()['revision'] > quiz['revision']
    assert client.post(f'/api/courses/1/quizzes/{quiz["id"]}/publish', json={'expected_revision': quiz['revision']}).status_code == 400
    published = client.post(f'/api/courses/1/quizzes/{quiz["id"]}/publish', json={'expected_revision': edited.json()['revision']})
    assert published.status_code == 200 and published.json()['status'] == 'published'
    assert client.put(f'/api/courses/1/quizzes/{quiz["id"]}', json=quiz_payload(client)).status_code == 400


def test_student_delivery_hides_keys_and_actor_is_bound(client):
    draft(client, publish=True)
    first = enroll(client, 'first-student@example.test')
    body, delivery = ask_for_quiz(client)
    no_keys(body)
    assert 'PRIVATE-RUBRIC-SENTINEL' not in json.dumps(body)
    assert delivery['options'][0] == {'id': 'A', 'text': '기울기의 반대 방향'}
    assert client.get('/api/courses/1/quiz-progress').json()['attempt_count'] == 0
    enroll(client, 'second-student@example.test')
    assert answer(client, delivery, 'A').status_code == 403
    assert client.get('/api/courses/1/quiz-progress').json()['attempt_count'] == 0
    authenticate(client, 'first-student@example.test')
    assert client.post('/api/courses/1/quiz-answers', json={
        'delivery_id': delivery['delivery_id'], 'answer': 'B',
        'student_id': first['id'], 'is_correct': True,
    }).status_code == 422
    result = answer(client, delivery, 'B')
    assert result.status_code == 200, result.text
    assert result.json()['is_correct'] is False
    assert result.json()['correct_answer'] == 'A'
    assert 'PRIVATE-RUBRIC-SENTINEL' in result.json()['explanation']


def test_retries_are_idempotent_and_retakes_do_not_inflate_estimate(client):
    draft(client, publish=True)
    student = enroll(client)
    _, delivery = ask_for_quiz(client)
    first = answer(client, delivery, 'B').json()
    assert first['is_first_attempt'] is True and first['is_correct'] is False
    repeated = answer(client, delivery, 'B')
    assert repeated.status_code == 200 and repeated.json()['attempt_id'] == first['attempt_id']
    assert repeated.json()['replayed'] is True
    assert answer(client, delivery, 'A').status_code == 400
    _, retake = ask_for_quiz(client)
    assert retake['delivery_id'] != delivery['delivery_id']
    graded = answer(client, retake, 'A').json()
    assert graded['is_correct'] is True and graded['is_first_attempt'] is False
    progress = client.get('/api/courses/1/quiz-progress').json()
    assert progress['attempt_count'] == 2 and progress['first_attempt_count'] == 1
    assert progress['retake_count'] == 1
    concept = progress['concepts'][0]
    assert concept['accuracy'] == 0 and concept['status'] == 'insufficient_data'
    authenticate(client)
    _, preview = ask_for_quiz(client)
    assert preview['is_preview'] is True
    assert answer(client, preview, 'A').json()['is_preview'] is True
    insights = client.get('/api/courses/1/quiz-insights').json()
    assert insights['first_attempt_count'] == 1 and insights['student_count'] == 1
    assert {row['student_id'] for row in insights['students']} == {student['id']}


def test_archived_quiz_cannot_be_delivered_or_graded(client):
    quiz = draft(client, publish=True)
    enroll(client)
    _, delivery = ask_for_quiz(client)
    authenticate(client)
    assert client.post(f'/api/courses/1/quizzes/{quiz["id"]}/archive').status_code == 200
    authenticate(client, 'student@example.test')
    assert answer(client, delivery, 'A').status_code == 404
    response = client.post('/api/ask', json={'course_id': 1, 'question': '경사하강법이란?'})
    assert response.json()['quizzes'] == []


def test_first_response_evidence_informs_exam_and_teaching_without_personal_handoff(client):
    authenticate(client)
    course_id = client.post('/api/courses', json={'title': '퀴즈 출제 근거 검증'}).json()['id']
    for title, text in [
        ('경사하강법', '경사하강법은 손실 기울기의 반대 방향으로 이동합니다.'),
        ('학습률', '학습률은 한 번에 이동하는 크기를 조절합니다.'),
    ]:
        assert client.post(f'/api/courses/{course_id}/materials/text', json={'title': title, 'text': text}).status_code in {200, 201}
    draft(client, course_id, publish=True)
    student = enroll(client, 'quiz-personal-sentinel@example.test', course_id)
    _, delivery = ask_for_quiz(client, course_id)
    service = client.app.state.service
    before = service.exam_plan(course_id, 12)
    before_count = next(row['count'] for row in before['topics'] if row['topic'] == '경사하강법')
    assert answer(client, delivery, 'B', course_id).json()['is_first_attempt'] is True
    after = service.exam_plan(course_id, 12)
    target = next(row for row in after['topics'] if row['topic'] == '경사하강법')
    assert target['count'] > before_count
    assert target['quiz_first_attempt_incorrect'] == 1
    assert after['quiz_insights']['first_attempt_count'] == 1
    _, retake = ask_for_quiz(client, course_id)
    assert answer(client, retake, 'A', course_id).json()['is_first_attempt'] is False
    authenticate(client)
    _, preview = ask_for_quiz(client, course_id)
    assert answer(client, preview, 'A', course_id).json()['is_preview'] is True
    insight = client.get(f'/api/courses/{course_id}/quiz-insights').json()
    assert insight['first_attempt_count'] == 1 and insight['retake_count'] == 1
    assert insight['preview_attempt_count'] == 1 and insight['student_count'] == 1
    assert insight['students'][0]['student_id'] == student['id']
    assert insight['students'][0]['student_name'] == 'quiz-personal-sentinel'
    plan = service.exam_plan(course_id, 12)
    brief = service.teaching_brief(course_id)
    for handoff in [plan, brief]:
        no_personal_rows(prompt_data(handoff['prompt']))
        assert 'quiz-personal-sentinel' not in handoff['prompt']
    evidence = prompt_data(plan['prompt'])['quiz_response_evidence']
    assert evidence['first_attempt_count'] == 1 and evidence['retake_count'] == 1
    assert evidence['preview_attempt_count'] == 1
    assert evidence['concepts'][0]['incorrect_count'] == 1
    assert evidence['concepts'][0]['correct_count'] == 0
    assert prompt_data(brief['prompt'])['analytics']['quiz_insights']['first_attempt_count'] == 1


def test_exam_errors_map_approved_sources_once_per_topic_and_support_custom_concepts(client):
    authenticate(client)
    course_id = client.post('/api/courses', json={'title': '출처로 연결하는 출제 주제'}).json()['id']
    for title, text in [
        ('경사하강법', '# 경사하강법\n경사하강법은 기울기의 반대 방향으로 이동합니다.'),
        ('수학 복습', '# 합성함수\n연쇄법칙은 합성 함수의 미분을 계산하는 규칙입니다.'),
        ('학습률', '# 학습률\n학습률은 이동 크기를 결정합니다.'),
    ]:
        assert client.post(f'/api/courses/{course_id}/materials/text', json={'title': title, 'text': text}).status_code in {200, 201}
    materials = client.get(f'/api/courses/{course_id}/materials').json()['materials']
    chain_material = next(row for row in materials if row['title'] == '수학 복습')
    chain_source = client.get(f'/api/courses/{course_id}/materials/{chain_material["id"]}').json()['chunks'][0]
    gradient = quiz_payload(client, course_id)
    gradient['concepts'] = ['경사하강법', '기울기']
    chain = {**gradient, 'title': '연쇄법칙 확인', 'concepts': ['연쇄법칙'],
             'question': '연쇄법칙은 어느 함수의 미분에 사용하나요?',
             'options': ['합성 함수', '상수만 있는 함수'],
             'explanation': '연쇄법칙은 합성 함수의 미분을 계산하는 규칙입니다.',
             'source_chunk_ids': [chain_source['id']]}
    created = []
    for payload in [gradient, chain]:
        result = client.post(f'/api/courses/{course_id}/quizzes', json=payload)
        assert result.status_code == 201, result.text
        quiz = result.json()
        assert client.post(f'/api/courses/{course_id}/quizzes/{quiz["id"]}/publish', json={'expected_revision': quiz['revision']}).status_code == 200
        created.append(quiz)
    enroll(client, course_id=course_id)
    _, delivery = ask_for_quiz(client, course_id)
    assert delivery['quiz_id'] == created[0]['id']
    assert answer(client, delivery, 'B', course_id).json()['is_first_attempt'] is True
    service = client.app.state.service
    topics = {row['topic']: row for row in service.exam_plan(course_id, 12)['topics']}
    assert topics['경사하강법']['quiz_first_attempt_incorrect'] == 1
    assert topics['합성함수']['quiz_first_attempt_incorrect'] == 0
    result = client.post('/api/ask', json={'course_id': course_id, 'question': '연쇄법칙을 설명해줘'})
    assert result.status_code == 200, result.text
    delivery = next(row for row in result.json()['quizzes'] if row['quiz_id'] == created[1]['id'])
    assert answer(client, delivery, 'B', course_id).json()['is_first_attempt'] is True
    topics = {row['topic']: row for row in service.exam_plan(course_id, 12)['topics']}
    assert topics['경사하강법']['quiz_first_attempt_incorrect'] == 1
    assert topics['합성함수']['quiz_first_attempt_incorrect'] == 1
    assert topics['학습률']['quiz_first_attempt_incorrect'] == 0


def test_mcp_instructor_reviews_and_approves_draft_before_student_response(client):
    authenticate(client)
    payload = quiz_payload(client)
    registration, code, teacher = authorize(client, role='instructor')
    teacher_token = exchange(client, registration, code, role='instructor').json()['access_token']
    tools = rpc(client, teacher_token, 'tools/list', role='instructor').json()['result']['tools']
    names = {tool['name'] for tool in tools}
    assert {'propose_concept_quiz', 'list_concept_quizzes', 'get_quiz_insights', 'review_concept_quiz', 'publish_concept_quiz'} <= names
    proposed = tool_payload(rpc(client, teacher_token, 'tools/call', {'name': 'propose_concept_quiz', 'arguments': {'course_id': 1, **payload}}, role='instructor'))
    assert proposed['status'] == 'draft' and '/#quizzes' in proposed['review_url']
    quiz_id = proposed.get('id', proposed.get('quiz_id'))
    denied = rpc(client, teacher_token, 'tools/call', {'name': 'publish_concept_quiz', 'arguments': {'course_id': 1, 'quiz_id': quiz_id, 'review_token': 'unreviewed', 'confirmation': '승인'}}, role='instructor').json()['result']
    assert denied['isError'] is True
    enroll(client)
    registration, code, _ = authorize(client, identity='student@example.test')
    student_token = exchange(client, registration, code).json()['access_token']
    student_tools = rpc(client, student_token, 'tools/list').json()['result']['tools']
    assert not {'review_concept_quiz', 'publish_concept_quiz'} & {tool['name'] for tool in student_tools}
    found = tool_payload(rpc(client, student_token, 'tools/call', {'name': 'search_course_materials', 'arguments': {'course_id': 1, 'question': '경사하강법이란?'}}))
    assert found['quizzes'] == []
    denied = rpc(client, student_token, 'tools/call', {'name': 'propose_concept_quiz', 'arguments': {'course_id': 1, **payload}}).json()['result']
    assert denied['isError'] is True
    denied = rpc(client, student_token, 'tools/call', {'name': 'review_concept_quiz', 'arguments': {'course_id': 1, 'quiz_id': quiz_id}}).json()['result']
    assert denied['isError'] is True
    review = tool_payload(rpc(client, teacher_token, 'tools/call', {'name': 'review_concept_quiz', 'arguments': {'course_id': 1, 'quiz_id': quiz_id}}, role='instructor'))
    assert review['status'] == 'draft' and review['correct_answer'] == 'A'
    assert 'PRIVATE-RUBRIC-SENTINEL' in review['explanation']
    approved = tool_payload(rpc(client, teacher_token, 'tools/call', {'name': 'publish_concept_quiz', 'arguments': {'course_id': 1, 'quiz_id': quiz_id, 'review_token': review['review_token'], 'confirmation': review['confirmation_phrase']}}, role='instructor'))
    assert approved['status'] == 'published' and approved['approval_channel'] == 'mcp'
    found = tool_payload(rpc(client, student_token, 'tools/call', {'name': 'search_course_materials', 'arguments': {'course_id': 1, 'question': '경사하강법이란?'}}))
    no_keys(found)
    assert 'PRIVATE-RUBRIC-SENTINEL' not in json.dumps(found)
    delivery = found['quizzes'][0]
    enroll(client, 'another-student@example.test')
    registration, code, _ = authorize(client, identity='another-student@example.test')
    another_token = exchange(client, registration, code).json()['access_token']
    denied = rpc(client, another_token, 'tools/call', {'name': 'submit_quiz_answer', 'arguments': {'course_id': 1, 'delivery_id': delivery['delivery_id'], 'answer': 'A'}}).json()['result']
    assert denied['isError'] is True
    another = tool_payload(rpc(client, another_token, 'tools/call', {'name': 'get_my_quiz_progress', 'arguments': {'course_id': 1}}))
    assert another['attempt_count'] == 0
    forged = rpc(client, student_token, 'tools/call', {'name': 'submit_quiz_answer', 'arguments': {'course_id': 1, 'delivery_id': delivery['delivery_id'], 'answer': 'B', 'student_id': teacher['id'], 'is_correct': True}}).json()['result']
    if not forged.get('isError'):
        result = forged.get('structuredContent') or json.loads(forged['content'][0]['text'])
        assert result['is_correct'] is False and result['is_preview'] is False
    grade = tool_payload(rpc(client, student_token, 'tools/call', {'name': 'submit_quiz_answer', 'arguments': {'course_id': 1, 'delivery_id': delivery['delivery_id'], 'answer': 'B'}}))
    assert grade['is_correct'] is False
    own = tool_payload(rpc(client, student_token, 'tools/call', {'name': 'get_my_quiz_progress', 'arguments': {'course_id': 1}}))
    assert own['first_attempt_count'] == 1
    assert 'students' not in own
    insight = tool_payload(rpc(client, teacher_token, 'tools/call', {'name': 'get_quiz_insights', 'arguments': {'course_id': 1}}, role='instructor'))
    assert insight['student_count'] == 1


def test_mcp_review_approval_requires_same_teacher_current_revision_and_one_use(client):
    quiz = draft(client)
    registration, code, teacher = authorize(client, role='instructor')
    teacher_token = exchange(client, registration, code, role='instructor').json()['access_token']
    review_args = {'course_id': 1, 'quiz_id': quiz['id']}
    review = tool_payload(rpc(client, teacher_token, 'tools/call', {'name': 'review_concept_quiz', 'arguments': review_args}, role='instructor'))
    approval = {**review_args, 'review_token': review['review_token'], 'confirmation': review['confirmation_phrase']}
    registration, code, other = authorize(client, identity='other-teacher@example.test', role='instructor')
    other_token = exchange(client, registration, code, role='instructor').json()['access_token']
    # Both synthetic teachers legitimately manage this course; the review still belongs to its reviewer.
    with client.app.state.service._db() as db:
        db.execute("INSERT INTO course_members(course_id,user_id,role) VALUES(?,?,'teacher')", (1, other['id']))
    denied = rpc(client, other_token, 'tools/call', {'name': 'publish_concept_quiz', 'arguments': approval}, role='instructor').json()['result']
    assert denied['isError'] is True
    authenticate(client)
    edited = client.put(f'/api/courses/1/quizzes/{quiz["id"]}', json=quiz_payload(client))
    assert edited.status_code == 200 and edited.json()['revision'] > review['revision']
    denied = rpc(client, teacher_token, 'tools/call', {'name': 'publish_concept_quiz', 'arguments': approval}, role='instructor').json()['result']
    assert denied['isError'] is True
    fresh = tool_payload(rpc(client, teacher_token, 'tools/call', {'name': 'review_concept_quiz', 'arguments': review_args}, role='instructor'))
    approval = {**review_args, 'review_token': fresh['review_token'], 'confirmation': '승인'}
    denied = rpc(client, teacher_token, 'tools/call', {'name': 'publish_concept_quiz', 'arguments': approval}, role='instructor').json()['result']
    assert denied['isError'] is True
    approval['confirmation'] = fresh['confirmation_phrase']
    published = tool_payload(rpc(client, teacher_token, 'tools/call', {'name': 'publish_concept_quiz', 'arguments': approval}, role='instructor'))
    assert published['status'] == 'published' and published['approved_by'] == teacher['id']
    assert published['approval_channel'] == 'mcp'
    replay = rpc(client, teacher_token, 'tools/call', {'name': 'publish_concept_quiz', 'arguments': approval}, role='instructor').json()['result']
    assert replay['isError'] is True
