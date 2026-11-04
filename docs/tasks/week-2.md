# Week 2 — 수집이 스케줄로 돌고, 트리플이 `extraction_runs`에 쌓인다

기준: tech-phase0.md §8.2 Week 2 행 — "추출 스키마·프롬프트·grounding·validator·hedge 사전, Langfuse 연결, gold 30건으로 첫 측정" / 주말 기준 상태 "트리플이 `extraction_runs`에 쌓인다".
이월: week-1.md 예비 T12(`llm.call`)·T13(`embed`)·T14(deployment), docs/review/week-1.md H1~H3·L1~L12, decisions.md "Week 1 주말 리뷰 판단 목록" ①~⑩(명세는 1ae1468·ff4b2fc·a779ca1에서 이미 개정됨 — 이번 주는 코드 반영).
사전 결정: `docs/spec/decisions.md`의 "Week 2 킥오프 사전 결정" 절. 아래 태스크의 "결정 W2-n"은 그 절의 번호다.

**선행 조건 (사람):**
1. `.env`에 `ANTHROPIC_API_KEY`(실키) — T7 스모크 호출, T13 실제 실행, T15·T16 `make golden` 전에. 키를 넣은 뒤 `make up`(worker가 `.env`를 다시 읽음).
2. `.env`의 `LANGFUSE_PUBLIC_KEY`·`LANGFUSE_SECRET_KEY`가 compose의 `LANGFUSE_INIT_PROJECT_*`로 만든 값과 같은지 — T8 전에 Langfuse UI(127.0.0.1:3001)에서 확인.
3. Docker Desktop 실행 중(testcontainers·`make up`). 로컬 Mac이 꺼져 있으면 스케줄 수집도 멈춘다.
4. T15 gold 라벨링 3h는 사람 작업이다.

## 태스크 (의존 순서대로)

### A. Week 1 이월 — 무인 실행(스케줄)을 켜기 전에 닫는 것

- [x] (e1aa1c3) T1 — 레포 루트 — import-linter에 `flows` 편입·`langfuse` 금지(L11), CI integration job(L8)
  - 명세: tech-phase0.md §7.1, §7.5 · CLAUDE.md 규칙 4·5 · review week-1 L8·L11 · 결정 W2-14
  - 산출물: `pyproject.toml`(`[tool.importlinter]` `root_packages`에 `flows`, forbidden 계약 `source_modules`에 `flows`, `forbidden_modules`에 `langfuse` 추가 — `flows`가 패키지로 인식 안 되면 `flows/__init__.py` 추가), `.github/workflows/ci.yml`(`lint-unit` 뒤 `integration` job: `needs: lint-unit`, `uv sync --frozen` → `uv run pytest tests/integration`, tiktoken 캐시 `actions/cache`로 `~/.cache/domino/tiktoken`)
  - 완료 기준: `uv run lint-imports` 계약 2개 KEPT이고 `flows`가 대상에 들어 있음(임시로 `flows/ingest_flow.py`에 `import httpx`를 넣으면 BROKEN이 되는지 로컬 1회 확인 후 되돌림 — 커밋 본문에 결과 1줄); push 후 `gh run list --limit 1`이 `completed success`이고 `gh run view`에 `integration` job이 있음; `make test` 초록
  - 추정: S (1.5h — L11 0.5h + L8 1h) · 모델: sonnet
  - 선행: 없음 (pyproject를 먼저 바꾸므로 이번 주 첫 merge)

- [x] (9f3c608) T2 — flows + domino_ingest — ingest 실패 경로: 문서 실패 시 `last_ok_at`(①·H1·L7), 소스 catch-all(L3), chunk 설정 연결(L1), 통신사 소스 `wire_origin`(③ flow 쪽), `skip_reason`(⑩)
  - 명세: code-phase0.md §1.4(통신사 소스 문단), §1.6(개정 의사코드·store 목록) · tech-phase0.md §2.2 `chunk_tokens`/`chunk_overlap`, §3.1 마이그레이션 0002 · decisions ①·③·⑩, 결정 W2-15
  - 산출물: `flows/ingest_flow.py`(① `mark_ok(at=min(실패 문서 published_at…, now))`; 소스 루프 `except Exception` → `mark_fail(spec, f"{type(e).__name__}: {e}")` + `log.exception`; 문서 루프의 예기치 못한 예외는 `PermanentError`와 같은 "실패 문서" 경로; `chunk(doc, max_tokens=settings.chunk_tokens, overlap=settings.chunk_overlap)`; `doc.wire_origin = spec.wire or detect_wire(...)`), `migrations/versions/0002_skip_reason.py`(tech §3.1 SQL 그대로, downgrade는 DROP COLUMN), `packages/domino_ingest/store.py`(`insert_skipped`가 `error.reason`을 `skip_reason`에), `tests/integration/test_ingest_integration.py`, `tests/integration/test_migrate.py`
  - 완료 기준 (테스트 먼저):
    - `test_ingest_integration.py::test_document_failure_holds_last_ok_at` — review H1 재현 절차 그대로(`put_immutable`이 `PermanentError`) → `last_ok_at`이 실패 entry의 `published_at`, `fail_count == 0`; monkeypatch 해제 후 2회차에 그 entry가 저장됨
    - `test_unexpected_normalize_error_skips_only_that_document`(L7)를 위 규칙에 맞게 고침(`last_ok_at` 단언 변경)
    - `test_unexpected_exception_marks_source_failed`(L3) — 크롤러가 `OverflowError`를 던지면 그 소스만 `fail_count == 1`·`last_error`에 `OverflowError`, 다음 소스는 정상 처리
    - `test_chunk_uses_settings` — `CHUNK_TOKENS=128`로 실행하면 모든 chunk `n_tokens ≤ 128`
    - `test_wire_source_sets_wire_origin` — `wire='reuters'` 소스의 문서는 byline이 없어도 `wire_origin == 'reuters'`, 같은 story의 전재와 `n_publishers == 1`(review H3 재현 케이스)
    - `test_skipped_has_reason` — 300자 미만 → `skip_reason == 'too_short'`, lang 실패 → `'lang_undetected'`
    - `test_migrate.py` — head에서 `raw_documents.skip_reason` 존재, `downgrade 0001`→`upgrade head` 무오류
    - `make test` 초록, `make migrate` 2회 연속 성공
  - 추정: M (3h) · 모델: sonnet
  - 선행: T1 권장(필수 아님)

