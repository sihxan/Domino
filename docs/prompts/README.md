# Claude Code 프롬프트 세트 — Phase 0 구현용

Nov 4, 2026 · @KDST

## 0. 사용 순서

1. **명세서를 파일로.** 이 Doc의 "기술 명세서 Phase 0"·"코드 명세서 Phase 0" 탭을 각각 Markdown으로 다운로드해 레포의 `docs/spec/tech-phase0.md`, `docs/spec/code-phase0.md`에 둔다. Claude Code는 Doc을 못 읽고 파일만 읽는다.
2. **CLAUDE.md**(§1)를 레포 루트에 둔다. 매 세션 자동으로 읽힌다 — 규칙은 여기에, 작업은 프롬프트에.
3. **매주 월요일**: 프롬프트 A(§2)를 **Opus · plan 모드**로. 산출은 `docs/tasks/week-N.md` 하나.
4. **작업 세션**: 프롬프트 B(§3)를 **Sonnet**으로, 세션당 패키지 하나·태스크 1\~3개. 새 세션마다 새로 시작(컨텍스트 비움).
5. **주말**: 프롬프트 C(§4)를 Opus로. 리뷰 결과는 다음 주 A의 입력.
6. Sonnet이 같은 태스크에서 두 번 실패하면 그 태스크만 프롬프트 D(Opus)로.
7. Week 2·6·11의 프롬프트 튜닝은 E(Opus).

| 프롬프트 | 모델 | 모드 | 빈도 |
| --- | --- | --- | --- |
| A 킥오프 | Opus | plan → 승인 후 실행(`week-N.md` 작성만) | 주 1 |
| B 구현 | Sonnet | 일반 | 매일 1\~3 세션 |
| C 리뷰 | Opus | 읽기 전용 지시 | 주 1 |
| D 디버그 | Opus | 일반 | 필요 시 |
| E 튜닝 | Opus | 일반 | Week 2·6·11 |

## 1. `CLAUDE.md`

```markdown
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
```

## 2. 프롬프트 A — 주차 킥오프 (Opus, plan 모드)

```markdown
이번 주는 Week {N}이다. 다음을 읽어라:
- docs/spec/tech-phase0.md §8.2 (12주 구현 순서)의 Week {N} 행과 "주말 기준 상태"
- docs/spec/code-phase0.md 중 이번 주에 해당하는 § 전부
- docs/tasks/week-{N-1}.md의 미완료 항목과 docs/review/week-{N-1}.md의 지적 사항 (있으면)
- docs/spec/decisions.md

할 일: docs/tasks/week-{N}.md를 작성해라. 코드는 쓰지 마라.

형식:
# Week {N} — {주말 기준 상태 한 줄}
## 태스크 (의존 순서대로)
- [ ] T1 — {패키지} — {한 줄 설명}
  - 명세: code-phase0.md §x.y, tech-phase0.md §z
  - 산출물: {파일 경로들}
  - 완료 기준: {테스트 파일/케이스 이름, make 명령, 관측 가능한 상태}
  - 추정: {S/M/L} · 모델: {sonnet|opus}
  - 선행: {T?}
- [ ] T2 …
## 이번 주 리스크
- {명세가 모호하거나 외부 의존(SDK 버전·API)이 불확실한 곳과, 그걸 어떻게 확인할지}
## 범위 축소 시 버릴 순서
1. {…} 2. {…}

규칙:
- 태스크는 1세션(2~3시간)에 끝나는 크기. L은 쪼개라.
- 각 태스크의 완료 기준은 테스트 이름 또는 make 명령이어야 한다. "동작한다"는 금지.
- 명세와 충돌하거나 명세에 없는 결정이 필요한 태스크는 "리스크"에 적고 decisions.md에 미리 결정을 기록하라.
- 모델 배정: SDK 연동·Neo4j bi-temporal·agent runner·프롬프트 튜닝·디버깅은 opus, 나머지는 sonnet.
- 이번 주 범위가 1인 주 25시간을 넘으면 tech-phase0.md의 축소 규칙대로 줄이고 "버릴 순서"에 명시하라.
```

## 3. 프롬프트 B — 패키지 구현 (Sonnet)

### 3.1 템플릿

