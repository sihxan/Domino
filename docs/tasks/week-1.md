# Week 1 — `make up` 후 문서가 Postgres에 쌓인다

기준: tech-phase0.md §8.2 Week 1 행 — "레포·compose·settings·마이그레이션 0001·Neo4j DDL·CI lint/unit. `sources/` 5개국 15소스. RSS·HTML 크롤러, normalizer, SimHash, chunker".
이전 주 없음(첫 주). 사전 결정은 `docs/spec/decisions.md`의 2026-11-04 항목.

**선행 조건 (사람):** 로컬에 `uv` 미설치 — T1 전에 `brew install uv`. Docker Desktop 실행 중이어야 `make up`·testcontainers가 돈다.

## 태스크 (의존 순서대로)

- [x] T1 (59b5116) — 레포 루트 — uv 프로젝트·도구 설정·패키지 골격·Makefile 뼈대
  - 명세: tech-phase0.md §2, §2.1, §7.5 · code-phase0.md §0.1
  - 산출물: `pyproject.toml`(Python 3.12, ruff, mypy strict, pytest, import-linter 계약 `core←ingest←extract←resolve←graph←signals←agent←report←eval`), `uv.lock`, `packages/domino_{core,ingest,extract,resolve,graph,signals,agent,report,eval}/__init__.py`(docstring 1줄), `tests/{unit,integration,golden}/`, `tests/conftest.py`, `Makefile`(test 타깃 동작, 나머지는 해당 태스크에서 채움), `.env.example`(tech §2.2 키 이름 전부, 값 없음)
  - 완료 기준: `uv sync` 성공, `make test` 초록(ruff + mypy --strict + lint-imports + pytest — `tests/unit/test_layout.py::test_packages_importable` 1개), `lint-imports` 계약 1개 KEPT
  - 추정: S (1.5h) · 모델: sonnet
  - 선행: 없음

- [x] T2 (95ee49a) — domino_core — errors·ids·timeutil·settings·models
  - 명세: code-phase0.md §0.2, §0.3, §0.6 · tech-phase0.md §2.2, §2.3
  - 산출물: `packages/domino_core/{errors.py, ids.py, timeutil.py, settings.py, models.py}`, `tests/unit/{test_ids.py, test_timeutil.py, test_settings.py, test_models.py}`
  - 완료 기준:
    - `test_ids.py`: prefix 16종 전부 허용·그 외 거부, `new_id` 형식 `^{prefix}_[0-9A-HJKMNP-TV-Z]{26}$`, 같은 ms 내 생성 순서 정렬, `det_id` 같은 입력→같은 값·`{prefix}_` + 20 hex
    - `test_timeutil.py`: `report_date` KST 자정 경계(UTC 14:59:59→전날, 15:00:00→당일), `asof_for(2026-11-04)` == `2026-11-03T21:00:00Z`, `to_kst` tz 변환, naive datetime 거부
    - `test_settings.py`: 환경변수로 생성, 모델 기본값 tech §2.2와 일치, `SecretStr`이 `repr`에 노출 안 됨
    - `test_models.py`: `SourceSpec`의 잘못된 region/grade/kind 거부
    - `make test` 초록
  - 추정: M (2.5h) · 모델: sonnet
  - 선행: T1

- [ ] T3 — domino_core — clients: db·redis·s3·http·neo
  - 명세: code-phase0.md §0.1, §0.2 · tech-phase0.md §1.1, §2.2 · decisions 2026-11-04 [core] http
  - 산출물: `packages/domino_core/clients/{__init__.py, db.py, redis.py, s3.py, http.py, neo.py}`, `tests/unit/test_clients.py`
  - 완료 기준: `test_clients.py` — (http) 5xx·timeout→`TransientError`, 4xx→`PermanentError`, 고정 UA 헤더; (s3) `put_immutable`이 기존 키면 덮어쓰지 않음; (neo) `run_cypher_file`이 `;` 단위로 분리 실행; 모두 하위 SDK mock. `make test` 초록
  - 추정: M (3h) · 모델: sonnet
  - 선행: T2

- [ ] T4 — 레포 루트 — docker-compose·Dockerfile·Caddyfile·`make up`
  - 명세: tech-phase0.md §1, §1.1 · decisions 2026-11-04 [root] compose profile, langfuse
  - 산출물: `docker-compose.yml`(12개 서비스, 볼륨, healthcheck, 리소스 상한; `api`·`web`·`agent-runner`·`caddy`는 profile `app`), `docker/worker.Dockerfile`, `Caddyfile`, `Makefile`의 `up`/`down`
  - 완료 기준: `make up` 후 `docker compose ps`에서 postgres·neo4j·redis·minio·prefect-server·langfuse가 `healthy`(embedder는 리스크 1 참고 — `running`이면 통과), `docker compose config -q` 무오류
  - 추정: M (3h) · 모델: sonnet
  - 선행: T1