- [x] (6d164f1) T3 — domino_ingest + flows — Crawl-delay 소스의 600초 상한 초과·뒤 소스 굶김 해소(H2)
  - 명세: code-phase0.md §1.2(`Crawler.fetch(spec, since)` 불변), §1.6 · tech-phase0.md §6.1 `ingest_flow` 10분 · review H2 · **결정 W2-1**
  - 산출물: `packages/domino_ingest/crawlers/base.py`(`fetch_budget(seconds)` context manager — `skip_known`과 같은 ContextVar 방식, `collect`가 다음 기사 다운로드를 시작하기 전에 `CLOCK.monotonic()`으로 기한 확인, 넘으면 멈추고 `budget.truncated = True`), `flows/ingest_flow.py`(소스당 `SOURCE_FETCH_BUDGET_S = 120`, flow 전체 `FLOW_FETCH_BUDGET_S = 450`; 소스별 기한 = min(120, flow 남은 시간); 남은 시간 < 10초면 나머지 소스는 손대지 않고 종료; 잘린 소스는 `mark_ok(at=since)`로 `last_ok_at` 전진 금지; 재시도 3회는 같은 소스 예산 안에서), `packages/domino_ingest/store.py`(`due_sources` 정렬을 `ORDER BY last_ok_at NULLS FIRST, id`로), `tests/unit/test_crawlers.py`, `tests/integration/test_ingest_integration.py`
  - 완료 기준 (테스트 먼저, 가짜 시계):
    - `test_crawlers.py::test_collect_stops_at_budget` — Crawl-delay 30초 도메인 10건, 예산 120초 → 다운로드 ≤ 5건, `truncated is True`; 예산 밖에서는 기존 동작 그대로(기존 T8 테스트 무수정 통과)
    - `test_ingest_integration.py::test_truncated_source_does_not_advance` — 잘린 소스의 `last_ok_at`이 `since` 그대로, 받은 문서는 저장됨, 2회차는 `skip_known`으로 이어서 받아 결국 전부 저장(수렴)
    - `test_ingest_integration.py::test_slow_source_does_not_starve_others` — id 순서상 앞의 느린 소스(가짜 Crawl-delay 30초 × 40건)가 있어도 1회 실행에서 나머지 소스 전부 `mark_ok`
    - `test_due_sources_order` — `last_ok_at` 오래된 소스가 먼저
    - `make test` 초록
  - 추정: M (2.5h) · 모델: sonnet
  - 선행: T2 (같은 flow 파일)

