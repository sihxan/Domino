# Domino — Phase 0 (인사이트 발행물)

매일 06:00 KST에 근거·흐름이 붙은 리포트를 발행하는 시스템. 지금은 Phase 0(발행 시작)만 만든다.

## 명세 (진실의 원천)
- `docs/spec/tech-phase0.md` — 범위, 토폴로지, 레포 구조, DDL(Postgres·Neo4j), 파이프라인 단계, 스케줄·운영, 테스트, 12주 순서
- `docs/spec/code-phase0.md` — 패키지별 시그니처, 프롬프트 전문, 도구 계약, agent 정의, 스키마, 게이트
- `docs/tasks/week-N.md` — 이번 주 작업 목록 (프롬프트 A가 만든다)
- `docs/spec/decisions.md` — 명세에 없어서 네가 내린 결정의 기록 (아래 규칙 6)

명세와 코드가 다르면 명세가 맞다. 명세를 고쳐야 한다고 판단되면 코드를 고치지 말고 decisions.md에 적고 멈춰라.

## 절대 규칙
1. 의존 방향은 `core ← ingest ← extract ← resolve ← graph ← signals ← agent ← report ← eval`. 역방향 import 금지 (`import-linter`가 막는다).
2. Neo4j 읽기 쿼리는 전부 `asof` 인자를 받고 쿼리 파일에 `ASOF_EDGE` 토큰이 있어야 한다. 없는 쿼리는 쓰지 마라.
3. Neo4j에서 DELETE 금지. 변경은 새 레코드 + `expired_at`/`invalid_at`.
4. LLM 호출은 `domino_core.clients.llm.call` 하나로만. `anthropic`을 직접 import하지 마라. 모델명은 `settings`에서만 읽는다.
5. 외부 I/O(httpx·neo4j·redis·boto3)는 `domino_core.clients`를 통해서만. 비즈니스 패키지에서 직접 import 금지.
6. 명세에 없는 결정이 필요하면: 가장 보수적인 선택을 하고 `docs/spec/decisions.md`에 `- [날짜] [패키지] 결정: … / 이유: … / 대안: …` 한 줄을 남긴다. 나에게 묻지 말고 진행하되 기록은 반드시.
7. 테스트 먼저. 태스크마다 `tests/`에 실패하는 테스트를 먼저 쓰고 통과시킨다. 테스트 없는 구현은 완료가 아니다.
8. 비밀·키를 코드·로그·테스트에 넣지 마라. `.env.example`에는 키 이름만.
9. 새 의존성은 `pyproject.toml`에 추가하고 decisions.md에 이유 한 줄. 버전은 고정한다.
10. 투자 권유 표현(매수·매도·추천·목표가·확실히)은 프롬프트·템플릿·테스트 데이터 어디에도 쓰지 마라.

## 명령
- `make up` — docker compose up -d (postgres·neo4j·redis·minio·embedder·prefect·langfuse)
- `make migrate` — alembic upgrade head + Neo4j DDL 멱등 실행
- `make seed` — sources/·theses/·storylines/ 적재
- `make test` — ruff + mypy --strict + import-linter + pytest (unit+integration)
- `make golden` — gold set 회귀 (LLM 실호출, 비용 발생 — 프롬프트·모델 변경 시만)
- `make report-dry-run DATE=YYYY-MM-DD` — 발행 파이프라인 전체, 발송만 생략

## 완료 정의 (태스크 단위)
- `make test` 초록
- 명세의 시그니처·테이블·필드 이름과 일치 (이름을 바꾸지 마라)
- 새 모듈에 docstring 1줄 + 타입 힌트 전부
- `docs/tasks/week-N.md`의 해당 항목에 `[x]`와 커밋 해시

## 작업 방식
- 세션당 패키지 하나. 다른 패키지를 고쳐야 하면 멈추고 이유를 말해라.
- 큰 파일(500줄 이상)을 한 번에 쓰지 말고 함수 단위로 쓰고 테스트하라.
- 커밋은 태스크 단위, 메시지는 `feat(ingest): simhash + story assignment (week-1 #3)` 형식.
- subagent를 띄울 때 모델은 sonnet. opus는 사용자가 명시할 때만.
- 막히면 같은 접근을 세 번 반복하지 말고 멈춰서 "무엇을 시도했고 무엇이 막혔는지"를 보고하라.

## 스택 고정
Python 3.12 · uv · Pydantic v2 · SQLAlchemy 2 (Core) · alembic · neo4j 5 driver · Prefect 3 · claude-agent-sdk · FastAPI · Next.js 15 · pytest · testcontainers