- [ ] T5 — 레포 루트 + domino_graph — 마이그레이션 0001·Neo4j DDL·`make migrate`
  - 명세: tech-phase0.md §3 (3.1~3.5 DDL 전부), §4.3 · 이 세션은 예외적으로 `domino_graph/ddl/`만 같이 만든다
  - 산출물: `alembic.ini`, `migrations/env.py`, `migrations/versions/0001_phase0.py`(tech §3 SQL 그대로), `packages/domino_graph/ddl/0001_schema.cypher`(tech §4.3 그대로), `packages/domino_graph/ddl/__init__.py`(`apply_ddl()`), `Makefile`의 `migrate`, `tests/integration/test_migrate.py`
  - 완료 기준: `test_migrate.py`(testcontainers) — `alembic upgrade head` 후 테이블 집합이 tech §3의 CREATE TABLE 목록과 정확히 일치, `downgrade base`→`upgrade head` 재실행 무오류; Neo4j DDL 2회 실행 후 `SHOW CONSTRAINTS` 7개·`SHOW INDEXES`에 §4.3의 9개 이름 존재. 로컬 `make migrate` 2회 연속 성공
  - 추정: M (3h) · 모델: sonnet
  - 선행: T3, T4

- [ ] T6 — 레포 루트 — CI (GitHub Actions lint·unit)
  - 명세: tech-phase0.md §7.1, §7.5 (이번 주는 lint·unit만)
  - 산출물: `.github/workflows/ci.yml`
  - 완료 기준: push 후 `gh run list --limit 1`이 `completed success`; job은 `uv sync --frozen` → ruff → mypy --strict → lint-imports → `pytest tests/unit`
  - 추정: S (1h) · 모델: sonnet
  - 선행: T2

- [ ] T7 — domino_ingest — 소스 레지스트리·로더·`make seed`
  - 명세: code-phase0.md §1.1, §0.6 `SourceSpec` · tech-phase0.md §3.1 `sources`, §8.2 Week 1 · decisions 2026-11-04 [ingest] source id, week-1 소스 범위
  - 산출물: `sources/{us,kr,jp,tw,de}.yaml`(국가당 3, `kind ∈ {rss, html}`만), `sources/wires.yaml`, `packages/domino_ingest/sources.py`(`load_sources(dir) -> list[SourceSpec]`, `upsert_sources`), `Makefile`의 `seed`(이번 주는 sources만), `tests/unit/test_sources.py`, `tests/integration/test_sources_upsert.py`
  - 완료 기준: `test_sources.py` — 저장소의 YAML 15개 전부 검증 통과, id 중복 거부, `parser`가 등록된 크롤러 이름이 아니면 거부, wires 정규식 컴파일; `test_sources_upsert.py` — 2회 upsert 후 행 15개·값 갱신; 각 URL을 로더 작성 시 1회 수동 확인(응답 200·robots 허용)하고 결과를 PR/커밋 본문에 표로
  - 추정: M (2.5h) · 모델: sonnet
  - 선행: T5

- [ ] T8 — domino_ingest — fetch_html·RssCrawler·HtmlListCrawler
  - 명세: code-phase0.md §1.2 · tech-phase0.md §5.1 ingest.fetch
  - 산출물: `packages/domino_ingest/crawlers/{__init__.py(CRAWLERS), base.py(Crawler, fetch_html), rss.py, html_list.py}`, `tests/unit/{test_fetch.py, test_crawlers.py}`, `tests/fixtures/ingest/{feed.xml, list.html, article.html}`
  - 완료 기준: `test_fetch.py` — robots disallow→`PermanentError`, 같은 도메인 연속 호출 간격 ≥ 2초(가짜 시계), 다른 도메인은 대기 없음, 4xx/5xx 매핑; `test_crawlers.py` — RSS fixture에서 `since` 이후 entry만 `RawDoc`으로, `spec.url#selector` 분리·링크 절대경로화. 네트워크 호출 0(http client mock)
  - 추정: M (3h) · 모델: sonnet
  - 선행: T3, T7