```markdown
docs/tasks/week-{N}.md의 {T번호들}을 구현한다. 패키지: {패키지명}. 다른 패키지는 수정하지 마라.

먼저 읽어라 (전부, 요약하지 말고):
- docs/spec/code-phase0.md §{해당 섹션}
- docs/spec/tech-phase0.md §{데이터 모델 또는 파이프라인 해당 섹션}
- docs/spec/decisions.md
- 이미 있는 코드: packages/{패키지명}/ 와 packages/domino_core/ (인터페이스만)

순서:
1. 태스크마다 tests/에 실패하는 테스트를 먼저 쓴다. 테스트 케이스는 code-phase0.md §7.7의 해당 파일 목록을 기준으로 하고, 명세의 예시 입출력을 그대로 fixture로 쓴다.
2. 구현한다. 함수 시그니처·필드 이름·테이블 이름은 명세와 글자 단위로 같아야 한다.
3. `make test`를 돌린다. 초록이 될 때까지 고친다. 세 번 같은 실패면 멈추고 보고한다.
4. 태스크마다 커밋한다: `feat({패키지}): … (week-{N} {T번호})`.
5. week-{N}.md의 해당 항목을 [x]로 바꾸고 커밋 해시를 적는다.

하지 마라:
- 명세에 없는 기능 추가, 이름 변경, "개선". 모호하면 CLAUDE.md 규칙 6대로 decisions.md에 적고 보수적으로.
- 실제 LLM·외부 API 호출을 테스트에 넣는 것. clients를 mock해라.
- 다른 패키지 파일 수정. 필요하면 멈추고 이유를 말해라.

끝나면 보고: 완료한 T, 남은 T, decisions.md에 추가한 줄, 명세에서 틀렸다고 생각한 부분(수정하지 말고 보고만).
```

### 3.2 실제로 채운 예 — Week 1 첫 세션

```markdown
docs/tasks/week-1.md의 T1~T3을 구현한다. 패키지: domino_core (T1), 레포 루트 (T2), domino_ingest (T3). 이 세션은 예외적으로 core와 루트를 같이 만든다 — 아직 아무것도 없기 때문이다.

먼저 읽어라 (전부):
- docs/spec/tech-phase0.md §1.1 (서비스 목록), §2 (레포 구조·settings·ID·시간), §3.1 (수집 DDL)
- docs/spec/code-phase0.md §0 (규약·core), §1.1~1.5 (ingest)

T1 domino_core:
- pyproject.toml (uv workspace, ruff, mypy strict, import-linter 계약: core←ingest←extract←resolve←graph←signals←agent←report←eval)
- packages/domino_core/{settings.py, ids.py, timeutil.py, errors.py, models.py, clients/{llm.py, embed.py, db.py, neo.py, redis.py, s3.py}}
- clients는 인터페이스와 재시도·에러 매핑까지만. llm.call은 anthropic SDK를 쓰되 예산 카운터·Langfuse trace·structured output 처리를 포함.
- tests: test_ids.py (prefix·ULID 형식·det_id 결정론), test_timeutil.py (report_date KST 경계 05:59/06:00, asof_for), test_llm_client.py (SDK mock: 429→TransientError, 스키마 불일치→PermanentError, 예산 초과→BudgetExceeded)

T2 루트:
- docker-compose.yml (tech §1.1의 12개 서비스, 볼륨, healthcheck), Caddyfile, .env.example, Makefile (up/migrate/seed/test/golden/report-dry-run), migrations/ (alembic init + 0001_phase0.py에 tech §3 DDL 전부)
- `make up && make migrate`가 로컬에서 통과해야 한다.

T3 domino_ingest:
- sources/ 로더 + SourceSpec 검증, RssCrawler, HtmlListCrawler, fetch_html(robots·도메인 세마포어·간격), normalize, simhash64/hamming/assign_story/detect_wire, chunk
- sources/us.yaml·kr.yaml에 각 3개 소스(Fed·EDGAR 8-K 검색·연준 보도자료 / 한국은행·DART·기재부)만 — 나머지 국가는 Week 4
- tests: test_simhash.py, test_chunker.py, test_normalizer.py(300자 미만 skip, lang), test_story.py(72h 창, 임계 6, 전재 판정 3)

순서는 템플릿 §3.1의 1~5. 끝나면 보고.
```

## 4. 프롬프트 C · D · E

### 4.1 C — 주말 리뷰 (Opus, 수정 금지)

