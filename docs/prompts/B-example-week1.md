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