- [ ] T9 — domino_ingest — normalizer·simhash·story·전재 판정
  - 명세: code-phase0.md §1.3, §1.4 · tech-phase0.md §5.1 ingest.story · decisions 2026-11-04 [ingest] simhash 부호, StoryHead/WireRules
  - 산출물: `packages/domino_ingest/{normalizer.py, simhash.py, story.py}`, `tests/unit/{test_normalizer.py, test_simhash.py, test_story.py}`
  - 완료 기준:
    - `test_simhash.py`(code §7.7) — 동일 문서 hamming 0, 전재(앞 300자 동일) ≤ 3, 무관 문서 > 20, signed↔unsigned 왕복
    - `test_normalizer.py` — 300자 미만 `PermanentError("too_short")`, lang(spec 우선·없으면 langdetect), doc_type 매핑(rss 중앙은행·정부→RELEASE, 그 외 NEWS), `snapshot_key == f"raw/{yyyy}/{mm}/{doc_id}.html"`, s3 mock 호출 1회
    - `test_story.py` — 72h 창 밖 head 무시, 임계 6 경계(6 배정·7 신규), 전재 판정(byline 정규식, text[:300] hamming 3 경계)
  - 추정: M (3h) · 모델: sonnet
  - 선행: T3, T7

- [ ] T10 — domino_ingest — chunker
  - 명세: code-phase0.md §1.5 · tech-phase0.md §2.2 `chunk_tokens`/`chunk_overlap`
  - 산출물: `packages/domino_ingest/chunker.py`, `tests/unit/test_chunker.py`
  - 완료 기준: `test_chunker.py`(code §7.7) — 문단 경계 우선, 모든 chunk ≤ 512 토큰, 인접 chunk overlap ≈ 64 토큰, 모든 chunk에서 `doc.text[c.char_start:c.char_end] == c.text`, 한국어·영어 fixture 각 1
  - 추정: S (2h) · 모델: sonnet
  - 선행: T2

- [ ] T11 — domino_ingest + flows — 적재·`ingest_flow`·종단 확인
  - 명세: code-phase0.md §1.6 · tech-phase0.md §3.1, §5 표 ingest.fetch/story/chunk, §6.1 · decisions 2026-11-04 [ingest] skipped 행, extract_flow 호출
  - 산출물: `packages/domino_ingest/store.py`(`due_sources`, `exists`, `insert_document`, `insert_skipped`, `upsert_story`, `recent_story_heads`, `story_docs`, `insert_chunk`, `set_status`, `mark_ok`, `mark_fail`), `flows/ingest_flow.py`, `tests/integration/test_ingest_integration.py`
  - 완료 기준: `test_ingest_integration.py`(testcontainers postgres+minio, 크롤러는 T8 fixture로 mock) — 1회 실행 후 문서·chunk·story 생성, 2회 실행 후 행 수 불변(url_hash 멱등), 300자 미만은 `status='skipped'`, 연속 5회 실패 소스 `enabled=false`. **주말 기준 상태:** `make up && make migrate && make seed && uv run python flows/ingest_flow.py` 후 `select count(*) from raw_documents where status='chunked'` > 0
  - 추정: M (3h) · 모델: sonnet
  - 선행: T5, T8, T9, T10

### 예비 (주말 기준 상태에 불필요 — 시간이 남을 때만, 순서대로)

- [ ] T12 — domino_core — `clients.llm.call` (pricing.yaml·예산 카운터·Langfuse trace·structured output)
  - 명세: code-phase0.md §0.4 · tech-phase0.md §2.2, §6.4
  - 산출물: `packages/domino_core/clients/llm.py`, `packages/domino_core/pricing.yaml`, `tests/unit/test_llm_client.py`
  - 완료 기준: `test_llm_client.py` — SDK mock: 429·5xx→`TransientError`, 스키마 불일치→`PermanentError`, 예산 초과→`BudgetExceeded`(호출 전 차단), cost = 토큰 × pricing.yaml 단가, 모델명은 인자로만(코드에 리터럴 없음 — grep 단언)
  - 추정: M (3h) · 모델: opus
  - 선행: T3

- [ ] T13 — domino_core — `clients.embed`
  - 명세: code-phase0.md §0.5
  - 산출물: `packages/domino_core/clients/embed.py`, `tests/unit/test_embed.py`
  - 완료 기준: `test_embed.py` — 70개 입력→TEI 호출 3회(32·32·6), 출력 L2 norm 1±1e-6, 1024-dim 아니면 `PermanentError`, 실패→`TransientError`
  - 추정: S (1h) · 모델: sonnet
  - 선행: T3