```markdown
Week {N} 리뷰. 코드를 수정하지 마라. docs/review/week-{N}.md만 작성한다.

읽어라: docs/tasks/week-{N}.md, 이번 주 커밋 전부(git log --since), 변경된 파일, docs/spec/decisions.md, 그리고 각 변경 파일에 해당하는 docs/spec/code-phase0.md §.

점검 항목 (항목마다 통과/위반과 파일:줄):
1. 명세 일치 — 시그니처·필드·테이블·도구 계약 이름이 명세와 다른 곳
2. CLAUDE.md 절대 규칙 1~10 위반
3. 테스트 — 태스크 완료 기준에 적힌 테스트가 실제로 있고 의미 있는 단언을 하는가 (항상 통과하는 테스트, mock이 구현을 대신하는 테스트)
4. 멱등성 — 두 번 실행 시 결과가 같은가 (ingest·extract·graph upsert)
5. asof — 읽기 쿼리에 ASOF_EDGE가 있는가, Python 쪽에서 asof를 기본값으로 숨긴 곳이 있는가
6. 비밀·로그 — 키·본문 전문이 로그에 찍히는 곳
7. decisions.md의 결정 중 명세를 고쳐야 할 것 (내가 판단할 목록)
8. 다음 주로 넘어가면 안 되는 것 (주말 기준 상태 미달)

형식: 심각도(치명/높음/낮음) 순, 각 항목에 재현 방법 또는 파일:줄, 그리고 "고치는 데 S/M/L". 마지막에 "다음 주 A 프롬프트에 넣을 한 줄" 3개.
```

### 4.2 D — 디버그 (Opus)

```markdown
막힌 태스크: week-{N} {T번호}. 증상: {실패 메시지 또는 동작, 그대로 붙여넣기}.
Sonnet이 시도한 것: {요약 또는 세션 보고 붙여넣기}.

읽어라: 해당 명세 §, 실패하는 테스트, 관련 모듈, decisions.md.

1. 원인을 가설 3개로 적고 각각 1분 안에 확인할 수 있는 실험을 붙여라.
2. 실험을 돌려 가설을 줄여라. 추측으로 코드를 고치지 마라.
3. 원인이 명세 결함이면 코드를 고치지 말고 decisions.md에 적고 멈춰라. 구현 결함이면 고치고 테스트를 추가해라 (같은 버그가 다시 통과 못 하게).
4. 보고: 원인 한 줄, 고친 파일:줄, 추가한 테스트, 명세 수정 필요 여부.
```

### 4.3 E — 프롬프트 튜닝 (Opus, Week 2·6·11)

```markdown
대상: {추출 프롬프트 | lead·subagent 프롬프트 | editorial 프롬프트}. 현재 지표: {make golden 결과 또는 dry-run 게이트 실패 목록 붙여넣기}.

읽어라: docs/spec/code-phase0.md §{2.2 | 5.4~5.5 | 6.4}, 최근 실패 사례 20건 (extraction_errors 또는 report_runs.gate_report 또는 Langfuse trace 링크).

규칙:
- 프롬프트 변경은 PROMPT_VERSION을 올리고 (ext-1.0 → ext-1.1), 변경 전후 `make golden` 수치를 docs/review/prompt-{version}.md에 나란히 적는다.
- 한 번에 가설 하나만 바꾼다. 두 개를 동시에 바꾸면 무엇이 효과였는지 모른다.
- 지표가 임계(tech §7.2)를 넘기면 멈춘다. 더 올리려고 few-shot을 늘리지 마라 (캐시 비용).
- 추출 프롬프트의 "Hard rules" 번호와 의미는 바꾸지 마라. 예시와 표현만.
- lead 프롬프트의 "절대 규칙" 10개는 삭제·완화 금지. 추가만 가능.

보고: 바꾼 것 한 줄, 전후 지표, 남은 실패 유형 상위 3개.
```

### 4.4 자주 하는 실수와 처방

| 증상 | 처방 |
| --- | --- |
| Sonnet이 명세를 "개선"하며 이름을 바꿈 | B 템플릿의 "하지 마라" 첫 줄을 프롬프트 맨 위로 올려라. 그래도 반복이면 해당 파일을 읽기 전용으로 두고 새 파일만 쓰게 |
| 테스트가 mock만 검증하고 로직을 안 봄 | C 리뷰 3번이 잡는다. A에서 완료 기준에 "fixture는 명세 예시 그대로"를 명시 |
| 세션이 길어지며 초반 명세를 잊음 | 세션당 태스크 1\~3개로 자르고 새 세션. 긴 세션은 품질이 떨어진다 |
| decisions.md 없이 조용히 결정 | CLAUDE.md 규칙 6을 B 프롬프트에도 한 줄 반복 |
| Opus 한도 소진 | A·C·D·E 외엔 Opus 금지. 주 후반 디버그는 D 대신 "증상만 정리해서 다음 주 월요일 A에 넣기" |