- [x] (9c068ef) T4 — flows + 레포 루트 — Prefect deployment(`ingest` `*/15`, 동시성 1) + 한국어 입력 확보(L12) (week-1 T14)
  - 명세: tech-phase0.md §6.1, §2 레포 트리 `flows/deploy.py` · review "다음 주로 넘어가면 안 되는 것" · **결정 W2-2, W2-3**
  - 산출물: `flows/deploy.py`(`build_deployments()`가 배포 객체 목록을 돌려주고 `__main__`이 등록; `ingest` cron `*/15 * * * *` timezone `Asia/Seoul`, `concurrency_limit=1`·collision `CANCEL_NEW`, work pool `default`), `Makefile`의 `deploy`(`docker compose exec prefect-worker python flows/deploy.py`), `tests/unit/test_deploy.py`, `docs/runbook.md`에 "deploy·코드 변경 후 재배포·KR lookback" 3줄
  - 완료 기준:
    - `test_deploy.py` — `build_deployments()`에 `ingest`가 cron `*/15 * * * *`, 동시성 1·`CANCEL_NEW`로 있음(Prefect 서버 없이)
    - `make up && make deploy` 후 `PREFECT_API_URL=http://localhost:4200/api uv run prefect deployment ls`에 `ingest/ingest`
    - 30분 뒤 `uv run prefect flow-run ls --flow-name ingest --limit 3`에 스케줄 run ≥ 2개 `COMPLETED`, `select count(*) from raw_documents` 증가(전후 값을 커밋 본문에)
    - KR lookback 1회(결정 W2-3의 SQL) 다음 run 뒤 `select count(*) from chunks c join raw_documents d on d.id = c.doc_id where d.lang = 'ko'` > 0 — 값을 커밋 본문과 아래 T15에 기록(15 미만이면 T15의 보충 경로)
  - 추정: S (2h) · 모델: sonnet
  - 선행: T2, T3 (H1·H2를 닫기 전에 무인 실행을 켜지 않는다)
  - 관측(2026-11-04 15:31~16:05 KST): `deployment ls`에 `ingest/ingest`, inspect로 cron `*/15 * * * *` Asia/Seoul·limit 1·`CANCEL_NEW` 확인, 겹친 수동 run은 `Cancelled`("Deployment concurrency limit reached"). 스케줄 run 2개 `Completed`(15:45 KST 0.2초 — 직전 수동 run 직후라 due 소스 없음, 16:00 KST 3분 56초 < 450초 예산). `raw_documents` 19(배포 직후) → 28(수동 run 55초) → 113(16:00 run 뒤), chunk 18 → 76. KR lookback(W2-3)은 첫 스케줄 run 뒤 1회 실행했고 16:00 run 뒤 BOK·MOEF는 따라잡았으나 MSIT는 `last_ok_at`이 아직 lookback 값(예산 잘림 → 다음 run에서 이어 받음).

- [x] T5 (14c876b, merge 3165ede) — domino_ingest — 통신사 키 검증(③ 로더), `make seed`가 `enabled`를 안 덮음(④), 전재 비교에서 제목 제외(decisions:104), simhash 동어반복 테스트(L6)
  - 명세: code-phase0.md §1.4, §1.6 끝 `upsert_sources` 문장 · decisions ③·④·:104 · review L2·L6, "A 프롬프트에 넣을 한 줄" 3
  - 산출물: `packages/domino_ingest/sources.py`(`load_sources`가 `wire`가 있으면 `wires.yaml` 키인지 검증 → 아니면 `PermanentError`; `upsert_sources`의 `DO UPDATE SET`에서 `enabled` 제외), `packages/domino_ingest/story.py`(`detect_wire`의 `text[:300]` 비교 대상 = 본문이 제목(공백 정규화 후)으로 시작하면 그 접두를 뺀 앞 300자, byline 정규식도 같은 대상), `tests/unit/{test_sources.py, test_story.py, test_simhash.py}`, `tests/integration/test_sources_upsert.py`
  - 완료 기준 (테스트 먼저):
    - `test_sources.py::test_wire_must_be_wires_key` — `wire: reuterz` 거부, `wire: reuters` 통과
    - `test_sources_upsert.py::test_seed_keeps_disabled` — `enabled=false`로 바꾼 뒤 재 upsert → 여전히 false, 신규 행은 YAML 값
    - `test_story.py::test_title_prefixed_copy_detected` — 제목만 다른 전재(decisions:104 재현, 이전 hamming 8)가 ≤ 3으로 판정
    - `test_simhash.py`의 전재 테스트를 "제목 접두·공백 차이·byline 교체" 변형 3종으로 교체(같은 문자열 비교 삭제), 무관 문서 > 20 유지
    - `make test` 초록
  - 추정: M (2h) · 모델: sonnet
  - 선행: 없음 (T2·T3과 파일이 겹치지 않음 — worktree 병행 가능)

- [x] T6 (df536ce, merge 3a4097c) — domino_core — `asof_for` 경량 경로(⑥), DSN을 `SecretStr`로(⑦), `Settings.tz` 삭제(⑧), `hide_parameters`(L4)
  - 명세: code-phase0.md §0.3 · tech-phase0.md §2.2 · decisions ⑥·⑦·⑧ · review L4·L5·L10
  - 산출물: `packages/domino_core/{settings.py, timeutil.py, clients/db.py, clients/redis.py}`(`pg_dsn`·`redis_url: SecretStr`, 호출부 `get_secret_value()`; `tz` 필드 삭제; `asof_for`는 `REPORT_HOUR_KST` 하나만 읽는 경량 설정 경로; `create_engine(..., hide_parameters=True)`), `migrations/env.py`(DSN 읽기), `.env.example`(`TZ=` 줄 삭제), `tests/unit/{test_settings.py, test_timeutil.py, test_clients.py}`, 필요한 테스트 fixture
  - 완료 기준 (테스트 먼저):
    - `test_timeutil.py::test_asof_for_without_other_env` — 필수 환경변수를 전부 지우고 `REPORT_HOUR_KST`만 두어도 `asof_for(date(2026,11,4)) == 2026-11-03T21:00Z`
    - `test_settings.py::test_dsn_not_in_repr` — `repr(Settings(...))`에 DSN 비밀번호 문자열 없음; `test_settings_has_no_tz`
    - `test_clients.py::test_engine_hides_parameters` — `StatementError` 문자열에 바인드 값이 없음
    - `make test` 초록, `make migrate` 성공(alembic이 SecretStr DSN을 읽음)
  - 추정: M (2h) · 모델: sonnet
  - 선행: 없음 (core만 — worktree 병행 가능)

