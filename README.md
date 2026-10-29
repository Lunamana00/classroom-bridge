# 수업 사이 · CLASSROOM BRIDGE

학생이 사용하는 LLM을 교수의 강의자료와 연결하고, 누적된 질문을 수업 준비에 활용하는 웹 프로토타입입니다. 자료 등록 → 질문 → 근거 선별 → 출처 확인 → 교수의 질문 현황 조회까지 한 화면에서 체험할 수 있습니다.

## 바로 실행

Python 3.11 이상이 필요합니다. 저장소를 내려받은 뒤 프로젝트 폴더에서 실행합니다.

```bash
cd classroom-bridge
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env.local
# 아래 Google 로그인 설정에 따라 .env.local을 채운 뒤 실행합니다.
.venv/bin/python -m classroom
```

브라우저에서 [http://127.0.0.1:8000](http://127.0.0.1:8000)을 엽니다. Google 로그인 설정을 마친 뒤 허용된 교수 계정으로 로그인하면 예시 강의자료와 질문이 담긴 데모 수업으로 시작할 수 있습니다. 학생은 교수의 초대 링크로 수업에 참여합니다. 기본 설정은 FactChat API 키가 있으면 `gpt-6-luna`, 없으면 단어 기반 데모 검색을 사용하며, 데모 검색에서도 로그인과 수업 권한 검사는 적용됩니다.

1. 교수 화면에서 수업을 선택하거나 새로 만듭니다.
2. TXT·MD·텍스트가 포함된 PDF를 업로드하거나 강의 내용을 붙여 넣습니다. 파일당 최대 10MB이며, 스캔 PDF의 OCR은 지원하지 않습니다.
3. 학생 화면에서 “경사하강법이 뭐야?”처럼 질문합니다. 선별된 자료, 출처, 외부 LLM에 전달할 답변 지시문을 확인합니다.
4. 교수 화면에서 질문 주제, 자료로 답할 수 없었던 질문, 보충 설명 지시문과 시험 출제안을 확인합니다.

데모 질문은 실제로 새로 입력한 질문과 구분해서 표시합니다. 데이터는 기본적으로 `data/classroom.sqlite`에 저장되며 서버를 재시작해도 유지됩니다.

## 자료 선별 방식 선택

후보 자료의 관련성과 답변 근거를 공식 OpenAI Python SDK로 FactChat 게이트웨이에 요청해 판단합니다. GPT는 `responses.create()`, Jev는 SDK의 `post()`로 `/decisions/`를 호출합니다. `.env.local`을 수정한 뒤 웹 서버를 재시작하세요.

```dotenv
CLASSROOM_MODE=auto
OPEN_API_KEY=your-api-key
OPENAI_BASE_URL=https://factchat-cloud.mindlogic.ai/v1/gateway
CODEX_MODEL=gpt-6-luna
JEV_MODEL=jev-1.14.0
```

- `CLASSROOM_MODE=auto`: API 키가 있으면 `codex` 모드의 `gpt-6-luna`, 없으면 데모 검색을 사용합니다.
- `CLASSROOM_MODE=codex`: FactChat Responses API로 `CODEX_MODEL`을 호출합니다. 기본값은 `gpt-6-luna`입니다.
- `CLASSROOM_MODE=jev`: FactChat Decisions API로 `JEV_MODEL`을 호출합니다. 기본값은 `jev-1.14.0`입니다. 게이트웨이의 `state`·`questions` 형식으로 관련성·근거·주입 여부와 전체 충분성을 판단합니다. 실패 시 모델을 자동 교체하지 않습니다.
- `CLASSROOM_MODE=demo`: API 호출 없이 단어 기반 검색으로 체험합니다.

`codex`·`jev`는 호환성을 위해 유지한 모드 이름입니다. Codex CLI 설치·로그인과 TypeSafe 키는 사용하지 않습니다. 키는 `OPEN_API_KEY` 또는 표준 이름인 `OPENAI_API_KEY`로 설정할 수 있으며, 둘 다 있으면 `OPEN_API_KEY`를 사용합니다. 설정 우선순위는 실행 환경변수 → `.env.local` → `.env`입니다.

학생이 자료 검색을 요청할 때 질문과 후보 자료를 FactChat 게이트웨이에 전달합니다. 기본 주소는 `https://factchat-cloud.mindlogic.ai/v1/gateway`이며 `OPENAI_BASE_URL`로 변경할 수 있습니다. 도구를 제공하지 않으며, JSON Schema 결과의 자료 ID·점수·충분성을 검사하고 출처 원문을 보존합니다. GPT Responses 호출은 `store=false`를 사용하며, Jev는 Decisions API의 상태 보관 정책을 따릅니다. 실패는 오류로 표시하고 다른 모델이나 데모로 자동 전환하지 않습니다. 키는 서버에서만 사용하고 브라우저와 오류 메시지에 노출하지 않습니다. `.env`와 `.env.local`은 Git 추적 대상에서 제외됩니다.

모든 모드에서 앱의 답변 미리보기는 **자료 발췌**입니다. Codex와 Jev는 이 앱에서 근거 선별을 담당하고, 최종 자연어 답변은 MCP에 연결된 GPT·Claude·Gemini 등의 LLM이 작성합니다.

## Google 로그인과 수업 권한

Google Cloud Console의 Google Auth Platform에서 웹 애플리케이션 OAuth 클라이언트를 생성합니다. 로컬 테스트용 승인된 리디렉션 URI는 다음과 같습니다.

- `http://127.0.0.1:8000/auth/google/callback`
- `http://localhost:8000/auth/google/callback`

프로젝트의 `.env.local`에 설정한 뒤 서버를 재시작합니다. Google 클라이언트 시크릿은 채팅이나 Git에 넣지 않습니다.

```dotenv
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
CLASSROOM_TEACHER_EMAILS=teacher@example.edu
CLASSROOM_BASE_URL=http://127.0.0.1:8000
```

Google Auth Platform에서 Branding과 Audience를 설정하고, External 테스트 상태에서는 로그인할 계정을 테스트 사용자에 등록합니다. 학교 Google Workspace 내부 사용자만 받을 경우 Internal을 선택할 수 있습니다. 범위는 `openid email profile`만 요청합니다. 서버 리디렉션 방식이므로 Authorized JavaScript origins는 필요하지 않습니다. Google 로그인은 Chrome·Safari 등 일반 브라우저에서 진행하세요.

교수 이메일은 쉼표로 구분합니다. 이메일이 인증된 Google 계정으로 로그인하며, 서버가 교수 허용 목록과 수업별 소속을 확인합니다. 학생은 초대 링크로 참여한 수업의 자료 조회·질문만 사용할 수 있습니다. 자료 변경·전체 질문 통계·시험 계획·초대·참여자 관리는 해당 수업의 담당 교수만 사용할 수 있습니다. 교수는 새 수업을 만들면 자동으로 담당자로 등록됩니다. 로그인 도입 전에 만들어진 수업은 **처음 로그인한 허용 교수**에게 귀속됩니다.

교수 화면의 학생 초대 버튼으로 7일 유효 링크를 만듭니다. 재발급·폐기할 수 있고 재발급하면 이전 링크가 만료됩니다. 참여한 학생의 권한은 링크 만료와 별개로 유지됩니다. 참여자 관리에서 학생을 제외하면 소속을 제거하고 기존 초대 링크도 폐기합니다. 다시 참여하려면 새 링크가 필요합니다.

로그인 세션은 7일 유효하며 서버 DB에 토큰의 해시만 저장합니다. 로그아웃 시 서버에서 폐기하며, 변경 요청은 CSRF 토큰을 검사합니다. Google OAuth 상태 쿠키의 서명 키는 `CLASSROOM_SESSION_SECRET`으로 지정하거나 서버 DB에 한 번 생성해 보관합니다. 개발 서버는 localhost에 바인딩하며, 운영 서버는 nginx의 HTTPS 프록시를 통해 제공합니다. 실제 Google 계정 로그인과 MCP 클라이언트 승인은 계정 소유자가 확인해야 합니다.

## MCP OAuth로 연결

OAuth를 지원하는 MCP 클라이언트에 다음 **Streamable HTTP** 주소를 추가합니다.

- 학생: `https://canigetbus.app/mcp/student`
- 교수: `https://canigetbus.app/mcp/instructor`

클라이언트가 서버의 인증 정보를 자동 탐색하고 등록한 뒤 Google 로그인과 연결 승인 화면을 엽니다. 로그인한 계정·클라이언트 이름·돌아갈 주소·학생/교수 권한을 확인하고 승인합니다. 교수용 연결은 교수 허용 목록에 있는 계정만 승인할 수 있습니다. 수업 소속과 담당 교수 권한은 도구 호출마다 다시 검사합니다. 학생은 참여하지 않은 수업을 조회하거나 교수 도구를 사용할 수 없습니다.

Google의 토큰은 MCP 클라이언트에 전달하지 않습니다. 앱이 MCP용 토큰을 별도로 발급하며, 토큰은 학생·교수 MCP 주소에 각각 묶입니다. PKCE S256, 2분 유효 일회용 코드, 1시간 접근 토큰, 최대 30일 갱신 토큰을 사용합니다. 갱신 시 토큰을 교체하고 이전 갱신 토큰의 재사용이 감지되면 해당 연결 전체를 폐기합니다. 워크스페이스의 **AI 연결 관리 → 연결 해제**에서도 토큰을 즉시 폐기할 수 있습니다. 웹 로그아웃과 AI 연결 해제는 별개입니다.

클라이언트 등록은 DCR(`POST /register`)로 지원합니다. 공개 클라이언트와 `client_secret_post`·`client_secret_basic`을 지원하며, 정확히 등록한 HTTPS 또는 loopback 리디렉션 주소만 허용합니다. CIMD URL을 원격으로 가져오는 등록 방식은 지원하지 않습니다. stdio는 계정 경계를 우회하지 않도록 지원하지 않으며, HTTPS MCP 주소를 사용합니다. 제품별 클라이언트 설정 형식은 다를 수 있습니다.

로컬 개발에서는 도메인 대신 `http://127.0.0.1:8000`을 사용합니다. 배포할 때 Google OAuth 클라이언트에 `https://canigetbus.app/auth/google/callback`을 추가해야 합니다. 새 MCP 클라이언트마다 Google Cloud Console 클라이언트를 만들 필요는 없습니다.

검증은 합성 계정으로 공식 MCP Python SDK의 자동 탐색·등록·PKCE 교환·도구 호출을 수행하고, 잘못된 verifier·다른 리소스·코드 재사용·만료·학생의 교수 권한 요청·수업 권한 제거·토큰 폐기를 확인합니다. 실제 Google 계정 로그인과 개별 ChatGPT/Claude/Codex 제품의 브라우저 승인 동작은 별도로 확인해야 합니다.

## 결과 해석

질문 빈도는 학생들이 관심을 갖거나 어려움을 느끼는 **신호**입니다. 질문만으로 개별 학생의 이해도나 성적을 확정하지 않습니다. 교수의 보충 설명·시험 출제 결과는 근거와 함께 외부 LLM에 넘기는 지시문 및 설계안입니다. 완성된 시험이나 실제 성적 예측은 아니며, 난이도 비율을 정해도 성적이 정규분포를 따른다고 보장할 수 없습니다.

자료와 질문은 수업별로 조회합니다. 자료에 근거가 없으면 답할 수 없음을 표시합니다. 교수의 자료와 학생 질문은 신뢰할 수 없는 입력으로 취급하며, 그 안의 문장을 시스템 지시로 실행하지 않습니다.

## 개발 및 확인

```bash
CLASSROOM_MODE=demo .venv/bin/python -m pytest
```

요청·응답 형식은 [API_CONTRACT.md](API_CONTRACT.md)에 정리되어 있습니다. `.env.example`의 `CLASSROOM_DB`로 저장 위치를 바꿀 수 있습니다. 배포 구성과 운영 경로는 [deploy/README.md](deploy/README.md)에 정리했습니다. 실제 Google 로그인과 사용할 MCP 제품의 연결은 해당 계정에서 확인해야 합니다.

상대 DB 경로는 실행 위치와 관계없이 프로젝트 폴더 기준으로 해석합니다. 검증한 의존성 버전은 `requirements.lock`에 기록했습니다. 데스크톱·모바일 화면에서 수업 생성, 파일·텍스트 등록, 질문과 출처 열기, 질문 현황 반영, 지시문 복사, 자료 삭제를 확인했습니다.
