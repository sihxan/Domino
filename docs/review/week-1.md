# Week 1 리뷰 (프롬프트 C)

- 일자: 2026-11-04 · 대상: `git log --since=2026-11-03` 30개 커밋(565d3c3…ff4b2fc), main
- 기준: `docs/tasks/week-1.md`, `docs/spec/code-phase0.md` §0·§1·§7.7, `docs/spec/tech-phase0.md` §1~§3.1·§4.3·§5·§6.1·§7·§8.2, `docs/spec/decisions.md`, CLAUDE.md 절대 규칙 1~10·완료 정의
- 독립 검증 방법: 코드·명세 직접 대조, `make test` 실행, 로컬 compose의 Postgres·minio·Prefect API 조회(읽기만), 의심 지점은 작은 스크립트로 재현. 실제 외부 수집(`flows/ingest_flow.py`)은 돌리지 않았다.

## 요약

- `make test` 초록: ruff 통과, `mypy --strict` 51개 파일 무오류, import-linter 계약 2개 KEPT, pytest **338 passed** (34초).
- 주말 기준 상태 충족: `raw_documents` chunked 7·skipped 12, `chunks` 18, `stories` 7. Prefect에 기록된 실제 run 1회는 136초에 COMPLETED.
- migration 0001의 SQL 5블록과 Neo4j DDL은 명세 원문과 **글자 단위로 같다**(스크립트로 대조).
- **치명 0건, 높음 3건, 낮음 12건.** 높음 3건은 모두 `ingest_flow`가 실패를 다루는 방식에서 나온다. 셋 다 지금 쌓인 데이터에는 영향이 없지만, 수집을 스케줄로 돌리기(T14) 전에 고쳐야 한다.

## 치명

없음. 비밀 유출, 절대 규칙 위반, 주말 기준 상태 미달은 찾지 못했다.

## 높음

### H1. 문서 저장이 실패해도 `mark_ok`가 `last_ok_at`을 전진시켜 그 문서가 영구 누락된다 — 고치는 데 S

- 점검 항목: 4 멱등성, 8 다음 주 전
- 위치: `flows/ingest_flow.py:92-98` (`except PermanentError` 뒤에도 98행 `store.mark_ok(spec, at=now)` 무조건 실행), `packages/domino_ingest/crawlers/rss.py:45` (`published_at < since`면 버림)
- 문제: decisions.md:99는 "저장하지 않으므로 다음 실행에 피드에 남아 있으면 재시도"라고 적었다. 하지만 다음 실행의 `since`는 방금 기록한 `last_ok_at`(이번 run 시작 시각)이다. 그래서 발행일이 있는 RSS entry는 `rss.py:45`에서 걸러져 다시 오지 않는다. 재시도되는 것은 발행일 없는 entry와 HtmlList 링크뿐이다.
- 최악의 경우: S3 설정 오류(버킷 이름 오타, 키 교체 후 403 AccessDenied)가 나면 `put_immutable`이 `PermanentError`를 던진다(`clients/s3.py:53`). 그러면 모든 소스의 모든 문서가 `document_failed` 경고만 남기고 버려진다. 이때 소스는 `fail_count=0`, `last_ok_at` 전진으로 기록돼 **건강해 보이는 채로 데이터를 잃는다**. Postgres `DataError`(NUL 바이트 등)도 같은 경로를 탄다.
- 재현: `tests/integration/test_ingest_integration.py`의 `engine` fixture 위에서 다음을 실행한다.
  1. `monkeypatch.setattr("domino_ingest.normalizer.put_immutable", lambda *a, **k: (_ for _ in ()).throw(PermanentError("s3 403")))` 상태로 RssCrawler fixture(발행일 있는 entry)를 `run(T0)`.
  2. `sources.last_ok_at == T0`, `fail_count == 0`, `raw_documents` 0행을 확인한다.
  3. monkeypatch를 풀고 `run(T0 + 15m)`를 돌려도 그 entry는 다운로드되지 않는다. 기존 테스트 `test_unexpected_normalize_error_skips_only_that_document`(:428)는 이 동작을 오히려 정답으로 고정한다.