### B. Week 2 본 범위 — 추출

- [ ] T7 — domino_core — `clients.llm.call`: Anthropic SDK·structured output·prompt caching·오류 매핑·비용·Redis 예산 (week-1 T12)
  - 명세: code-phase0.md §0.4, §0.1 · tech-phase0.md §2.2(모델·예산 설정), §5 표 일 예산 열, §6.4 · **결정 W2-4, W2-5, W2-6**
  - 산출물: `packages/domino_core/clients/llm.py`(`LLMResult`, `call`), `packages/domino_core/pricing.yaml`(모델별 USD/MTok: input·output·cache_write·cache_read, `budgets:`·`monthly_cap_usd`), `tests/unit/test_llm_client.py`, `tests/golden/test_llm_smoke.py`(`@pytest.mark.golden`, 실호출 1회), `pyproject.toml`(`anthropic` 고정 버전)
  - 완료 기준 (테스트 먼저, SDK mock):
    - `test_llm_client.py` — 429·5xx·529·연결 오류·timeout → `TransientError`; 400·401·403·404 → `PermanentError`; 스키마 불일치·JSON 파싱 실패·`stop_reason == "max_tokens"`(schema 있음) → `PermanentError`이고 그때도 비용은 카운터에 반영; 예산 카운터 ≥ 한도면 SDK 호출 0회로 `BudgetExceeded`; `cost_usd == 토큰 × pricing.yaml 단가`(cache read·write 분리); `pricing.yaml`에 없는 모델 → `PermanentError`; `cache_system=True`면 system 블록에 `cache_control: ephemeral`; 알 수 없는 budget 접두 → `PermanentError`; 월 카운터가 상한 이상이면 `task ∈ {extract, classify}`만 차단
    - 모델 리터럴 단언: `packages/` 아래 `claude-` 문자열은 `settings.py`·`pricing.yaml`에만(grep 테스트)
    - 스모크: `uv run pytest tests/golden/test_llm_smoke.py -m golden` 1회(Haiku, 짧은 schema 1건, 비용 < $0.01) — `parsed` 채워짐·`cost_usd > 0`·Redis 키 증가. 결과 1줄을 커밋 본문에
    - `make test` 초록(golden은 기본 제외)
  - 추정: M (3h) · 모델: opus
  - 선행: T1 (pyproject 먼저 merge — 의존성 충돌 방지)

- [ ] T8 — domino_core — Langfuse 연결 (`llm.call`의 trace)
  - 명세: code-phase0.md §0.4 `trace`, tech-phase0.md §6.4 관측 · decisions [root] Langfuse v2 · **결정 W2-7**
  - 산출물: `packages/domino_core/clients/langfuse.py`(SDK v2 래퍼: `trace_generation(task, model, input, output, usage, cost_usd, latency_ms, metadata)`), `llm.py`에서 호출, `pyproject.toml`(`langfuse` 2.x 고정), `tests/unit/test_langfuse_client.py`
  - 완료 기준:
    - `test_langfuse_client.py` — 성공 호출 1회 → generation 1건에 `prompt_version`·`task`·cost·토큰 태그; Langfuse 예외는 경고 로그만 남기고 `llm.call` 결과는 정상 반환; 비밀키가 로그에 없음
    - 실물 확인: T7 스모크를 다시 돌린 뒤 `curl -s -u $LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY 'http://127.0.0.1:3001/api/public/traces?limit=1'`의 `data`가 1건 이상(명령과 trace id를 커밋 본문에, 키는 쓰지 않음)
    - `make test` 초록
  - 추정: S (1.5h) · 모델: opus
  - 선행: T7

- [x] (1974979) T9 — domino_extract — 스키마·시스템 프롬프트 전문·`PROMPT_VERSION`·hedge·attribution 사전
  - 명세: code-phase0.md §2.1, §2.2, §2.5(`HEDGE`) · **결정 W2-8**
  - 산출물: `packages/domino_extract/{schema.py, hedge_lexicon.yaml, attribution_lexicon.yaml, lexicon.py}`, `packages/domino_extract/prompts/{__init__.py, extract_system.md}`(§2.2 코드 블록 글자 그대로), `tests/unit/test_schema.py`
  - 완료 기준 (테스트 먼저):
    - `test_schema.py` — §2.2 예시 1~3의 Output JSON이 `ExtractionResult`로 검증 통과; `no_causal_content=true`+트리플 → 거부; 트리플 11개 → 거부; `impact` 1.1 → 거부; `PROMPT_VERSION == "ext-1.0"`; `extract_system.md`가 code-phase0.md §2.2 블록과 글자 단위 동일(테스트가 명세 파일에서 블록을 잘라 비교); hedge·attribution 사전이 `en`·`ko` 키를 가짐
    - `make test` 초록
  - 추정: S (1.5h) · 모델: sonnet
  - 선행: 없음