- [ ] T14 — flows — Prefect deployment (`ingest_flow` `*/15`, 동시성 1)
  - 명세: tech-phase0.md §6.1
  - 산출물: `flows/deploy.py`, `Makefile`에 배포 호출을 `up` 뒤에 연결
  - 완료 기준: `make up` 후 Prefect UI(4200)에 `ingest` deployment가 cron `*/15 * * * *`로 보이고, 30분 뒤 `raw_documents` 수가 증가
  - 추정: S (1h) · 모델: sonnet
  - 선행: T4, T11

### 세션 묶음 제안 (프롬프트 B 1회 = 1줄)

1. T1 + T2 (루트·core — 아직 아무것도 없어서 예외적으로 같이)
2. T3
3. T4
4. T5 + T6
5. T7
6. T8
7. T9
8. T10 + T11
9. (예비) T12 → Opus 세션 / T13 + T14

## 이번 주 리스크

1. **embedder(TEI)가 Apple Silicon(arm64)에서 안 뜰 수 있다.** 공식 CPU 이미지는 amd64 위주 → `platform: linux/amd64` 에뮬레이션은 느리고 healthcheck가 타임아웃 날 수 있다. 이번 주엔 embedder를 쓰는 코드가 없다(Week 3). 확인: T4에서 `docker compose up embedder` 1회, 실패하면 `running`만 확인하고 decisions.md에 기록 후 진행.
2. **Langfuse 버전.** v3 self-host는 ClickHouse·별도 Redis·S3가 필요해 tech §1.1의 12개 서비스 목록과 안 맞는다. → 사전 결정: v2 이미지 고정, 같은 postgres의 `langfuse` DB 사용. 확인: T4에서 `langfuse` healthy.
3. **명세 내 시간 경계 불일치.** 프롬프트 세트 §3.2 예시는 "report_date KST 경계 05:59/06:00"이라 하지만, code §0.3·tech §2.3 정의상 `report_date`는 KST 자정 경계이고 06:00은 `asof_for`의 기준이다. → 명세(정의)를 따른다. T2 테스트는 위 완료 기준대로.
4. **Week 1 소스 범위 불일치.** 프롬프트 세트 §3.2 예시는 US·KR 3개씩, EDGAR·DART 포함이지만 tech §8.2는 "5개국 15소스"이고 EDGAR·DART 파서는 Week 4다. → tech를 따른다: US·KR·JP·TW·DE 각 3, `kind ∈ {rss, html}`만.
5. **simhash 64-bit unsigned vs Postgres `bigint`(signed).** 그대로 넣으면 2^63 이상에서 overflow. → 사전 결정: 저장 시 two's complement signed 변환, 읽을 때 복원(T9 테스트에 왕복 포함).
6. **명세에 없는 타입·경로:** `StoryHead`, `WireRules`, `GateFailure`(core.errors가 report의 타입을 참조해 의존 방향 위반), ingest의 HTTP 경로(`httpx` 직접 import 금지인데 `fetch_html`이 ingest에 있음), skipped 문서의 NOT NULL 컬럼, `extract_flow` 미존재. → 전부 decisions.md에 사전 결정.
7. **라이브러리 무게·네트워크:** `kss`는 설치가 무겁고, `tiktoken`은 첫 실행 시 인코딩 파일을 내려받는다(CI·오프라인 테스트에서 실패 가능). 확인: T10에서 `uv add` 시간·CI 첫 실행 로그 확인. `kss` 설치가 10분을 넘거나 실패하면 decisions.md에 기록하고 정규식 문장 분리로 대체.
8. **소스 URL 유효성·robots.** 정부·중앙은행 RSS 주소가 바뀌었거나 robots가 막을 수 있다. 확인: T7에서 URL당 1회 수동 요청, 실패하면 같은 국가의 다른 A급 소스로 교체(국가 수는 유지).
9. **`uv` 미설치·Docker 필요.** T1 전에 사람이 설치. testcontainers 통합 테스트는 Docker Desktop이 켜져 있어야 한다.

## 범위 축소 시 버릴 순서

기본 범위(T1~T11) 추정 27.5h로 주 25h를 넘는다. 예비 T12~T14(5h)는 이미 범위 밖이다.

1. T14 Prefect deployment → Week 2 (수동 실행으로 주말 기준 상태 충족)
2. T13 `clients.embed` → Week 3 (Resolver가 처음 쓰는 주)
3. T12 `clients.llm.call` → Week 2 첫 태스크 (Week 2의 "Langfuse 연결"과 같이)
4. T7 소스 5개국 15개 → US·KR 6개 (나머지 9개는 Week 4 소스 확장에 합침) · −1h
5. T8 `HtmlListCrawler` → Week 4 (위 6개를 전부 RSS로 고르면 주말 기준 상태에 영향 없음) · −1.5h

4·5까지 적용하면 기본 범위 25h.