- 고치는 방향:
  - 문서 단위 `PermanentError`가 1건이라도 있으면 `mark_ok`를 하지 않거나, 실패 문서들 중 가장 이른 `published_at`으로 `last_ok_at`을 둔다.
  - 저장된 문서는 `skip_known`이 다시 받지 않으므로 중복 비용은 작다.
  - decisions.md:99의 문구도 함께 고친다.

### H2. 오래 쉰 뒤 첫 실행에서 Destatis(Crawl-delay 30초)가 flow 상한 600초를 넘기면, 그 뒤 소스들이 다시 수집되지 않는 상태(livelock)에 빠진다 — 고치는 데 M

- 점검 항목: 8 다음 주 전(T14 스케줄과 같이)
- 위치:
  - `flows/ingest_flow.py:102` `timeout_seconds=600` — Prefect 3 sync flow도 `flow_engine.py:1331` `timeout(...)`으로 강제한다.
  - `flows/ingest_flow.py:82` `since = state.last_ok_at` — 상한 없음.
  - `packages/domino_ingest/crawlers/base.py:115-120` 기사당 `max(Crawl-delay, 2s)`.
  - `base.py:132-163` `collect`가 전부 받은 뒤에야 반환하므로, 받다가 끊기면 아무것도 저장되지 않는다.
  - `packages/domino_ingest/store.py:51` `ORDER BY id`.
- 근거(실측과 코드에서 추론, 장애 상황 자체는 재현하지 않음):
  - 실제 run 1회는 136초였고, 그중 Destatis 기사 2건의 대기만 60초였다.
  - Destatis는 새 기사 1건마다 30초를 쓴다. 다른 소스가 약 75초를 쓰므로, 한 run에서 Destatis 기사를 약 17건 넘게 받으면 타임아웃이다.
  - 로컬 Mac이 며칠 꺼져 있으면 `since`가 그만큼 과거로 가서 feed의 기사 수가 이 한도를 넘는다.
- 타임아웃이 나면 이렇게 된다:
  1. Destatis는 저장 0건이고 `mark_ok`·`mark_fail`이 둘 다 없다. `fail_count`가 오르지 않으니 경보도 없다.
  2. 다음 run도 같은 `since`로 같은 기사를 다시 받다가 같은 지점에서 끊긴다.
  3. id 순서상 그 뒤의 `src_jp_*`·`src_kr_*`·`src_tw_*`·`src_us_*` 12개 소스가 매 run 처리되지 않는다.
- 고치는 방향: 다음 중 하나 이상.
  - (a) `collect`가 문서를 받는 대로 저장 콜백을 불러 진행분이 남게 한다. 다음 run에서는 `skip_known`이 받은 것을 건너뛰므로 수렴한다.
  - (b) 소스별 시간 예산을 두고, 남은 entry는 다음 run으로 넘긴다.
  - (c) flow 상한과 Crawl-delay 소스 처리 방식을 decisions에 함께 적는다.
- 참고: lookback만 잘라 내면 H1과 같은 누락이 생긴다.

### H3. `wire_origin`과 `n_publishers`가 서로 다른 문자열을 써서, 출처가 하나인 Story가 2로 세어진다 — 고치는 데 S (Week 4 통신사 소스 전까지)

- 점검 항목: 1 명세 일치(tech §5.1 "`n_publishers`는 `coalesce(wire_origin, publisher)` distinct")
- 위치: `packages/domino_ingest/story.py:79-83`, `packages/domino_ingest/store.py:150`
- 문제:
  - 원문 통신사 문서의 첫 300자에 byline이 안 걸리면 `wire_origin=None`이 되고, 집계 키는 `sources.publisher`(예: `Reuters`)가 된다.
  - 같은 Story의 전재는 `detect_wire`가 wires.yaml 키(예: `reuters`)를 돌려준다.
  - `count(DISTINCT coalesce(wire_origin, publisher))`는 두 값을 다르게 보아 2를 낸다. 이 값이 그대로 `confirm_min_publishers=2` 승격(Q3) 조건을 채운다.
  - decisions.md:104(trafilatura가 본문 앞에 제목을 붙여 byline이 300자 밖으로 밀림) 때문에 원문 쪽 byline 미매칭은 실제로 흔하다.