- [x] T10 (fdd45f2, merge f9a3a36) — domino_extract — grounding (NFKC·공백 정규화, 역매핑, rapidfuzz 재탐색)
  - 명세: code-phase0.md §2.4, §7.7 `test_grounding.py` · tech-phase0.md §5.1 extract.validate (1)
  - 산출물: `packages/domino_extract/grounding.py`(`norm`, `ground`), `tests/unit/test_grounding.py`, `pyproject.toml`(`rapidfuzz` 고정)
  - 완료 기준 (테스트 먼저): `test_grounding.py` — 정확 일치, 공백 중복, NFKC 전각, 따옴표 변형, BOM, 부분 일치 95(경계 94·95), 실패 → None, 모든 성공 케이스에서 `text[s:e]`가 원문 좌표의 해당 구간(역매핑), 한국어 1건. `make test` 초록
  - 추정: M (2h) · 모델: sonnet
  - 선행: 없음

- [ ] T11 — domino_extract — validator
  - 명세: code-phase0.md §2.5, §7.7 `test_validator.py` · tech-phase0.md §5.1 extract.validate · **결정 W2-8**
  - 산출물: `packages/domino_extract/validator.py`(`ValidatedTriple`, `DropReason`, `looks_attributive`, `validate`), `tests/unit/test_validator.py`
  - 완료 기준 (테스트 먼저): `test_validator.py` — hedge 강등(CAUSAL+"may"→SPECULATIVE, `is_correlation_only=True`), 자기참조 drop, `state_controlled` 소스의 attributive fact → attribution(비 state 소스는 그대로), `char_start/char_end`가 문서 좌표(`chunk.char_start + g[0]`)이고 `doc.text[s:e]`가 quote 위치, grounding 실패 4/10 → `PermanentError("grounding_rate_low")`·3/10 → 통과. `make test` 초록
  - 추정: M (2h) · 모델: sonnet
  - 선행: T9, T10

- [ ] T12 — domino_extract — classifier·extractor(재시도 1회)·extract store
  - 명세: code-phase0.md §2.3, §2.6(`wrap`·`retry_once_or_drop`·`record_run`), §2.2 마지막 줄(사용자 메시지 형식) · tech-phase0.md §3.2 `extraction_runs`·`extraction_errors`, §5 extract.classify·extract.llm 행 · **결정 W2-9, W2-10, W2-11**
  - 산출물: `packages/domino_extract/{classifier.py, extractor.py, store.py}`(`store`: `new_chunks(limit)`, `run_exists(chunk_id, prompt_version)`, `set_is_causal`, `set_chunk_status`, `record_run`, `record_errors`), `tests/unit/{test_classifier.py, test_extractor.py}`, `tests/integration/test_extract_store.py`
  - 완료 기준 (테스트 먼저, `llm.call` mock):
    - `test_classifier.py` — 힌트 0개 → LLM 호출 0회·False; 힌트 있음+"Y" → True; LLM `TransientError`/`PermanentError` → True(보수적 통과); `BudgetExceeded`는 전파
    - `test_extractor.py` — `wrap`이 §2.2 마지막 줄 형식 그대로; 1차 스키마 실패 → 2차는 같은 system·user 끝에 거절 사유 1줄 → 성공 시 트리플 반환; 2차도 실패 → `ExtractFailed(stage, raw)`; `grounding_rate_low`도 같은 경로
    - `test_extract_store.py`(testcontainers postgres) — `new_chunks`가 `lang ∈ {en, ko}`·`status='new'`·문서 `chunked`만 id 순으로; `record_run` 2회 → 행 1개(`(chunk_id, prompt_version)` 멱등); drop마다 `extraction_errors` 1행(stage `grounding`|`validator`)
    - `make test` 초록
  - 추정: M (3h) · 모델: sonnet
  - 선행: T7, T11

- [ ] T13 — flows — `extract_flow`·ingest 끝 트리거·extract deployment·종단 확인
  - 명세: code-phase0.md §2.6 · tech-phase0.md §5 extract 행, §6.1 `extract_flow` 20분·동시성 1 · **결정 W2-2, W2-9, W2-12**
  - 산출물: `flows/extract_flow.py`, `flows/ingest_flow.py`(끝에 extract 트리거 — 결정 W2-12), `flows/deploy.py`(`extract` deployment, 스케줄 없음, 동시성 1·`CANCEL_NEW`), `tests/integration/test_extract_integration.py`(testcontainers postgres+redis, `llm.call` mock)
  - 완료 기준:
    - `test_extract_integration.py` — 1회 실행 후 `extraction_runs` 행·`chunks.status='extracted'`·`is_causal` 채워짐; 2회 실행 후 행 수 불변·LLM mock 추가 호출 0; 힌트 없는 chunk는 `dropped`·LLM 0회; `BudgetExceeded`면 남은 chunk `new` 그대로 종료; `TransientError`면 그 chunk 그대로 두고 종료; 2회 실패 chunk는 `status='error'`+`extraction_runs.n_triples=0`+`extraction_errors` 1행; `zh` chunk는 선택 안 됨
    - `test_deploy.py`에 `extract` 동시성 1 단언 추가
    - **주말 기준 상태:** `make up && make migrate && make deploy` 후 다음 ingest run이 extract run을 띄우고, `select count(*) from extraction_runs where n_triples > 0` > 0, Langfuse traces에 `prompt_version=ext-1.0` 태그(T8 curl). 값·비용(`sum(cost_usd)`)을 커밋 본문에
    - `make test` 초록
  - 추정: M (3h) · 모델: sonnet
  - 선행: T4, T8, T12

