# 수업 사이 · CLASSROOM BRIDGE

학생이 사용하는 LLM을 교수의 강의자료와 연결하고, 누적된 질문을 수업 준비에 활용하는 로컬 프로토타입입니다. 자료 등록 → 질문 → 근거 선별 → 출처 확인 → 교수의 질문 현황 조회까지 한 화면에서 체험할 수 있습니다.

## 바로 실행

Python 3.11 이상이 필요합니다. 저장소를 내려받은 뒤 프로젝트 폴더에서 실행합니다.

```bash
cd classroom-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python -m classroom
```

브라우저에서 [http://127.0.0.1:8000](http://127.0.0.1:8000)을 엽니다. 예시 강의자료와 질문이 담긴 데모 수업으로 시작할 수 있습니다. 기본 설정은 Jev API 키가 있으면 Jev, 없으면 설치된 Codex CLI, 둘 다 없으면 단어 기반 데모 검색을 사용합니다.

1. 교수 화면에서 수업을 선택하거나 새로 만듭니다.
2. TXT·MD·텍스트가 포함된 PDF를 업로드하거나 강의 내용을 붙여 넣습니다. 파일당 최대 10MB이며, 스캔 PDF의 OCR은 지원하지 않습니다.
3. 학생 화면에서 “경사하강법이 뭐야?”처럼 질문합니다. 선별된 자료, 출처, 외부 LLM에 전달할 답변 지시문을 확인합니다.
4. 교수 화면에서 질문 주제, 자료로 답할 수 없었던 질문, 보충 설명 지시문과 시험 출제안을 확인합니다.

데모 질문은 실제로 새로 입력한 질문과 구분해서 표시합니다. 데이터는 기본적으로 `data/classroom.sqlite`에 저장되며 서버를 재시작해도 유지됩니다.

## 자료 선별 방식 선택

Codex CLI와 Jev API 중에서 후보 자료의 관련성과 답변 근거를 판단할 도구를 선택할 수 있습니다. `.env`를 수정한 뒤 서버를 다시 시작하세요. 이미 실행 중인 stdio MCP 서버도 클라이언트에서 재시작해야 새 설정을 읽습니다.

`.env`의 기본 설정은 다음과 같습니다.

```dotenv
CLASSROOM_MODE=auto
TYPESAFE_API_KEY=
JEV_MODEL=jev-latest
CODEX_BIN=codex
CODEX_MODEL=
```

- `CLASSROOM_MODE=auto`: 비어 있지 않은 `TYPESAFE_API_KEY`가 있으면 Jev를 우선 사용합니다. 키가 없으면 설치된 Codex CLI를 사용하고, CLI도 없으면 데모 모드를 사용합니다.
- `CLASSROOM_MODE=jev`: Jev만 사용합니다. `TYPESAFE_API_KEY`가 필요합니다.
- `CLASSROOM_MODE=codex`: Jev 키가 있어도 Codex CLI를 사용합니다. CLI를 찾지 못하면 오류를 표시합니다.
- `CLASSROOM_MODE=demo`: Codex나 Jev 호출 없이 단어 기반 검색으로 체험합니다. 자동 검사도 이 모드를 사용합니다.

### Codex CLI

기존 Codex 로그인 상태를 사용하므로 CLI를 사용할 컴퓨터에서 로그인이 되어 있어야 합니다. 이 방식에는 별도 Jev 키가 필요하지 않습니다.

- `CODEX_BIN`: Codex 실행 파일 이름 또는 절대 경로입니다. 데스크톱 클라이언트가 실행 경로를 찾지 못하면 절대 경로로 지정하세요.
- `CODEX_MODEL`: 비워 두면 CLI 기본 모델을 사용합니다. 개인 Codex 설정 파일의 모델은 상속하지 않으므로 특정 모델을 원하면 여기에 지정하세요.

근거 선별용 실행은 개인 설정과 규칙을 불러오지 않고, 별도 임시 폴더에서 읽기 전용으로 수행합니다. 결과는 정해진 JSON 형식으로 받아 자료 ID와 판단 값을 검사합니다.

### Jev API

`.env`의 `TYPESAFE_API_KEY`에 발급받은 키를 넣습니다. 기본 모델은 `JEV_MODEL=jev-latest`입니다. `auto` 모드에서는 키를 넣고 서버를 재시작하면 Jev로 전환됩니다. 기존 Codex CLI로 돌아가려면 `CLASSROOM_MODE=codex`를 사용하거나 `auto` 상태에서 Jev 키를 비웁니다.

Codex·Jev 모드는 사용자의 자료 검색 요청이 있을 때 학생 질문과 후보 강의자료를 해당 서비스에 전달합니다. 주기적으로 자동 호출하지 않습니다. 호출 실패는 오류로 표시하며, 다른 선별 방식으로 몰래 바꾸지 않습니다. 로그인 정보와 API 키는 서버에서만 사용하고 브라우저·오류 메시지·로그에 노출하지 않습니다. `.env`는 Git 추적 대상에서 제외되어 있으므로 키는 각 실행 환경에서 따로 설정하세요.

모든 모드에서 앱의 답변 미리보기는 **자료 발췌**입니다. Codex와 Jev는 이 앱에서 근거 선별을 담당하고, 최종 자연어 답변은 MCP에 연결된 GPT·Claude·Gemini 등의 LLM이 작성합니다.

## LLM에서 MCP 사용

학생용과 교수용 도구를 나누어 제공합니다. UI를 사용하지 않고도 MCP 클라이언트에서 같은 로컬 데이터베이스를 이용할 수 있습니다.

| 역할 | 도구 |
| --- | --- |
| 학생 | `list_courses`, `search_course_materials` |
| 교수 | `list_courses`, `get_question_insights`, `get_teaching_brief`, `prepare_exam_plan`, `add_course_material` |

### 로컬 stdio 연결

`mcpServers` 설정을 지원하는 클라이언트에서 해당 역할의 예제를 참고합니다.

- [학생 설정](examples/mcp-student.json)
- [교수 설정](examples/mcp-instructor.json)

예제의 `/ABSOLUTE/PATH/classroom-bridge`를 실제 설치 폴더의 절대 경로로 바꾸세요. Python 경로와 `CLASSROOM_DB` 경로를 모두 수정해야 합니다. 서버는 프로젝트의 `.env`를 읽으므로 API 키를 예제 설정 파일에 넣을 필요가 없습니다. 설정 파일 위치와 허용되는 항목은 클라이언트마다 다릅니다.

프로토타입에서는 MCP SDK로 HTTP·stdio 연결을 확인했고, 로그인된 Codex CLI의 실제 근거 선별 호출도 확인했습니다. Codex CLI를 학생용 MCP 클라이언트로 연결해 수업 조회 → 질문 도구 호출 → 출처가 있는 자연어 답변 → 질문 1회 기록까지 검증했습니다. Jev 요청·응답 처리는 모의 응답 테스트로 확인하며, 실제 Jev 키를 사용한 연결은 별도 확인이 필요합니다. ChatGPT·Gemini 등 개별 제품의 연결 설정도 별도로 확인해야 합니다.

직접 실행할 때는 다음 명령을 사용합니다. stdio 서버는 MCP 클라이언트가 실행하고 통신하는 프로그램이므로 일반 웹페이지처럼 열리지 않습니다.

```bash
cd classroom-bridge
.venv/bin/python -m classroom.mcp_server --role student
# 교수용은 --role instructor
```

학생용 설정에는 교수 도구가 제공되지 않습니다. 다만 현재 역할 구분과 교수 화면은 로컬 시연용이며, 사용자 인증이나 수업별 접근 권한을 구현한 것은 아닙니다.

### HTTP 연결

웹 서버 실행 중 Streamable HTTP MCP를 지원하는 클라이언트에서 다음 주소를 사용합니다.

- 학생: `http://127.0.0.1:8000/mcp/student`
- 교수: `http://127.0.0.1:8000/mcp/instructor`

주소 뒤에 `/mcp`를 추가하지 않습니다. 일반 ChatGPT·Gemini 웹 대화가 자동으로 MCP를 지원하거나, 클라우드 서비스에서 내 컴퓨터의 `localhost`에 접근할 수 있는 것은 아닙니다. 원격 서비스와 연결하려면 해당 서비스가 지원하는 연결 방식에 맞춰 HTTPS 주소와 인증을 갖춘 배포가 필요합니다. 이 프로토타입은 로컬 시연 범위입니다.

Codex용 서버 설정은 [TOML 예제](examples/codex-student.toml)를 참고하세요. 질문 검색은 질문 기록과 선택된 선별 도구의 호출을 포함하므로 클라이언트에서 도구 사용 승인 검토가 발생할 수 있습니다. 조회 도구와 기록 도구의 성격을 MCP 메타데이터로 구분해 제공합니다.

### LLM에 전달할 지시문

- [학생 답변 지시문](instructions/student.md): 수업 선택, 근거 조회, 출처 표시, 근거가 부족한 경우의 응답 방식
- [교수 분석 지시문](instructions/instructor.md): 질문 현황 해석, 보충 설명, 시험 출제안 사용 방식

이 파일들은 복사해서 사용하는 지시문입니다. 파일이 있다는 이유로 LLM에 자동 등록되거나 실행되지는 않습니다.

## 결과 해석

질문 빈도는 학생들이 관심을 갖거나 어려움을 느끼는 **신호**입니다. 질문만으로 개별 학생의 이해도나 성적을 확정하지 않습니다. 교수의 보충 설명·시험 출제 결과는 근거와 함께 외부 LLM에 넘기는 지시문 및 설계안입니다. 완성된 시험이나 실제 성적 예측은 아니며, 난이도 비율을 정해도 성적이 정규분포를 따른다고 보장할 수 없습니다.

자료와 질문은 수업별로 조회합니다. 자료에 근거가 없으면 답할 수 없음을 표시합니다. 교수의 자료와 학생 질문은 신뢰할 수 없는 입력으로 취급하며, 그 안의 문장을 시스템 지시로 실행하지 않습니다.

## 개발 및 확인

```bash
CLASSROOM_MODE=demo .venv/bin/python -m pytest
```

요청·응답 형식은 [API_CONTRACT.md](API_CONTRACT.md)에 정리되어 있습니다. `.env.example`의 `CLASSROOM_DB`로 저장 위치를 바꿀 수 있습니다. 실제 서비스로 운영하기 전에는 인증, 수업 접근 권한과 배포 구성이 추가로 필요합니다.

상대 DB 경로는 실행 위치와 관계없이 프로젝트 폴더 기준으로 해석합니다. 검증한 의존성 버전은 `requirements.lock`에 기록했습니다. 데스크톱·모바일 화면에서 수업 생성, 파일·텍스트 등록, 질문과 출처 열기, 질문 현황 반영, 지시문 복사, 자료 삭제를 확인했습니다.