- 재현(실행 확인함): `WireRules(patterns={'reuters':[r'\(Reuters\)']}, publishers={'src_reuters':'Reuters','src_x':'Some Paper'})`에서 byline 없는 같은 본문 2건을 차례로 `detect_wire`에 넣었다. 결과는 `orig.wire_origin=None`, `copy.wire_origin='reuters'`이고, distinct 키는 `{'Reuters','reuters'}`로 2개였다.
- 고치는 방향: 통신사 판정에 `SourceSpec.wire`(tech §3.1 `sources.wire`)를 쓰고 집계도 `coalesce(d.wire_origin, s.wire, s.publisher)`로 한다. 또는 `detect_wire`가 원문 문서의 집계 키와 같은 문자열을 돌려주게 한다. 단위 테스트에 "원문 byline 없음 + 전재" 경우를 추가한다.

## 낮음

| # | 항목 | 위치 | 내용 · 재현 | 크기 |
|---|---|---|---|---|
| L1 | 1 명세 | `flows/ingest_flow.py:74` | `chunk(doc)`를 기본값으로 호출한다. `settings.chunk_tokens`·`chunk_overlap`(tech §2.2)은 어디서도 읽지 않는다(grep 결과 0건). `.env`를 바꿔도 효과가 없다. 같은 줄 근처의 `simhash_threshold`는 설정에서 읽는다. | S |
| L2 | 4 멱등성·7 | `packages/domino_ingest/sources.py:49` | `make seed` 재실행이 `enabled = EXCLUDED.enabled`(YAML 기본 true)로 자동 비활성화된 소스를 되살린다. `fail_count`는 5로 남으므로 다음 실패 한 번에 바로 다시 꺼진다. 운영 상태와 설정이 한 열에 섞여 있다. decisions.md:55에서 의도적으로 정했으므로 판단 목록(아래 7-④)에 올린다. | S |
| L3 | 8 | `flows/ingest_flow.py:81-96`, `crawlers/rss.py:19` | 소스·문서 루프가 `DominoError`만 잡는다. 라이브러리 예외(예: feed 날짜가 비정상일 때 `datetime.fromtimestamp`의 `OverflowError`·`ValueError`, trafilatura 내부 예외)는 flow 전체를 끝낸다. 문제 문서가 다음 run에도 같은 자리에 있으면 H2처럼 뒤 소스들이 처리되지 않는다. 소스 단위 catch-all → `mark_fail`로 막을 수 있다. | S |
| L4 | 6 로그 | `packages/domino_core/clients/db.py:24` | `create_engine(..., hide_parameters=False)`(기본값). SQLAlchemy 오류 문자열에 바인드 파라미터가 약 300자까지 들어간다(`StatementError` 재현: `[parameters: {'text': 'BODY-BODY-…'}]`). `raise … from e`로 연결되므로 Prefect·structlog traceback에 본문 일부가 찍힌다. 지금은 공개 보도자료라 비밀은 아니다. | S |
| L5 | 6 비밀 | `packages/domino_core/settings.py:19,23` | `pg_dsn`·`redis_url`은 비밀번호가 들어가는 DSN인데 `str`이다(명세 §2.2 그대로). 그래서 `repr(Settings())`에 평문으로 나온다. 지금은 Settings를 로그에 찍는 코드가 없다. 명세 개정 판단 대상(7-⑦). | S |
| L6 | 3 테스트 | `tests/unit/test_simhash.py:76-81` | "전재(앞 300자 동일) ≤ 3" 테스트가 **같은 문자열**의 simhash를 비교하므로 항상 0이다(동어반복). decisions.md:104의 제목 접두 문제는 T11 통합 테스트에서야 드러났다. 제목 접두·공백 차이·byline 교체가 있는 변형으로 바꿔야 의미가 있다. | S |
| L7 | 3 테스트 | `tests/integration/test_ingest_integration.py:428-436` | `test_unexpected_normalize_error_skips_only_that_document`가 H1의 동작(실패 문서가 있어도 `fail_count=0`·`last_ok_at` 전진)을 정답으로 고정한다. H1을 고칠 때 같이 바꾼다. | S |
| L8 | 3 테스트 | `.github/workflows/ci.yml:20` | CI가 `pytest tests/unit`만 돈다. T6 범위대로이지만 tech §7.5는 integration job도 요구한다. 멱등성 검증(T11)이 로컬에서만 돈다. | S |
| L9 | 3 테스트 | `pyproject.toml:97-104` | tech §7.1의 "커버리지 ≥ 80% (`domino_core` 제외)"를 측정하는 도구(pytest-cov)가 없다. | S |
| L10 | 5 asof | `packages/domino_core/timeutil.py:29` | `asof_for`가 `get_settings()`를 거치므로, 필수 환경변수(PG_DSN·ANTHROPIC_API_KEY·RESEND_API_KEY 등 16개)가 없으면 순수 함수인데도 실패한다. Week 3 `test_asof.py`·graph 단위 테스트가 전부 env fixture를 필요로 하게 된다(decisions.md:73의 결과). 7-⑥. | S |
| L11 | 2 규칙 4·5 | `pyproject.toml:107-117` | import-linter `root_packages`에 `flows`가 없다. `flows/`가 `anthropic`·`httpx`를 직접 import해도 forbidden 계약에 걸리지 않는다. Week 2 `extract_flow`부터 의미가 생긴다. | S |
| L12 | 8 수율 | 로컬 DB(조회) | 실제 run 1회에서 19건 중 12건이 skipped였다. snapshot을 trafilatura 기본·recall 모드로 다시 추출해 보니 늘어나는 부분은 거의 네비게이션 boilerplate였다. 따라서 `favor_precision=True`는 맞는 선택이고, skip은 정당하다(BBK 국채 공고는 표, MOEF는 본문이 첨부). 다만 다음 둘은 문제다. (1) skip 사유(too_short·lang_undetected)가 행에 남지 않아 12건을 구별할 수 없다. (2) **한국어 chunk가 0개**다(BOK·MSIT는 24h 신규 0, MOEF 보도자료는 첨부 기반이라 쓸 본문 없음). 그리고 `src_de_bafin_press`는 robots.txt `RemoteProtocolError`로 한 번도 성공하지 못했다(`fail_count=1`, `last_ok_at` NULL). | M |