### C. Week 2 본 범위 — gold 30 첫 측정

- [ ] T14 — domino_eval — gold 포맷·매칭·Metrics·회귀·`make golden`·gold 내보내기 도구
  - 명세: code-phase0.md §7.1, §7.2 · tech-phase0.md §7.1 골든 층, §7.2 임계 · **결정 W2-13**
  - 산출물: `packages/domino_eval/{gold.py, matching.py, regression.py, gold_export.py}`(`GoldItem` 모델·로더, 매칭 규칙, `Metrics`, `run_extract_regression`, `assert_thresholds`, `python -m domino_eval.gold_export --lang ko --n 15 --out …`가 DB chunk를 라벨 빈 칸 jsonl로), `tests/golden/test_extract_regression.py`(`@pytest.mark.golden`), `Makefile`의 `golden`(`uv run pytest tests/golden -m golden -s`), `tests/unit/{test_matching.py, test_regression_metrics.py}`
  - 완료 기준 (테스트 먼저, LLM mock):
    - `test_matching.py` — quote 문자 Jaccard 0.59 → 짝 없음·0.60 → 짝; cause/effect 이름은 `norm()`+casefold 일치; 짝 없는 예측 FP·짝 없는 gold FN
    - `test_regression_metrics.py` — 손으로 계산한 3건 fixture에서 `grounding_rate`·`rk_macro_f1`·`dir_acc`·`task1_f1`·`triples_per_chunk`·`conf_over_08_ratio`가 기대값; `assert_thresholds`가 tech §7.2 임계 각각의 경계에서 통과/실패
    - `make test` 초록(golden 제외), `make golden`이 gold 파일이 없으면 명확한 메시지로 실패
  - 추정: M (3h) · 모델: sonnet
  - 선행: T12

- [ ] T15 — domino_eval (사람) — gold 30건 라벨링(en 15·ko 15) + 첫 `make golden`
  - 명세: code-phase0.md §7.1 · tech-phase0.md §7.2 · **결정 W2-13**
  - 산출물: `packages/domino_eval/gold/extract_030.jsonl`, `docs/review/golden-ext-1.0.md`(Metrics 표·비용·실패 상위 유형 3)
  - 완료 기준: 30줄이 `GoldItem`으로 로드됨(`uv run python -c "from domino_eval.gold import load_gold; assert len(load_gold('packages/domino_eval/gold/extract_030.jsonl')) == 30"`), ko 15·en 15, 인과 없음(has_causal false) 각 언어 ≥ 4; `make golden` **1회** 실행(예상 비용 ≤ $0.5, 예산 키 `budget:golden:<date>` 한도 $2) 결과를 `golden-ext-1.0.md`에 기록 — 임계 미달이어도 측정 자체가 완료 기준
  - 추정: M (3h 사람 + 0.5h sonnet 보조) · 모델: sonnet(내보내기·기록 보조) — 라벨은 사람만
  - 메모(T4): KR lookback 후 한국어 chunk 46개(2026-11-04 16:05 KST, 첫 스케줄 run 뒤 1회 실행·16:00 run 뒤 측정) — 15 이상이라 보충 경로 불필요(인과 서술 포함 여부는 T14 내보내기·T15 라벨링에서 판단, MSIT 소스가 아직 수집 중이라 더 늘 수 있음).
  - 선행: T4(KR chunk), T14

- [ ] T16 — domino_extract — 프롬프트 튜닝 1라운드 (프롬프트 E)
  - 명세: docs/prompts/E-tuning.md · code-phase0.md §2.2 · tech-phase0.md §7.2 · **결정 W2-13**
  - 산출물: `packages/domino_extract/prompts/extract_system.md`·`schema.py`의 `PROMPT_VERSION`(ext-1.0 → ext-1.1, 바꿀 때만), `docs/review/prompt-ext-1.1.md`(전후 지표)
  - 완료 기준: 가설 ≤ 2개, 가설당 `make golden` 1회 + 채택안 확인 1회(이번 주 golden 총 ≤ 4회·하루 ≤ $2 — T15 1회 포함, 결정 W2-13); Hard rules 번호·의미 불변(`test_schema.py`의 명세 일치 테스트는 ext-1.1에서 "예시·표현만 다름"으로 바꾸고 decisions에 기록); 임계를 넘으면 그 자리에서 멈춤. gold 30은 과적합 위험이 커서 개선이 2건 이하 차이면 채택하지 않는다
  - 추정: M (2.5h) · 모델: opus
  - 선행: T15