## 점검 항목별 결과

### 1. 명세 일치 — 통과(편차 3건)

| 대상 | 결과 |
|---|---|
| `errors`·`ids`(prefix 16종)·`timeutil`·`Settings` 필드·`SourceSpec`/`RawDoc`/`Document`/`Chunk` 필드 | 명세와 이름·타입 일치 |
| `fetch_html(url, *, timeout=20) -> (html, headers)`, `Crawler.fetch(spec, since)`, `CRAWLERS` | 일치 |
| `normalize`(snapshot 1단계·300자·lang·doc_type 개정 규칙·`text[:2000]` simhash) | 일치(code §1.3 개정판) |
| `simhash64`·`hamming`·`assign_story`(+`now` kw, decisions.md:87)·`detect_wire` | 일치. 다만 집계 키 불일치는 H3 |
| `chunk(doc, *, max_tokens=512, overlap=64)` | 시그니처는 일치. 설정값을 연결하지 않음은 L1 |
| store 11개 함수 이름(week-1 T11) | 일치. `insert_skipped(raw, e, spec)`의 3번째 인자와 추가 함수 2개는 decisions.md:97에 기록 |
| `ingest_flow` 의사코드(code §1.6 개정) | 일치. `with_retry`는 `_crawl`(`flows/ingest_flow.py:42-56`)로 구현, 이름만 다름 |
| migration 0001 SQL 5블록, Neo4j DDL 21줄 | tech §3.1~3.5·§4.3와 글자 단위 동일, 테이블 20개 |

### 2. CLAUDE.md 절대 규칙 1~10 — 통과