### 예비 (본 범위 밖 — 시간이 남을 때만, 순서대로)

- [ ] T17 — domino_core — `clients.embed` (week-1 T13)
  - 명세: code-phase0.md §0.5 · decisions [root] embedder 배치
  - 산출물: `packages/domino_core/clients/embed.py`, `tests/unit/test_embed.py`
  - 완료 기준: `test_embed.py` — 70개 입력 → TEI 호출 3회(32·32·6), 출력 L2 norm 1±1e-6, 1024-dim 아니면 `PermanentError`, 실패 → `TransientError`. `make test` 초록
  - 추정: S (1h) · 모델: sonnet
  - 선행: 없음 — 기본은 Week 3 첫 태스크(Resolver가 처음 쓴다)

- [ ] T18 — 레포 루트 — 커버리지 측정(L9)
  - 명세: tech-phase0.md §7.1 "커버리지 ≥ 80% (`domino_core` 제외)"
  - 산출물: `pyproject.toml`(`pytest-cov` 고정, `--cov` 설정), `Makefile` `test`에 리포트
  - 완료 기준: `make test` 출력에 `domino_core` 제외 커버리지 %가 찍힘(80% 미달이면 수치만 기록, 실패 처리는 Week 3에 결정)
  - 추정: S (0.5h) · 모델: sonnet
  - 선행: T1

### 세션 묶음 제안 (프롬프트 B 1회 = 1줄, 같은 줄의 ‖는 worktree 병행)

1. T1 (먼저 merge — pyproject)
2. T2 ‖ T5 ‖ T6 ‖ T9 ‖ T10 (파일이 겹치지 않음: flows+store / sources·story / core / extract schema / extract grounding)
3. T3 (T2 뒤) ‖ T7 (opus)
4. T4 (T3 뒤 — 스케줄 켬, KR lookback) ‖ T8 (opus, T7 뒤) ‖ T11 (T9·T10 뒤)
5. T12 (T7·T11 뒤)
6. T13 ‖ T14
7. T15 (사람) → T16 (opus)
8. (예비) T17 + T18

`pyproject.toml`·`uv.lock`은 T1·T7·T8·T10이 바꾼다 — merge할 때 `uv lock`으로 다시 만들고 `uv sync --frozen`이 되는지 확인한다. `flows/ingest_flow.py`는 T2 → T3 → T13 순서로만 고친다.

## 이번 주 리스크