| 규칙 | 결과 | 근거 |
|---|---|---|
| 1 의존 방향 | 통과 | `lint-imports` layers 계약 KEPT |
| 2 asof·ASOF_EDGE | 해당 없음 | Neo4j 읽기 쿼리 0개(`domino_graph/queries/` 없음) |
| 3 Neo4j DELETE 금지 | 통과 | `delete`·`detach` grep 0건 |
| 4 LLM은 `llm.call`만, 모델명은 settings | 통과 | `anthropic` import 0건. 모델 리터럴은 `settings.py:34-38`(기본값)과 테스트에만 있음 |
| 5 외부 I/O는 clients 경유 | 통과(L11) | forbidden 계약 KEPT. `flows/`는 계약 밖 |
| 6 결정 기록 | 통과 | 명세 밖 상수·시그니처 변경은 모두 decisions.md에 있음(:5-104) |
| 7 테스트 먼저 | 확인 불가 | 커밋이 구현과 테스트를 같이 담고 있어 순서를 알 수 없다. 태스크마다 테스트는 있음 |
| 8 비밀 | 통과(L4·L5) | `.env`는 git 밖(`.gitignore:2`). compose는 `${VAR:?}`만 쓰고, `.env.example`에는 키 이름만 있음 |
| 9 의존성 고정·기록 | 통과 | 런타임 22개·dev 6개 모두 `==` 고정이고 decisions에 이유가 있음 |
| 10 투자 권유 표현 | 통과 | grep 0건(`test_chunker.py:326`이 fixture를 검사함) |

완료 정의: `make test` 초록, `week-1.md` T1~T11 `[x]`와 해시 있음, 새 모듈 docstring 전부 있음(빈 `__init__.py` 9개는 `from __future__`가 없지만 주석·타입이 없어 무관), mypy strict 통과.

### 3. 테스트 — 통과(L6~L9)

태스크마다 완료 기준에 적힌 테스트가 모두 있고 단언도 구체적이다.

| 태스크 | 확인한 것 |
|---|---|
| T2 | 같은 ms ULID 정렬: 1000개, 실측 2만 개도 정렬 유지. `asof_for` 경계. `SecretStr` repr |
| T3 | SDK mock은 클라이언트 경계에만 쓰고 매핑 로직은 실제로 실행됨 |
| T5 | 테이블 집합 정확 일치, downgrade→upgrade, DDL 2회 후 제약 7·인덱스 9 |
| T7 | URL 확인 표가 커밋 c95b9cc 본문에 있음 |
| T6 | `gh run list`: 이번 주 push 8건 모두 `completed success` |
| T8 | 가짜 시계로 간격·Crawl-delay 검증, 네트워크 0 |
| T9 | 6/7 임계 경계, hamming 3/4 경계를 실제 변형 텍스트로 만듦 |
| T10 | 좌표 원문 일치·누락 없음·overlap 범위 |
| T11 | 실제 Postgres·minio, 2회 실행 행 수 불변, skipped snapshot 1회, 5회 실패 비활성화 |

mock이 피검 구현을 대신하는 테스트는 없었다. 약점은 L6(동어반복)과 L7(H1을 정답으로 고정)이다.

### 4. 멱등성 — 부분 위반(H1·L2)

| 대상 | 결과 |
|---|---|
| ingest | 2회 실행 행 수 불변(테스트). `url_hash`·`(doc_id, idx)` `ON CONFLICT DO NOTHING`. story는 매번 소속 문서에서 다시 계산. snapshot은 `put_immutable`로 1회 |
| `make migrate` | 2회 무오류(테스트) |
| `make seed` | 행 수는 불변이지만 `enabled`를 되돌림(L2) |
| 재실행 안전성 | 문서 실패 시 누락(H1). 저장 전에 끊기면 orphan snapshot이 남는다(새 doc_id). 비용은 작다 |
| extract·graph upsert | 아직 없음(Week 2·3) |

### 5. asof — 해당 없음(L10)

읽기 쿼리가 없다. Python 쪽에 asof 기본값을 숨긴 곳도 없다. `asof_for`는 인자가 필수이고, `ingest_flow(now=None)`의 `now`는 asof가 아니다. Week 3의 `GraphRepo`·`asof.py`·`test_asof.py`가 첫 점검 대상이다.

### 6. 비밀·로그 — 통과(L4·L5)

- structlog가 `SecretStr`를 중첩 포함 마스킹한다(`log.py:19-29`, 테스트 있음).
- 로그에 찍히는 본문은 chunker fallback 경고의 40자 샘플(`chunker.py:114`)뿐이고, 이는 의도된 것이다.
- `mark_fail`의 `last_error`는 예외 문자열(URL 포함) 500자로, 비밀이 들어가지 않는다.

### 7. 명세를 고쳐야 할 수 있는 decisions.md 결정 (판단 목록)

1. **:99 문서 실패 처리**: "다음 실행에서 재시도"라는 전제가 발행일 있는 RSS에서는 거짓이다(H1). code §1.6 의사코드에 "문서 실패가 있으면 `mark_ok` 보류"를 넣을지 결정이 필요하다.
2. **:97 store 시그니처**: `insert_skipped(raw, e, spec)`, `known_url_hashes`, `source_publishers`, `mark_ok(at=)`가 code §1.6에 없다. 명세를 코드에 맞출지 결정.
3. **:14·:88 `StoryHead`/`WireRules`와 "먼저 수집된 문서"=작은 ULID**: code §1.4에 정의를 옮기고, 통신사 판정 필드를 `SourceSpec.wire`로 고정할지 결정(H3).
4. **:55 `make seed`가 `enabled`를 덮어씀**: `enabled`를 운영 상태로 보고 upsert 대상에서 뺄지(L2).
5. **:86 shingle 토큰 단위(CJK 한 글자, 한글 어절)**: story·전재 임계값 6·3이 이 정의에 달려 있는데 명세는 "3-gram"만 적었다. code §1.4에 옮길지.
6. **:73 `asof_for`가 전역 Settings 의존**: 순수 함수로 두려면 `report_hour_kst`만 읽는 경량 경로가 필요하다(L10).
7. **명세 §2.2 `pg_dsn`·`redis_url`이 `str`**: §2.2 본문의 "비밀은 SecretStr라 로그에 안 찍힌다"와 어긋난다(L5).
8. **:45 `Settings.tz`는 시스템 `TZ`에 덮여 읽히지 않는 필드다**: §2.2에서 지우거나 이름을 바꿀지.
9. **:98 `stories.first_seen/last_seen`=수집 시각, 대표 simhash=첫 문서 고정**: 72h 창과 Storyline 60일 규칙의 시간 기준이 문서 발행일이 아니다. tech §3.1 주석에 적을지.
10. **:82 skip 사유 미저장**: `TooShortError`와 `LangUndetectedError`가 행에서 구별되지 않는다. 사유 열(DDL 0002)이 필요한지(L12).

### 8. 다음 주로 넘어가면 안 되는 것

주말 기준 상태("`make up` 후 문서가 Postgres에 쌓인다")는 충족했다. 다만 다음 세 가지는 Week 2 추출 착수와 함께, 또는 그 전에 처리해야 한다.

- **T14 미착수**: 수집이 스케줄로 돌지 않는다. 지금 DB의 chunk 18개가 Week 2 추출 입력의 전부다. "쌓인다"가 계속 유지되려면 deployment가 필요하다(S, 1h).
- **H1·H2**: T14로 무인 실행을 켜는 순간부터 드러나는 실패 경로다. 스케줄과 같은 주에 고친다.
- **한국어 입력 0**(L12): Week 2 gold 30건 첫 측정(tech §7.2는 한·영 각 50 기준)에 쓸 한국어 chunk가 없다. 수집을 누적하거나 KR 소스를 보강해야 한다.

## 다음 주 A 프롬프트에 넣을 한 줄

1. `Week 2 첫 태스크: T14(ingest deployment */15·동시성 1)를 끝내고, 같은 묶음에서 ingest_flow의 실패 경로 두 개를 닫는다 — 문서 실패가 있으면 last_ok_at을 전진시키지 않기(review-1 H1), Crawl-delay 소스가 600초 상한을 넘겨 뒤 소스를 굶기지 않게 받은 문서를 즉시 저장(H2).`
2. `extract 착수 전 입력 점검: 현재 chunk 18개 중 한국어 0개 — gold 30의 한국어 몫을 수집 누적 또는 KR 소스 보강으로 Week 2 중반까지 확보하고, chunk()에 settings.chunk_tokens/chunk_overlap을 연결하며 CI에 integration job을 추가한다(L1·L8·L12).`
3. `wire_origin·n_publishers 키 불일치(review-1 H3)와 decisions 104(제목 접두로 byline·text[:300] 비교 실패)를 한 예비 태스크로 등록해, 승격(Q3)이 처음 돌기 전(Week 3) 또는 늦어도 통신사 소스가 들어오는 Week 4 전에 닫는다.`