1. **Anthropic SDK의 structured output·prompt caching 세부가 불확실하다.** 고정할 SDK 버전에서 Pydantic 스키마를 넘기는 방식(네이티브 structured output vs 강제 tool use), `maxLength`·`le`·`max_length` 같은 제약이 서버에서 강제되는지(아니면 SDK가 지우고 클라이언트 검증만 하는지), `claude-haiku-5-5`의 최소 캐시 길이(시스템 프롬프트 §2.2가 약 2~3천 토큰이라 미달이면 `cache_read`가 항상 0). → 확인: T7 시작 시 claude-api 스킬로 현재 SDK 문서 확인, 스모크 1회에서 `usage.cache_read_input_tokens`를 2회 연속 호출로 확인. 결정 W2-6에 따라 클라이언트 측 Pydantic 검증은 항상 한다(서버 강제 여부와 무관). 캐시가 안 걸리면 비용 추정만 바꾸고 decisions에 기록.
2. **Langfuse 서버 v2 ↔ Python SDK 버전.** SDK v3(OTel 기반)는 v3 서버를 요구한다. → 결정 W2-7: SDK 2.x 마지막 계열로 고정. 확인: T8의 curl로 trace 1건.
3. **Prefect 3.9 process worker의 deployment 방식.** `flow.from_source("/app", entrypoint)`를 worker 컨테이너 안에서 실행해 경로를 맞추는 방식(결정 W2-2)이 이 버전에서 되는지, deployment `concurrency_limit` + `CANCEL_NEW`, `run_deployment(timeout=0)`이 기대대로인지. → 확인: T4에서 `prefect deployment inspect ingest/ingest`로 cron·동시성 확인, 겹치는 수동 run 1회로 `CANCEL_NEW` 확인. 안 되면 `prefect.yaml` + `prefect deploy --all`(pull 단계 `set_working_directory: /app`)로 바꾸고 decisions에 기록. 코드를 바꾸면 worker 이미지 재빌드(`make up`) 후 `make deploy`가 필요하다(runbook).
4. **명세 의사코드 오류 2건 — 명세 수정 필요(코드는 사전 결정대로, 명세는 건드리지 않음):** (a) code §2.6이 `except PermanentError`를 `except BudgetExceeded`보다 먼저 써서, `BudgetExceeded ⊂ PermanentError`라 예산 초과가 "재시도 후 포기"로 처리된다 → 결정 W2-9: `BudgetExceeded`를 먼저 잡는다. (b) code §1.6 store 목록의 `known_url_hashes(source_id)`는 구현·decisions:97의 `known_url_hashes(hashes)`와 다르다(판단 ② 전기 오류로 보임) → 결정 W2-16: 코드 유지. 둘 다 주말 리뷰(C)에서 명세에 반영할지 판단.
5. **명세 공백 — 사전 결정으로 메움:** extract 대상 언어(Week 1 소스 15개 중 zh 3·de 3·ja 2인데 `CAUSAL_HINTS`·`HEDGE`는 en·ko만 → 결정 W2-10), `looks_attributive`·`DropReason`·`retry_once_or_drop` 정의(결정 W2-8·W2-11), 예산 한도 매핑(`budget_key`만으로 한도를 알 수 없음, classify $0.3·월 $250이 설정에 없음 → 결정 W2-5), Week 2에 resolve·graph가 없을 때 chunk 상태(결정 W2-9), ingest 10분 상한 안에 20분짜리 extract 서브플로가 들어가는 모순(결정 W2-12).
6. **한국어 입력(L12).** 수집 누적만으로는 KR 3소스(BOK·MSIT 신규 드묾, MOEF는 첨부 본문)에서 인과 서술이 있는 한국어 chunk 15개가 이번 주 안에 안 쌓일 수 있다. → T4에서 KR 소스 60일 lookback 1회(결정 W2-3), 그래도 부족하면 T15에서 사람이 공공기관 공개 본문 단락을 직접 gold에 넣는다(결정 W2-13). LLM 생성 텍스트·LLM 사전 라벨은 쓰지 않는다.
7. **gold 30·라벨러 1인.** tech §7.2는 라벨러 2인 교차·κ ≥ 0.7인데 이번 주는 1인 파일럿(결정 W2-13)이다. 30건은 지표 분산이 커서 튜닝(T16) 과적합 위험이 있다. 또 tech §7.2("100건은 Week 3")와 §8.2("gold 100" Week 4)가 어긋난다 — Week 3 킥오프에서 정한다.
8. **비용.** 스케줄이 켜진 뒤(T4·T13) 추출 하루 상한 $2 + classify $0.3, golden은 이번 주 ≤ 4회·하루 $2(결정 W2-5). 스모크·golden은 사람이 키를 넣은 뒤에만 돈다. 확인: T13 커밋 본문에 `sum(extraction_runs.cost_usd)`, 주말에 Redis `budget:*` 키 값.
9. **CI integration job(L8)이 느리거나 Docker 이미지 pull에서 실패할 수 있다**(neo4j·bitnami minio 이미지, tiktoken 다운로드). → T1에서 첫 실행 시간을 커밋 본문에 기록, 10분 넘으면 integration을 `push: main`에만 돌리고 decisions에 기록.
10. **H2 수치(소스 120초·flow 450초)는 추정이다.** Destatis는 run당 최대 4건(120/30)만 받는다 — 하루 수십 건 상한이라 충분하지만, 장기 정지 후에는 몇 run에 걸쳐 따라잡는다. 확인: T4의 30분 관측에서 flow 소요 시간이 450초 + 저장 시간 안인지.

## 범위 축소 시 버릴 순서

본 범위 T1~T16 추정 37.5h(A 이월 13h + B 추출 16h + C gold 8.5h)로 주 25h를 넘는다. 예비 T17·T18(1.5h)은 이미 범위 밖이다. 주말 기준 상태(트리플이 `extraction_runs`에 쌓인다)에 필요한 것은 T2·T3·T4(무인 실행 안전) + T7~T13이고, 아래는 그 밖의 것을 먼저 버리는 순서다.

1. T16 프롬프트 튜닝 → Week 3 (gold 100으로 튜닝하는 편이 과적합이 적다) · −2.5h
2. T5 통신사 키 검증·`enabled` 보존·제목 접두·L6 → Week 3 (Q3 승격이 처음 도는 주, 통신사 소스가 들어오는 Week 4 전에 반드시) · −2h
3. T6 core ⑥⑦⑧·L4 → Week 3 첫 태스크 (⑥은 Week 3 `test_asof.py`의 선행) · −2h
4. T1의 CI integration job(L8) → Week 3 (L11 import-linter 0.5h는 남긴다 — `flows/extract_flow.py`가 생기기 전에 필요) · −1h
5. T14·T15 gold 30 첫 측정 → Week 3 (Week 3의 gold 작업과 합침; 비용 0이 되고 한국어 확보 시간도 번다) · −6h

1~5를 적용하면 24h(T1 일부·T2·T3·T4·T7~T13). 그래도 넘치면 T8 Langfuse를 Week 3으로 미루되(−1.5h) `llm.call`의 trace 인자는 그대로 받아 둔다. T2·T3·T4는 버리지 않는다 — 버리면 스케줄을 켤 수 없고, 그러면 T13의 입력이 현재 chunk 18개뿐이다.
