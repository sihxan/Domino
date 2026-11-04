# 기술 명세서 — Phase 0 (발행 시작)

Nov 4, 2026 · @KDST

## 0. 범위·전제·비목표

이 문서는 **Phase 0 — 매일 06:00 KST에 비공개 베타 10명에게 리포트를 발행하기까지**를 코드 수준으로 정의한다. 설계 결정의 근거는 앞 탭들(v2 검토, 시간 지평, 리포트 제품 설계, red-team, 실현가능성)에 있고, 여기서는 **무엇을 어떻게 만드나**만 쓴다. 두 문서(기술·코드)를 합치면 구현자가 질문 없이 12주를 갈 수 있어야 한다. 모호한 곳이 있으면 이 문서의 결함이다.

### 0.1 만드는 것 (Phase 0)

| 영역 | 포함 | 규모 |
| --- | --- | --- |
| 수집 | 소스 레지스트리 15개국 45개 소스(A급 1차 출처 중심), RSS·공시·통계 API, GDELT 발견 경로, 직접 fetch, story dedupe | 500 chunk/일 |
| 추출 | 사전 분류 → Haiku 5.5 구조화 추출 → grounding → validator → hedge 강등 | 트리플 300\~800/일 |
| 그래프 | Neo4j bi-temporal (Entity·Event·Source·Evidence·Story·Storyline), Resolver 3단계, 검토 큐 | Entity 5천\~2만 |
| 신호 | Trend 2신호(사건 빈도·지표 모멘텀), Thesis 10개 수동 + signpost 코드 판정, 세계 흐름 모니터 **빠른 축** 100 시계열 + naive nowcast | 야간 배치 |
| 리포트 agent | Claude Agent SDK: lead + Scout·Tracer·Verifier, MCP 도구 11개, master claim graph | 일 1회, $3\~5 |
| 발행 | 페르소나 2(World Brief L0 Brief, Industry Chain L2 Standard), slice → editorial → 게이트 → 이메일 + 웹 | 06:00 KST |
| 기록 | 예측 장부, Brier, 공개 적중률 페이지, 자기검증 꼬리, LLM 베이스라인 겹침률 | 첫날부터 |

### 0.2 안 만드는 것 (Phase 1·2로)

World Model·stock-flow·되감기, 조사 agent·`domino-data` 도구, 충격 템플릿, 컨센서스 4원천(Phase 0은 LLM 베이스라인만), divergence 카드, Analyst·Critic subagent, Ask panel, As-of slider, 사례 라이브러리, 40개국, 속보 알림, 결제. 단 §8의 **확장 계약**(테이블·인터페이스·훅)은 Phase 0에 미리 둔다 — 나중에 마이그레이션하지 않기 위해.

### 0.3 성공 기준 (Week 12)

1. 12주차에 **연속 14일** 06:00 발행 (게이트 실패로 보류된 날 포함 ≤ 2일).
2. 각주 커버리지 100%, 금지어 0건, grounding 성공률 ≥ 95%.
3. 15개국 A급 소스 일일 커버 ≥ 80%.
4. Thesis 10개 + signpost 공개, 예측 장부에 ≥ 30건 사전 등록.
5. LLM 베이스라인 겹침률 첫 측정값 보고 (목표가 아니라 측정).
6. 베타 10명 주간 열람률 측정 시작.

### 0.4 용어

| 용어 | 뜻 |
| --- | --- |
| Document | 수집된 원문 1건 (URL 단위) |
| Story | 같은 사건을 다룬 Document 묶음 (SimHash 클러스터) |
| Chunk | Document를 512 토큰으로 자른 추출 단위 |
| Triple | `cause → effect` 인과 1건 + 메타 (추출 결과) |
| Evidence | Triple의 근거 span (quote + char offset + snapshot 해시) |
| Entity / Event / Storyline | 그래프 정점 (제품 설계 탭 §5) |
| asof | 모든 조회의 기준 시각. bi-temporal 필터의 입력 |
| master claim graph | lead agent가 만드는 하루치 리포트 재료 JSON (페르소나 무관) |
| Persona | 레이어 × 렌즈 × 깊이 설정값 |
| Gate | 렌더 후 발행 전 자동 검사. 실패 = 보류 |

## 1. 런타임 토폴로지

VPS 1대(4 vCPU · 16 GB · 160 GB SSD, Hetzner CPX41급)에 docker-compose로 전부 올린다. 베타 10명·일 500 chunk에서는 충분하고, embedder(bge-m3 CPU)가 가장 무거우므로 부족하면 그것만 2대째로 분리한다. Neo4j는 Community Edition 자체 호스팅(AuraDB Free는 GDS가 없어 PPR을 못 돌린다).

&#91;embedded content: Phase 0 런타임 토폴로지\]

### 1.1 서비스 목록

| 서비스 | 이미지 | 역할 | 포트 | 리소스 상한 |
| --- | --- | --- | --- | --- |
| `postgres` | postgres:16 | 문서·chunk·시계열·장부·리포트·job 상태 | 5432 (내부) | 2 GB |
| `neo4j` | neo4j:5-community + GDS 플러그인 | 인과 그래프 | 7687/7474 (내부) | 4 GB heap |
| `redis` | redis:7-alpine | 캐시·분산 락·rate limit | 6379 (내부) | 512 MB |
| `minio` | bitnamilegacy/minio (Hub의 minio/minio 저장소 삭제) | 원문 snapshot(불변)·리포트 HTML | 9000 (내부) | 디스크 |
| `embedder` | ghcr.io/huggingface/text-embeddings-inference:cpu | bge-m3 1024-dim | 8080 (내부) | 4 vCPU |
| `prefect-server` | prefecthq/prefect:3 | 스케줄·UI | 4200 (내부, SSH 터널) | 1 GB |
| `prefect-worker` | 자체 빌드 (모노레포) | 모든 flow 실행 | — | 2 GB |
| `agent-runner` | 자체 빌드 + claude-agent-sdk | 리포트 agent (worker가 실행 트리거) | — | 1 GB |
| `api` | 자체 빌드 (FastAPI) | 리포트·기록·검토큐 API, webhook | 8000 → Caddy 443 | 512 MB |
| `web` | 자체 빌드 (Next.js) | Report view, 적중률, 검토큐 UI | 3000 → Caddy 443 | 512 MB |
| `langfuse` | langfuse/langfuse v2 (self-host, 같은 postgres의 `langfuse` DB) | LLM trace·eval | 3001 (내부) | 1 GB |
| `caddy` | caddy:2 | TLS 터미네이션, 리버스 프록시 | 80/443 | — |

### 1.2 데이터 흐름 한 바퀴 (하루)

1. **15분마다** `ingest_flow`: 소스 45개 + GDELT 발견 → `raw_documents` 적재, snapshot을 minio에, SimHash로 `stories` 배정, chunk 생성 → `extract_flow` 트리거.
2. **따라서** `extract_flow`: chunk당 사전 분류 → Haiku 추출 → grounding·validator → Resolver → Neo4j MERGE(candidate) → 독립 출처 2 이상이면 confirmed 승격. 실패는 `extraction_errors`.
3. **1시간마다** `storyline_flow`: 신규 Event를 활성 Storyline에 연결.
4. **04:30 KST** `nightly_flow`: confidence 재계산, Trend 2신호, 빠른 축 시계열 수집 + naive nowcast, signpost 판정, PPR 투영 캐시, LLM 베이스라인 질문 생성.
5. **06:00 KST** `report_flow`: agent-runner가 master claim graph 생성(상한 25분) → 페르소나별 slice → editorial → 렌더 → 게이트 → 06:40\~07:00 발송. 게이트 실패 시 보류 알림(사람 개입 없이 안전).
6. **07:00\~** 독자가 열면 webhook으로 `deliveries.opened_at` 기록. 각주 클릭은 web의 Evidence 화면으로.
7. **상시** `api`가 검토 큐(Resolver unsure·방향 모순·신고)를 노출, 사람이 하루 1회 처리.

컴퓨트는 전부 상태 없음(stateless)이고 상태는 저장소 5개에만 있다. 어떤 컴퓨트 컨테이너도 재시작으로 복구되고, Prefect가 실패한 flow를 재실행한다(멱등 키는 §5).

## 2. 레포 구조·패키지 경계·설정

모노레포 하나, Python 3.12, 패키지 매니저 `uv`, 루트 `pyproject.toml` 단일 프로젝트(`packages/domino_*`를 그 안의 패키지 디렉터리로). 프론트는 `apps/web`에 Node 22·pnpm. 의존 방향은 **아래로만**: `core ← ingest ← extract ← resolve ← graph ← signals ← agent ← report`. 상위 패키지가 하위를 import하고 역방향은 금지(`import-linter`로 CI에서 검사).

```text
domino/
├── pyproject.toml              # uv 단일 프로젝트, ruff, mypy(strict), pytest, import-linter
├── docker-compose.yml          # §1.1의 12개 서비스
├── Caddyfile
├── .env.example
├── Makefile                    # make up / migrate / seed / test / report-dry-run
├── packages/
│   ├── domino_core/            # settings, ids, time, errors, 공통 Pydantic 모델, db 세션, 클라이언트(anthropic·neo4j·redis·s3·embedder·langfuse)
│   ├── domino_ingest/          # sources/ 로더, crawlers(rss, gdelt, edgar, dart, fred, eia, portwatch, interconnection, prediction_markets, generic_html), normalizer, simhash, chunker
│   ├── domino_extract/         # schema.py, prompts/, classifier.py, extractor.py, grounding.py, validator.py, hedge_lexicon.yaml
│   ├── domino_resolve/         # embedder client, alias store, judge.py, resolver.py
│   ├── domino_graph/           # ddl/*.cypher, repo.py (GraphRepo), asof.py, storyline.py, queries/*.cypher
│   ├── domino_signals/         # trend.py, flows/ (series collectors), nowcast.py, scoring.py, signposts.py, baseline.py
│   ├── domino_agent/           # tools/ (MCP 도구 11개), agents.py (AgentDefinition), prompts/, runner.py, budget.py
│   ├── domino_report/          # claim_graph.py (schema), slice.py, editorial.py, render/ (jinja2 templates), gate.py, deliver.py, personas.yaml
│   └── domino_eval/            # gold/, regression.py, baseline_overlap.py, ledger.py, brier.py
├── flows/                      # Prefect: ingest_flow.py, extract_flow.py, storyline_flow.py, nightly_flow.py, report_flow.py, deploy.py
├── apps/
│   ├── api/                    # FastAPI: routers/{reports,ledger,review,webhooks,health}.py
│   └── web/                    # Next.js 15: /r/[date]/[persona], /record, /review, /e/[evidence_id]
├── sources/                    # 국가별 YAML: us.yaml kr.yaml tw.yaml jp.yaml cn.yaml de.yaml nl.yaml gb.yaml fr.yaml in.yaml vn.yaml mx.yaml sa.yaml ae.yaml au.yaml + wires.yaml
├── theses/                     # 사람이 쓰는 Thesis JSON 10개 (스키마는 코드 명세서 §4)
├── storylines/seed.yaml        # Storyline 20개 시드
├── migrations/                 # alembic (Postgres)
├── tests/                      # unit/ integration/ golden/
└── docs/runbook.md
```

### 2.1 패키지 책임 한 줄씩

| 패키지 | 책임 | 외부 의존 |
| --- | --- | --- |
| `domino_core` | 설정·ID(ULID)·시간(KST↔UTC)·에러 계층·DB 세션·외부 클라이언트 래퍼. **비즈니스 로직 없음** | pydantic-settings, sqlalchemy, neo4j, redis, boto3, httpx, anthropic, langfuse |
| `domino_ingest` | 소스 레지스트리 → Document·Story·Chunk 생성 | feedparser, httpx, trafilatura(본문 추출), simhash |
| `domino_extract` | Chunk → ValidatedTriple\[\] | anthropic, rapidfuzz |
| `domino_resolve` | 문자열 → canonical entity\_id | numpy |
| `domino_graph` | Neo4j 읽기·쓰기 전부 (다른 패키지는 Cypher를 직접 쓰지 않는다) | neo4j |
| `domino_signals` | 야간 계산 전부 (Trend·흐름·nowcast·signpost·점수·LLM 베이스라인 질문) | numpy, pandas, statsmodels |
| `domino_agent` | 리포트 agent 실행과 MCP 도구 | claude-agent-sdk |
| `domino_report` | master claim graph → 발행물 | jinja2, markdown-it, resend |
| `domino_eval` | 품질 측정 전부 | — |

### 2.2 설정 (`domino_core/settings.py`)

```python
class Settings(BaseSettings):
    env: Literal["dev", "prod"] = "dev"
    # stores
    pg_dsn: SecretStr                # postgresql+psycopg://... (비밀번호 포함)
    neo4j_uri: str; neo4j_user: str; neo4j_password: SecretStr
    redis_url: SecretStr             # 비밀번호 포함 가능
    s3_endpoint: str; s3_bucket: str = "domino"; s3_key: SecretStr; s3_secret: SecretStr
    embedder_url: str = "http://embedder:8080"
    langfuse_host: str; langfuse_public_key: str; langfuse_secret_key: SecretStr
    # llm
    anthropic_api_key: SecretStr
    model_extract: str = "claude-haiku-5-5"
    model_judge: str = "claude-sonnet-5-5"
    model_lead: str = "claude-sonnet-5-5"
    model_worker: str = "claude-haiku-5-5"
    model_editorial: str = "claude-sonnet-5-5"
    # budgets (USD)
    budget_extract_daily: float = 2.0
    budget_report_run: float = 5.0
    budget_judge_daily: float = 1.0
    # pipeline
    chunk_tokens: int = 512; chunk_overlap: int = 64
    simhash_threshold: int = 6        # 64-bit Hamming distance
    resolver_auto_cos: float = 0.92; resolver_judge_cos: float = 0.80
    confirm_min_publishers: int = 2
    # report
    report_hour_kst: int = 6          # asof_for()와 report_flow cron이 이 값 하나를 읽는다
    personas_path: Path = Path("packages/domino_report/personas.yaml")
    # delivery
    resend_api_key: SecretStr; from_email: str
    beta_list_path: Path = Path("beta.yaml")
```

`.env`는 git 밖, `.env.example`은 키 없이 전부 나열. prod는 `/etc/domino/.env`를 docker-compose `env_file`로. 비밀은 `SecretStr`이라 로그에 안 찍힌다. **모델명은 설정이지 코드가 아니다** — 모델 교체는 `.env` 한 줄 + 회귀 테스트(§7) 통과.

### 2.3 ID·시간 규약

- 모든 ID는 ULID 문자열(정렬 가능, 26자), 접두어로 타입 표시: `doc_`, `sty_`, `chk_`, `tri_`, `ev_`, `ent_`, `evt_`, `src_`, `sl_`(storyline), `th_`, `sp_`(signpost), `pred_`, `rpt_`, `run_`, `rq_`(review_queue), `tr_`(trends).
- `sources.id`는 예외로 ULID가 아니라 `sources/*.yaml`의 사람 키(`src_us_fed_press`)다 — §3.1 주석의 `src_<ulid>`는 이 뜻.
- SimHash(64-bit unsigned)는 Postgres `bigint`(signed)에 two's complement로 저장하고, 읽을 때 unsigned로 복원한다.
- `stories.first_seen`·`last_seen`은 소속 문서의 **수집 시각**(`fetched_at`) 기준이고(발행일 아님), 대표 `simhash`는 첫 문서 것으로 고정한다. 72h Story 창은 이 시각을 쓴다.
- 저장은 전부 UTC `timestamptz`, 표시만 KST. "오늘"은 `report_date = (now_utc → KST).date()`.
- Neo4j의 시간 속성은 ISO-8601 UTC 문자열이 아니라 `datetime()` 타입으로 저장 (range index가 걸린다).

## 3. 데이터 모델 — Postgres

Postgres는 **문서·시계열·장부·리포트·운영 상태**를 가진다. 인과 그래프는 Neo4j(§4)에만 있고, Postgres는 그래프 ID를 문자열로만 참조한다(FK 없음). 전부 alembic 마이그레이션 `0001_phase0.py` 하나로 만든다.

### 3.1 수집

```sql
CREATE TYPE source_grade AS ENUM ('A','B','C');
CREATE TYPE doc_type AS ENUM ('NEWS','REPORT','PAPER','FILING','RELEASE','DATA');

CREATE TABLE sources (
  id            text PRIMARY KEY,            -- src_<ulid>, sources/*.yaml의 key와 동일
  country       char(2) NOT NULL,            -- ISO-3166 alpha-2
  region        text NOT NULL,               -- NA|EU|CN|EA|ME|EM
  belts         text[] NOT NULL DEFAULT '{}',
  publisher     text NOT NULL,
  grade         source_grade NOT NULL,
  state_controlled boolean NOT NULL DEFAULT false,
  wire          text,                        -- 통신사면 이름 (reuters|ap|afp|xinhua|yonhap|...)
  kind          text NOT NULL,               -- rss|html|edgar|dart|fred|eia|portwatch|interconnection|predmarket|gdelt
  url           text NOT NULL,
  cadence_min   int NOT NULL DEFAULT 15,
  parser        text NOT NULL,               -- crawlers/ 모듈의 클래스 이름
  lang          char(2) NOT NULL,
  enabled       boolean NOT NULL DEFAULT true,
  last_ok_at    timestamptz, last_error text, fail_count int NOT NULL DEFAULT 0
);

CREATE TABLE raw_documents (
  id            text PRIMARY KEY,            -- doc_<ulid>
  source_id     text NOT NULL REFERENCES sources(id),
  url           text NOT NULL,
  url_hash      char(64) NOT NULL UNIQUE,    -- sha256(normalized url)
  title         text,
  published_at  timestamptz,
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  lang          char(2),
  doc_type      doc_type NOT NULL,
  snapshot_key  text NOT NULL,               -- minio: raw/<yyyy>/<mm>/<doc_id>.html
  snapshot_sha  char(64) NOT NULL,
  text          text NOT NULL,               -- 정규화된 본문 (trafilatura)
  text_sha      char(64) NOT NULL,
  simhash       bigint NOT NULL,
  story_id      text,                        -- sty_<ulid>, NULL이면 미배정
  wire_origin   text,                        -- 전재 판정 결과
  status        text NOT NULL DEFAULT 'new'  -- new|chunked|extracted|skipped|error
);
CREATE INDEX ON raw_documents (fetched_at DESC);
CREATE INDEX ON raw_documents (story_id);
CREATE INDEX ON raw_documents (status) WHERE status IN ('new','chunked');

CREATE TABLE stories (
  id            text PRIMARY KEY,            -- sty_<ulid>
  first_seen    timestamptz NOT NULL,
  last_seen     timestamptz NOT NULL,
  title         text NOT NULL,               -- 대표 제목 (최고 등급 문서)
  simhash       bigint NOT NULL,             -- 대표 simhash
  n_docs        int NOT NULL DEFAULT 1,
  n_publishers  int NOT NULL DEFAULT 1,      -- distinct publisher, 전재 제외
  languages     text[] NOT NULL DEFAULT '{}',
  countries     text[] NOT NULL DEFAULT '{}',
  neo4j_id      text                          -- Neo4j Story 노드 id (동일 값)
);

CREATE TABLE chunks (
  id            text PRIMARY KEY,            -- chk_<ulid>
  doc_id        text NOT NULL REFERENCES raw_documents(id),
  idx           int NOT NULL,
  char_start    int NOT NULL, char_end int NOT NULL,
  text          text NOT NULL,
  n_tokens      int NOT NULL,
  is_causal     boolean,                     -- 사전 분류 결과, NULL=미분류
  status        text NOT NULL DEFAULT 'new', -- new|extracted|dropped|error
  UNIQUE (doc_id, idx)
);
```

마이그레이션 `0002_skip_reason.py` (리뷰 week-1 ⑩):

```sql
ALTER TABLE raw_documents ADD COLUMN skip_reason text;   -- status='skipped'일 때 too_short|lang_undetected|…, 그 외 NULL
```

### 3.2 추출·검토

```sql
CREATE TABLE extraction_runs (
  id            text PRIMARY KEY,            -- run_<ulid>
  chunk_id      text NOT NULL REFERENCES chunks(id),
  model         text NOT NULL, prompt_version text NOT NULL,
  input_tokens  int, output_tokens int, cost_usd numeric(8,5),
  n_triples     int NOT NULL, n_dropped int NOT NULL,
  raw_json      jsonb NOT NULL,              -- 모델 원출력 (디버깅·회귀용)
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (chunk_id, prompt_version)          -- 멱등 키
);

CREATE TABLE extraction_errors (
  id bigserial PRIMARY KEY, chunk_id text NOT NULL, run_id text,
  stage text NOT NULL,                       -- schema|grounding|validator|resolver|graph
  reason text NOT NULL, payload jsonb, created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE review_queue (
  id            text PRIMARY KEY,            -- rq_<ulid>
  kind          text NOT NULL,               -- resolver_unsure|direction_conflict|user_flag|storyline_unsure
  payload       jsonb NOT NULL,              -- 판정에 필요한 전부 (이름·후보·evidence 3개)
  status        text NOT NULL DEFAULT 'open',-- open|decided|skipped
  decision      jsonb,                       -- {action, target_id, note}
  decided_by    text, decided_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON review_queue (status, created_at);
```

### 3.3 시계열·신호

```sql
CREATE TABLE series (
  id            text PRIMARY KEY,            -- 예: portwatch.hormuz.transits, fred.DGS10, edgar.<cik>.rpo
  axis          text NOT NULL,               -- fast|slow
  provider      text NOT NULL, provider_key text NOT NULL,
  unit          text, freq text NOT NULL,    -- D|W|M|Q
  entity_id     text,                        -- 연결된 Neo4j Entity (nullable)
  belt          text, region text,
  enabled       boolean NOT NULL DEFAULT true
);

CREATE TABLE series_points (
  series_id     text NOT NULL REFERENCES series(id),
  observed_at   date NOT NULL,               -- 현실 시점
  available_at  timestamptz NOT NULL,        -- 우리가 받은 시점 (bi-temporal 근사)
  value         double precision NOT NULL,
  PRIMARY KEY (series_id, observed_at, available_at)
);
CREATE INDEX ON series_points (series_id, observed_at DESC);

CREATE TABLE trends (
  id            text PRIMARY KEY,            -- tr_<ulid>
  subject_kind  text NOT NULL,               -- entity|storyline
  subject_id    text NOT NULL,
  window_days   int NOT NULL,                -- 7|30|90
  computed_for  date NOT NULL,
  z_freq double precision, z_mom double precision, z_attn double precision, z_narr double precision,
  trend         double precision NOT NULL,
  direction     text NOT NULL,               -- rising|falling|flat
  regime        text NOT NULL,               -- normal|elevated|extreme
  persistence_days int NOT NULL,
  transition    text,                        -- 'normal->elevated' 등, 어제 대비
  top_events    jsonb,                       -- [{event_id, delta_z}]
  UNIQUE (subject_kind, subject_id, window_days, computed_for)
);

CREATE TABLE nowcasts (
  series_id text NOT NULL, made_on date NOT NULL, horizon_steps int NOT NULL,
  model text NOT NULL,                       -- naive|seasonal_naive|ets
  yhat double precision NOT NULL, lo double precision, hi double precision,
  actual double precision, resolved_at timestamptz,
  PRIMARY KEY (series_id, made_on, horizon_steps, model)
);
```

### 3.4 Thesis·예측 장부

```sql
CREATE TABLE theses (
  id            text PRIMARY KEY,            -- th_<ulid> (theses/*.json의 id)
  title         text NOT NULL, statement text NOT NULL,
  layer         text NOT NULL,               -- L0|L1|L2|L3 (어느 레이어에서 노출)
  horizon_end   date NOT NULL,
  status        text NOT NULL,               -- proposed|active|strengthening|weakening|confirmed|refuted|retired
  confidence    double precision NOT NULL,
  drivers       text[] NOT NULL, chain_edge_ids text[] NOT NULL DEFAULT '{}',
  created_by    text NOT NULL,               -- human|agent
  review_log    jsonb NOT NULL DEFAULT '[]',
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE signposts (
  id            text PRIMARY KEY,            -- sp_<ulid>
  thesis_id     text NOT NULL REFERENCES theses(id),
  statement     text NOT NULL,
  metric_kind   text NOT NULL,               -- series|trend|event_count
  metric_ref    text NOT NULL,               -- series.id | trend subject | storyline id
  op            text NOT NULL,               -- >=|<=|>|<
  threshold     double precision NOT NULL,
  due           date NOT NULL,
  falsifies     boolean NOT NULL DEFAULT false,
  status        text NOT NULL DEFAULT 'pending', -- pending|hit|miss
  resolved_at   timestamptz, resolved_value double precision
);

CREATE TABLE predictions (
  id            text PRIMARY KEY,            -- pred_<ulid>
  created_on    date NOT NULL,
  origin_kind   text NOT NULL,               -- signpost|report_item|nowcast
  origin_id     text NOT NULL,
  statement     text NOT NULL,
  metric_kind   text NOT NULL, metric_ref text NOT NULL, op text NOT NULL, threshold double precision NOT NULL,
  due           date NOT NULL,
  p             double precision NOT NULL,   -- 우리 확률
  baseline_p    double precision,            -- 컨센서스/베이스라인 확률 (Phase 0: NULL 또는 LLM 베이스라인)
  outcome       int,                         -- 1|0, NULL=미해소
  resolved_at   timestamptz,
  brier         double precision,            -- (p-outcome)^2
  public        boolean NOT NULL DEFAULT true
);
CREATE INDEX ON predictions (due) WHERE outcome IS NULL;
```

### 3.5 리포트·발행

```sql
CREATE TABLE report_runs (
  id            text PRIMARY KEY,            -- rpt_<ulid>
  report_date   date NOT NULL UNIQUE,
  asof          timestamptz NOT NULL,
  status        text NOT NULL,               -- running|gated|published|held|failed
  claim_graph   jsonb,                       -- master claim graph (코드 명세서 §6)
  agent_trace_id text, cost_usd numeric(8,4), started_at timestamptz, finished_at timestamptz,
  gate_report   jsonb
);

CREATE TABLE report_outputs (
  id            text PRIMARY KEY,
  run_id        text NOT NULL REFERENCES report_runs(id),
  persona       text NOT NULL,               -- personas.yaml key
  html_key      text NOT NULL, md_key text NOT NULL,   -- minio keys
  gate_passed   boolean NOT NULL, gate_detail jsonb,
  n_items       int NOT NULL, footnote_coverage double precision NOT NULL,
  baseline_overlap double precision,         -- LLM 베이스라인 겹침률 (0~1)
  UNIQUE (run_id, persona)
);

CREATE TABLE subscribers (
  id text PRIMARY KEY, email text NOT NULL UNIQUE, persona text NOT NULL,
  active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(),
  last_read_run_id text                      -- "바뀐 것만" diff 기준
);

CREATE TABLE deliveries (
  id text PRIMARY KEY, output_id text NOT NULL REFERENCES report_outputs(id),
  subscriber_id text NOT NULL REFERENCES subscribers(id),
  provider_msg_id text, sent_at timestamptz, opened_at timestamptz, clicked_at timestamptz,
  UNIQUE (output_id, subscriber_id)
);

CREATE TABLE job_runs (                       -- Prefect와 별개로 멱등·감사용
  flow text NOT NULL, key text NOT NULL,     -- 예: ('extract', chunk_id) / ('report', '2026-11-04')
  status text NOT NULL, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  error text, PRIMARY KEY (flow, key)
);

CREATE TABLE llm_baseline_checks (             -- 코드 명세서 §7.3
  id bigserial PRIMARY KEY, run_id text NOT NULL, item_idx int NOT NULL,
  question text NOT NULL, model text NOT NULL, answer text NOT NULL,
  overlap double precision NOT NULL,         -- 0~1
  created_at timestamptz NOT NULL DEFAULT now()
);
```

### 3.6 보존·크기

| 테이블 | 성장 | 보존 |
| --- | --- | --- |
| raw\_documents / chunks | 일 300 문서 · 500 chunk → 월 \~200 MB (text 포함) | 영구 (snapshot은 minio) |
| extraction\_runs.raw\_json | 월 \~100 MB | 90일 후 raw\_json NULL로 비움 |
| series\_points | 100 시계열 × 일 1 → 무시 가능 | 영구 |
| trends | 대상 500 × 윈도 3 × 일 1 → 월 45k 행 | 영구 (history 역할) |
| report\_runs.claim\_graph | 일 1 × \~200 KB | 영구 — 재현·회귀의 근거 |

## 4. 데이터 모델 — Neo4j

노드 6종(`Entity`, `Event`, `Source`, `Evidence`, `Story`, `Storyline`), 관계 7종. **삭제 없음** — 모든 변경은 새 레코드 + 이전 레코드의 `expired_at`/`invalid_at`. 모든 조회는 `asof`를 받는다. `domino_graph/ddl/0001_schema.cypher`를 부팅 시 멱등 실행.

### 4.1 노드 속성

| 라벨 | 필수 | 선택 | 제약·인덱스 |
| --- | --- | --- | --- |
| `Entity` | `id`, `name`, `type` ∈ {INDICATOR, ASSET, INDUSTRY, TECHNOLOGY, POLICY, COMPANY, COMMODITY, INFRA, STRUCTURAL}, `region` (글로벌이면 `'GL'`), `embedding` float\[1024\], `created_at` | `aliases[]`, `tickers[]`, `series_id`, `description`, `canonical_of` (ALIAS일 때 대상 id) | `id` UNIQUE; `(name, type, region)` UNIQUE; vector `entity_embedding`; fulltext `entity_text` ON name, aliases |
| `Event` | `id`, `title`, `occurred_at`, `category` ∈ {MACRO, TECH, POLICY, CORPORATE, GEO}, `country`, `region`, `salience` 0\~1, `created_at` | `actors[]` (Entity id), `action_type` (CAMEO 대분류), `scheduled_at`, `story_id`, `expects_primary` bool, `narrative_split` bool, `summary` | `id` UNIQUE; range on `occurred_at`, `scheduled_at` |
| `Source` | `id`, `url`, `publisher`, `grade`, `published_at`, `doc_id` | `lang`, `country`, `state_controlled`, `wire_origin`, `doc_type` | `url` UNIQUE |
| `Evidence` | `id`, `edge_id`, `quote`, `char_start`, `char_end`, `chunk_id`, `snapshot_sha`, `llm_confidence`, `claim_type` ∈ {attribution, fact}, `extracted_at`, `embedding` float\[1024\] | `retracted` bool | `id` UNIQUE; index on `edge_id`; vector `evidence_embedding` |
| `Story` | `id`, `first_seen`, `last_seen`, `n_publishers`, `title` | `languages[]`, `countries[]` | `id` UNIQUE |
| `Storyline` | `id`, `title`, `started_at`, `status` ∈ {active, dormant, closed}, `state`, `summary`, `embedding` | `key_entities[]`, `summary_updated_at` | `id` UNIQUE; vector `storyline_embedding` |

### 4.2 관계 속성

| 관계 | 방향 | 필수 | 선택 |
| --- | --- | --- | --- |
| `CAUSES` | `(Entity)→(Entity)` | `id`, `mechanism`, `direction` ∈ {UP, DOWN, VOLATILITY, NEUTRAL}, `polarity`, `relation_kind` ∈ {CAUSAL, CORRELATION, SPECULATIVE, CONDITIONAL}, `causal_type` ∈ {DIRECT, ENABLING, BOTTLENECK, SUBSTITUTION}, `horizon` ∈ {ultra\_short, short, long}, `status` ∈ {candidate, confirmed}, `confidence` 0\~1, `impact` 0\~1, `evidence_count`, **`valid_at`, `invalid_at`, `created_at`, `expired_at`** | `lag_days`, `persistence`, `stat_support`, `stat_tested_at` |
| `FROM` | `(Evidence)→(Source)` | — | — |
| `PART_OF` | `(Source)→(Story)` | — | — |
| `TRIGGERED` | `(Event)→(Entity)` | `direction` | — |
| `IN_STORYLINE` | `(Event)→(Storyline)` | `position`, `delta` ∈ {escalated, de-escalated, confirmed, reversed}, `linked_by` ∈ {rule, llm, human} | — |
| `ALIAS_OF` | `(Entity)→(Entity)` | `similarity`, `decided_by` ∈ {rule, embedding, llm, human}, `created_at` | — |
| `SCHEDULED` | `(Event)→(Event)` | — | — |

Evidence↔CAUSES 연결은 관계가 아니라 `Evidence.edge_id = CAUSES.id` 속성 join이다 (Neo4j는 관계→노드 관계가 없다).

### 4.3 DDL

```cypher
CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (n:Entity) REQUIRE (n.name, n.type, n.region) IS UNIQUE;
CREATE CONSTRAINT event_id IF NOT EXISTS FOR (n:Event) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT source_url IF NOT EXISTS FOR (n:Source) REQUIRE n.url IS UNIQUE;
CREATE CONSTRAINT evidence_id IF NOT EXISTS FOR (n:Evidence) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT story_id IF NOT EXISTS FOR (n:Story) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT storyline_id IF NOT EXISTS FOR (n:Storyline) REQUIRE n.id IS UNIQUE;

CREATE INDEX evidence_edge IF NOT EXISTS FOR (n:Evidence) ON (n.edge_id);
CREATE INDEX event_time IF NOT EXISTS FOR (n:Event) ON (n.occurred_at);
CREATE INDEX event_sched IF NOT EXISTS FOR (n:Event) ON (n.scheduled_at);
CREATE INDEX causes_temporal IF NOT EXISTS FOR ()-[r:CAUSES]-() ON (r.created_at, r.expired_at, r.valid_at, r.invalid_at);
CREATE INDEX causes_status IF NOT EXISTS FOR ()-[r:CAUSES]-() ON (r.status, r.confidence);

CREATE FULLTEXT INDEX entity_text IF NOT EXISTS FOR (n:Entity) ON EACH [n.name, n.aliases];
CREATE VECTOR INDEX entity_embedding IF NOT EXISTS FOR (n:Entity) ON (n.embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } };
CREATE VECTOR INDEX evidence_embedding IF NOT EXISTS FOR (n:Evidence) ON (n.embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } };
CREATE VECTOR INDEX storyline_embedding IF NOT EXISTS FOR (n:Storyline) ON (n.embedding)
  OPTIONS { indexConfig: { `vector.dimensions`: 1024, `vector.similarity_function`: 'cosine' } };
```

### 4.4 핵심 Cypher 6개 (`domino_graph/queries/`)

**Q1 `asof` 조건 — 모든 읽기 쿼리에 문자열로 삽입하는 공통 조각**

```cypher
// ASOF_EDGE(r, $asof)
r.created_at <= $asof AND (r.expired_at IS NULL OR r.expired_at > $asof)
AND r.valid_at <= $asof AND (r.invalid_at IS NULL OR r.invalid_at > $asof)
```

**Q2 트리플 적재 (멱등 MERGE)** — `$t`는 코드 명세서 §3의 `GraphTriple`

```cypher
MERGE (c:Entity {id: $t.cause_id}) MERGE (e:Entity {id: $t.effect_id})
MERGE (c)-[r:CAUSES {direction: $t.direction, relation_kind: $t.relation_kind}]->(e)
  ON CREATE SET r.id = $t.edge_id, r.mechanism = $t.mechanism, r.polarity = $t.polarity,
    r.causal_type = $t.causal_type, r.horizon = $t.horizon, r.status = 'candidate',
    r.confidence = 0.0, r.impact = $t.impact, r.evidence_count = 0, r.lag_days = $t.lag_days,
    r.valid_at = $t.valid_at, r.created_at = datetime(), r.invalid_at = null, r.expired_at = null
WITH r
MERGE (s:Source {url: $t.source_url})
  ON CREATE SET s.id = $t.source_id, s.publisher = $t.publisher, s.grade = $t.grade,
    s.published_at = $t.published_at, s.doc_id = $t.doc_id, s.lang = $t.lang, s.country = $t.country,
    s.state_controlled = $t.state_controlled, s.wire_origin = $t.wire_origin
MERGE (st:Story {id: $t.story_id}) MERGE (s)-[:PART_OF]->(st)
MERGE (ev:Evidence {id: $t.evidence_id})
  ON CREATE SET ev.edge_id = r.id, ev.quote = $t.quote, ev.char_start = $t.char_start, ev.char_end = $t.char_end,
    ev.chunk_id = $t.chunk_id, ev.snapshot_sha = $t.snapshot_sha, ev.llm_confidence = $t.llm_confidence,
    ev.claim_type = $t.claim_type, ev.extracted_at = datetime(), ev.embedding = $t.evidence_embedding
MERGE (ev)-[:FROM]->(s)
RETURN r.id AS edge_id
```

**Q3 승격 — 독립 출처 수 재계산 (전재 제외, 통신사 1개로 집계)**

```cypher
MATCH ()-[r:CAUSES {id: $edge_id}]->()
MATCH (ev:Evidence {edge_id: r.id})-[:FROM]->(s:Source)
WHERE ev.retracted IS NULL OR ev.retracted = false
WITH r, count(DISTINCT coalesce(s.wire_origin, s.publisher)) AS n_pub
SET r.evidence_count = n_pub,
    r.status = CASE WHEN n_pub >= $min_pub AND r.relation_kind = 'CAUSAL' THEN 'confirmed' ELSE r.status END
RETURN r.status, n_pub
```

**Q4 seed → k-hop 경로 (도구 `graph.trace_chain`)**

```cypher
MATCH (seed:Entity) WHERE seed.id IN $seed_ids
MATCH p = (seed)-[rs:CAUSES*1..3]->(t:Entity)
WHERE ALL(r IN rs WHERE r.status = 'confirmed' AND r.confidence >= $min_conf AND ASOF_EDGE(r, $asof)
      AND ($horizons IS NULL OR r.horizon IN $horizons))
WITH p, seed, t, rs,
     reduce(acc = 1.0, r IN rs | acc * r.confidence * r.impact) * (0.85 ^ (size(rs) - 1)) AS path_impact
RETURN seed.id AS seed_id, t.id AS target_id, [n IN nodes(p) | n.id] AS node_ids,
       [r IN rs | {id: r.id, mechanism: r.mechanism, confidence: r.confidence, impact: r.impact,
                   horizon: r.horizon, evidence_count: r.evidence_count, stat_support: r.stat_support}] AS edges,
       size(rs) AS hops, path_impact
ORDER BY path_impact DESC LIMIT $limit
```

**Q5 방향 모순 탐지 (야간)**

```cypher
MATCH (a:Entity)-[r1:CAUSES]->(b:Entity), (a)-[r2:CAUSES]->(b)
WHERE r1.id < r2.id AND r1.direction <> r2.direction
  AND r1.status = 'confirmed' AND r2.status = 'confirmed'
  AND ASOF_EDGE(r1, $asof) AND ASOF_EDGE(r2, $asof)
RETURN a.id, b.id, r1.id, r2.id, r1.direction, r2.direction
```

**Q6 그래프 diff (도구 `graph.diff`)**

```cypher
MATCH (a:Entity)-[r:CAUSES]->(b:Entity)
WHERE (r.created_at >= $since AND r.status = 'confirmed')
   OR (r.invalid_at IS NOT NULL AND r.invalid_at >= $since)
RETURN a.id, a.name, b.id, b.name, r.id, r.status, r.confidence, r.created_at, r.invalid_at
ORDER BY r.created_at DESC LIMIT 200
```

`ASOF_EDGE(r, $asof)`는 Cypher 매크로가 아니라 `domino_graph/asof.py`가 쿼리 문자열을 조립할 때 치환한다. 치환 없이 `asof`가 빠진 읽기 쿼리는 단위 테스트가 거부한다(쿼리 파일에 `ASOF_EDGE` 토큰이 있어야 통과).

## 5. 파이프라인 명세

각 단계는 **멱등 키**를 갖고, 같은 키로 재실행하면 결과가 같다. 실패는 `job_runs`와 `extraction_errors`에 남고, Prefect 재시도는 단계별 정책을 따른다. 예산은 Redis 카운터(`budget:<flow>:<date>`)로 하루 단위 집계하고 초과 시 해당 단계만 멈춘다(리포트는 그래프에 있는 것으로 발행).

| 단계 | 입력 | 출력 | 멱등 키 | 실패 정책 | 일 예산 |
| --- | --- | --- | --- | --- | --- |
| ingest.fetch | `sources` (enabled, cadence 도래) | `raw_documents` (status=new) + minio snapshot | `url_hash` | 소스별 총 3회 시도(재시도 2회, 지수 백오프 2·4초), 연속 5회 실패 시 `enabled=false` + 알림 | — |
| ingest.story | 신규 Document | `stories` 배정, `wire_origin` | `doc_id` | 실패 시 story 미배정으로 두고 다음 배치에서 재시도 | — |
| ingest.chunk | Document (status=new) | `chunks` (status=new) | `(doc_id, idx)` | 결정론적, 재시도 1회 | — |
| extract.classify | Chunk | `chunks.is_causal` | `chunk_id` | 실패 시 `is_causal=true`로 보수적 통과 | $0.3 |
| extract.llm | Chunk (is\_causal) | `extraction_runs` + ValidatedTriple\[\] | `(chunk_id, prompt_version)` | 스키마 실패 1회 재프롬프트, 재실패 시 drop + error 기록 | $2.0 |
| extract.validate | ExtractionResult + chunk text | ValidatedTriple\[\] (grounding·규칙 통과) | — (순수 함수) | drop만, 실패 없음 | — |
| resolve | ValidatedTriple의 cause/effect/targets 문자열 | canonical `entity_id` | `(name, type, region)` | judge 실패 시 신규 Entity 생성 + 검토 큐 | $1.0 |
| graph.upsert | GraphTriple | Neo4j MERGE (Q2) + 승격 (Q3) | `evidence_id` (= det\_id("ev", chunk\_id, quote, cause\_id, effect\_id, direction)) | 트랜잭션, 실패 시 전체 롤백 + 재시도 3회 | — |
| graph.event | Chunk의 `event` 필드 | `Event` 노드 + `TRIGGERED` | `hash(story_id, title)` | 동일 | — |
| storyline.link | 신규 Event | `IN_STORYLINE` | `event_id` | 규칙 → LLM → 검토 큐 | $0.5 |
| signals.flows | `series` (fast axis) | `series_points` | `(series_id, observed_at, available_at)` | 소스별 독립, 실패 소스만 건너뜀 | — |
| signals.trend | 그래프 + series | `trends` | `(subject, window, date)` | 전체 재계산 가능 | — |
| signals.nowcast | series | `nowcasts` | `(series, made_on, h, model)` | — | — |
| signals.signposts | `signposts` (pending, due ≤ today+0) | status hit/miss + `predictions` 해소 | `signpost_id` | — | — |
| signals.rescore | CAUSES 전체 | `confidence` 갱신 | date | 전체 재계산 | — |
| signals.baseline\_q | 어제 리포트 항목 | LLM 베이스라인 답 + overlap | `(run_id, item_idx)` | — | $0.5 |
| report.agent | asof, 그래프, trends, theses | `report_runs.claim_graph` | `report_date` | 25분 상한·$5 상한, 초과 시 부분 결과로 진행 | $5 |
| report.publish | claim\_graph × persona | `report_outputs` + 이메일 | `(run_id, persona)` | 게이트 실패 → `held` + 보류 알림, 재시도 없음 | $0.5 |

### 5.1 단계별 세부 규칙

**ingest.fetch** — `kind`별 크롤러 클래스가 `fetch(since) -> list[RawDoc]`를 구현한다(코드 명세서 §1). HTML은 trafilatura로 본문·발행일 추출, 실패 시 `<title>`만. 본문 < 300자면 `status=skipped`. robots.txt 준수, 도메인별 동시 1·간격 2초. GDELT는 문서가 아니라 **URL 발견기**: `V2Locations`에 요충국이 있고 `V2Themes`에 ECON\_·TAX\_·TRADE·ENERGY 류가 포함된 URL만 큐에 넣고, 그 URL은 `kind=html` 경로로 fetch된다.

**ingest.story** — 64-bit SimHash(본문 첫 2,000자, 3-gram shingles). 최근 72시간 Story 대표 simhash와 Hamming ≤ 6이면 그 Story. 통신사 전재 판정: 본문 첫 300자가 같은 Story 안의 다른 문서와 SimHash ≤ 3이거나 byline이 `wires.yaml` 패턴에 맞으면 `wire_origin`을 설정. `n_publishers`는 `coalesce(wire_origin, publisher)` distinct.

**extract.classify** — Phase 0은 Haiku로 yes/no (입력 chunk만, 출력 1토큰, \~$0.0003/건). "인과·영향·결과·때문·driven by·leads to" 류 키워드가 0개면 LLM 없이 `false`. Phase 1에서 FinCausal fine-tune 분류기로 교체(인터페이스 동일: `classify(chunk) -> bool`).

**extract.llm** — 코드 명세서 §2의 시스템 프롬프트(캐시됨) + chunk. structured output으로 `ExtractionResult` 강제. `temperature=0`. 트리플 > 10이면 스키마가 거부 → 재프롬프트 1회.

**extract.validate** — 순서: (1) grounding: 공백·유니코드 NFKC 정규화 후 `find`, 실패 시 rapidfuzz `partial_ratio ≥ 95`로 재탐색, 그래도 실패면 drop; (2) 자기참조 drop; (3) hedge 사전 매칭 시 CAUSAL→SPECULATIVE 강등; (4) `relation_kind != CAUSAL`이면 `status`는 영구 candidate 표시용 플래그 `is_correlation_only`; (5) `claim_type` 결정: 출처 `state_controlled`이고 quote가 발표·주장 서술이면 `attribution`.

**resolve** — 코드 명세서 §3. 임베딩은 `f"{name} ({type}, {region})"`. cos ≥ 0.92 → 자동 병합, 0.80\~0.92 → Sonnet judge, < 0.80 → 신규. judge `unsure` → 신규 Entity + 검토 큐.

**graph.upsert** — 한 chunk의 트리플 전부를 한 트랜잭션. `evidence_id = det_id("ev", chunk_id, quote, cause_id, effect_id, direction)`(= `"ev_" + sha1("|".join(parts))[:20]`, 코드 명세서 §0.3)로 결정론적 — 재실행해도 Evidence가 중복되지 않고, 한 quote가 여러 엣지(체인 A→B→C)를 받쳐도 엣지마다 Evidence가 따로 생긴다. cause/effect id는 resolve 뒤에 정해지므로 upsert 직전에 계산한다. 승격(Q3)은 upsert 직후 같은 트랜잭션에서.

**storyline.link** — 규칙: `actors` 교집합 ≥ 2 **and** 직전 Event 60일 이내 → 자동. 아니면 Sonnet judge(코드 명세서 §3.4). Phase 0은 **기존 Storyline에 붙이기만**, 신규 생성은 검토 큐로.

**signals.rescore** — v1 §5.1 noisy-OR + 감쇠 + κ. C급 전용 엣지 상한 0.6. `last_confirmed`는 Evidence의 최신 `extracted_at`.

**report.agent** — 코드 명세서 §5. 입력은 `asof = report_date 06:00 KST`. 출력 JSON을 스키마 검증 후 `report_runs.claim_graph`에 저장. 25분 또는 $5 초과 시 SDK가 중단하고 lead가 그때까지의 결과로 JSON을 닫는다(프롬프트에 명시).

### 5.2 재실행 규칙

- `make replay DATE=2026-11-04`는 그날의 `report_flow`를 **같은 asof**로 다시 돌린다. bi-temporal 덕에 그래프 입력은 동일하지만 agent 출력은 비결정적이므로 결과는 `report_runs`에 새 run으로 저장되고 원본은 보존된다(회귀 비교용).
- 추출 프롬프트 버전이 바뀌면 `prompt_version`이 바뀌어 `(chunk_id, prompt_version)` 멱등 키가 새로워진다 → 과거 chunk 재추출은 명시적 백필 명령으로만 (`make backfill FROM= TO=`), 기본은 신규 chunk만.

## 6. 스케줄링과 운영

### 6.1 Prefect flow와 cron (KST)

| flow | 트리거 | 상한 | 동시성 | 하는 일 |
| --- | --- | --- | --- | --- |
| `ingest_flow` | `*/15 * * * *` | 10분 | 1 (중복 실행 금지) | fetch → story → chunk → `extract_flow` 서브플로 |
| `extract_flow` | ingest 끝에 호출 | 20분 | 1 | classify → llm → validate → resolve → graph.upsert/event |
| `storyline_flow` | `5 * * * *` | 5분 | 1 | 신규 Event 연결 |
| `nightly_flow` | `30 4 * * *` | 60분 | 1 | rescore → flows → trend → nowcast → signposts → PPR 캐시 → baseline\_q → 방향 모순 큐 |
| `report_flow` | `0 {report_hour_kst} * * *` (기본 `0 6 * * *`, `settings.report_hour_kst`에서 생성) | 40분 | 1 | agent → slice → editorial → render → gate → deliver |
| `health_flow` | `*/5 * * * *` | 1분 | 1 | 소스 실패·예산·디스크·큐 길이 체크, 알림 |

`nightly_flow`가 06:00까지 안 끝나면 `report_flow`는 **어제 밤 결과로 진행**하고 헤더에 "신호 갱신 지연"을 표시한다. 리포트가 안 나가는 것보다 낫다.

### 6.2 발행 타임라인 (평일·주말 동일)

| 시각 | 단계 | 실패 시 |
| --- | --- | --- |
| 06:00 | agent 시작 (asof 고정) | 25분 초과 → 부분 결과로 진행 |
| 06:25 | claim graph 저장, 스키마 검증 | 실패 → 전체 보류, 알림 |
| 06:26 | 페르소나별 slice → editorial (병렬 2) | editorial 실패 → 그 페르소나만 보류 |
| 06:32 | 렌더 → 게이트 | 실패 → 그 페르소나 보류, 실패 항목 알림 |
| 06:35 | LLM 베이스라인 겹침 측정 (발행과 병렬, 발행을 막지 않음) | — |
| 06:40 | Resend 발송 + minio에 HTML·MD 저장 + 웹 공개 | 발송 실패 → 10분 후 재시도 1회 |
| 07:00 | 배송 webhook 수신 시작 | — |

### 6.3 보류 경로

보류(`held`)는 실패가 아니라 정상 상태다. 보류 시: (1) 베타 구독자에게 "오늘 리포트는 품질 검사를 통과하지 못해 보류됩니다 — 사유: 각주 커버리지 97%"를 보낸다(투명성이 신뢰), (2) 운영자에게 Slack/이메일로 게이트 상세, (3) 운영자가 `make publish-held DATE= PERSONA=`로 수동 승인하거나 그날은 건너뛴다. **보류를 피하려고 게이트를 느슨하게 하지 않는다.**

### 6.4 비용 상한과 관측

| 항목 | 상한 | 초과 시 |
| --- | --- | --- |
| 추출 (일) | $2 | 나머지 chunk는 다음 날 |
| judge·linker (일) | $1 | 신규 Entity 생성 + 검토 큐로 우회 |
| 리포트 run | $5 | SDK `max_budget_usd`, 부분 결과 |
| 월 합계 | $250 | 알림, 수동 확인 전 추출 중단 |

관측: Langfuse에 모든 LLM 호출(prompt\_version·cost·latency 태그), Prefect UI에 flow 상태, Postgres 뷰 `v_daily_health`(문서 수·chunk 수·트리플 수·confirmed 수·검토 큐 길이·소스 실패·게이트 결과·비용)를 `health_flow`가 읽어 임계 초과 시 알림. 대시보드는 Phase 0에선 Langfuse + 이 뷰를 보는 단일 페이지(`/ops`)로 충분.

### 6.5 백업·복구

- Postgres: `pg_dump` 야간 03:00 → minio `backups/pg/<date>.sql.gz`, 30일 보관. Neo4j: `neo4j-admin database dump` 야간 03:10 → minio, 30일. minio 자체는 VPS 디스크 + 주 1회 외부(R2/B2) 동기화.
- 복구 절차는 `docs/runbook.md`: 컨테이너 재생성 → 볼륨 복원 → DDL 재실행(멱등) → `health_flow` 통과 확인. 목표 RTO 2시간, RPO 24시간.

### 6.6 런북 (자주 깨지는 것 순)

| 증상 | 원인 | 조치 |
| --- | --- | --- |
| 특정 소스 `fail_count` 상승 | HTML 구조 변경·차단 | `parser` 교체 또는 `enabled=false`, 대체 소스 활성 |
| grounding 성공률 < 95% | 모델 변경·정규화 버그 | `extraction_runs.raw_json`으로 재현, 회귀 테스트 |
| 검토 큐 > 50 | Resolver 임계 과민 | `resolver_judge_cos` 0.80→0.85 |
| 게이트 각주 실패 반복 | lead가 evidence\_id 없는 claim 생성 | lead 프롬프트의 "근거 없는 문장 금지" 강화, Verifier 출력 확인 |
| 리포트 25분 초과 | subagent 루프 | `maxTurns` 하향, Scout top\_k 축소 |
| 비용 급증 | 캐시 미적중(프롬프트 변경) | prompt caching 헤더 확인 |

## 7. 평가·테스트

### 7.1 테스트 층

| 층 | 대상 | 실행 | 통과 기준 |
| --- | --- | --- | --- |
| 단위 (pytest) | 순수 함수 전부: simhash, chunker, grounding, validator 규칙, hedge 강등, 점수 함수, slice, 게이트, asof 치환, Brier | 매 PR, < 2분 | 100% 통과, 커버리지 ≥ 80% (`domino_core` 제외) |
| 통합 (testcontainers) | Postgres·Neo4j·Redis·minio 띄우고 ingest→extract(LLM mock)→graph 한 바퀴, Q2\~Q6 | 매 PR, < 5분 | 트리플 적재·승격·asof 필터·diff 결과가 fixture와 일치 |
| 골든 (LLM 실호출) | `domino_eval/gold/extract_100.jsonl` (한·영 각 50 chunk, 사람 라벨) | 프롬프트·모델 변경 PR + 주 1회 | 아래 §7.2 |
| 리포트 회귀 | 과거 `claim_graph` 5건을 같은 asof로 재생성해 Critic 루브릭(코드 명세서 §6.5 게이트의 LLM 부분) 점수 비교 | 프롬프트·agent 정의 변경 PR | 평균 점수 하락 ≤ 5% |
| 발행 dry-run | `make report-dry-run DATE=` — 실제 그래프, 발송만 생략 | 매일 수동 가능, Week 9\~12 매일 | 게이트 통과 |

### 7.2 골든 세트 회귀 게이트 (CI 차단)

| 지표 | 임계 |
| --- | --- |
| grounding 성공률 (quote가 원문에서 발견) | ≥ 95% |
| `relation_kind` macro-F1 (4분류) | ≥ 0.80 |
| `direction` 정확도 | ≥ 0.85 |
| Task-1 (chunk에 인과 있음/없음) F1 | ≥ 0.85 |
| 트리플 수 분포 (chunk당 평균) | gold 대비 ±30% |
| `confidence` 분포 | 0.8 이상 비율 ≤ 60% (과신 감지) |

`gold/*.jsonl` 포맷은 코드 명세서 §7.1. 100건은 Week 3에 만들고 Week 8에 300건으로 늘린다. 라벨러 2인 교차, κ < 0.7인 항목은 제외.

### 7.3 LLM 베이스라인 겹침률

발행물의 가치 지표. `nightly_flow`가 어제 리포트의 항목마다 질문을 만들고("2026-11-03 기준, \[항목 ①의 사건\]이 \[항목 ③의 대상\]에 어떤 영향을 주나?"), 웹 검색이 붙은 범용 모델 2종(Phase 0: Claude Sonnet 5.5 + web search, 두 번째는 OpenAI 계열 — 키가 있으면)에 던져 답을 받는다. 겹침은 항목의 ③·④ 문장과 베이스라인 답의 **claim 단위 포함 여부**를 Haiku가 판정(0\~1). 항목 겹침률 ≥ 0.5면 "요약 등급", 리포트 겹침률 = 요약 등급 항목 비율. 매일 `report_outputs.baseline_overlap`에 기록, `/record` 페이지에 월간 공개. **발행 전에는 측정하지 않는다** — 발행을 막는 게 아니라 재는 것.

### 7.4 예측 장부·Brier

- 사전 등록: signpost 전부(Thesis 작성 시), 리포트 항목의 ⑤ "지켜볼 것" 중 수치·시한이 있는 것(Scout·Tracer 출력에서 lead가 `predictions[]`로 내보냄), nowcast.
- 해소: `signals.signposts`가 매일 `due ≤ today` 항목의 `metric_ref`를 조회해 `outcome` 결정. 데이터 결측이면 `due + 7일`까지 대기 후 `outcome = NULL, resolved_at` 기록("판정 불가"로 공개).
- 공개 페이지 `/record`: 전체 목록(사전 등록 시각 포함), 월별 Brier, 보정 곡선(p 구간 5개), 판정 불가 비율. **삭제·수정 불가**, 정정은 새 레코드 + 링크.

### 7.5 CI (GitHub Actions)

`lint` (ruff·mypy strict·import-linter) → `unit` → `integration` → `golden`(`[golden]` 라벨 또는 `packages/domino_extract/**` 변경 시) → `build` (이미지) → `deploy`(main 머지 시 VPS에 `docker compose pull && up -d`, 마이그레이션 자동, DDL 멱등). 롤백은 이전 이미지 태그로 `up -d`.

## 8. Phase 1·2 확장 계약과 구현 순서

### 8.1 Phase 0에 미리 두는 훅 (마이그레이션 없이 확장)

| 확장 (Phase) | Phase 0에 두는 것 | 이유 |
| --- | --- | --- |
| World Model·stock-flow (1) | `series.axis`·`entity_id`·`belt`, `series_points.available_at`, `Entity.type=STRUCTURAL`, `CAUSES.horizon` | 벨트 노드가 Entity·series로 바로 매핑, bi-temporal 되감기 가능 |
| 조사 agent·`domino-data` (2) | MCP 도구 레지스트리를 `domino_agent/tools/registry.py`에 두고 도구 추가가 등록 한 줄 | 도구 추가 시 agent 정의 변경 없음 |
| 컨센서스 4원천 (1) | `predictions.baseline_p`, `llm_baseline_checks` | LLM 베이스라인이 첫 원천, 나머지는 같은 열에 |
| divergence 카드 (1) | master claim graph의 `items[].kind ∈ {event, trend, thesis, divergence, consensus}` — Phase 0은 앞 셋만 생성 | 렌더러·게이트가 유형을 이미 안다 |
| 사례 라이브러리 (1) | `analogs` 테이블은 Phase 1에서 추가하되, Thesis JSON에 `analog_ids[]` 필드 예약 | — |
| Analyst·Critic subagent (2) | `agents.py`의 `AGENTS` dict에 키만 추가 | — |
| 속보 알림 (2) | `Event.salience`와 `expects_primary`를 지금부터 채움 | 충격 템플릿의 입력 |
| API 상품 (B, 보류) | FastAPI 라우터는 이미 리포트·기록·검토를 노출. 인증·과금만 추가 | A가 성립하면 그때 |

### 8.2 구현 순서 (12주, 1인 풀타임 또는 2인 각 25h 기준)

| 주 | 할 일 | 주말 기준 상태 |
| --- | --- | --- |
| 1 | 레포·compose·settings·마이그레이션 0001·Neo4j DDL·CI lint/unit. `sources/` 5개국 15소스. RSS·HTML 크롤러, normalizer, SimHash, chunker | `make up` 후 문서가 Postgres에 쌓인다 |
| 2 | 추출 스키마·프롬프트·grounding·validator·hedge 사전, Langfuse 연결, gold 30건으로 첫 측정 | 트리플이 `extraction_runs`에 쌓인다 |
| 3 | embedder·Resolver 3단계·judge·검토 큐 테이블+API, `GraphRepo` Q1\~Q3, 승격 | Neo4j에 Entity·CAUSES·Evidence·Story |
| 4 | 소스 15개국 45개, GDELT 발견, EDGAR·DART·FRED·EIA 파서, 통신사 전재, Storyline 시드 20 + Linker 규칙, gold 100 | A급 일일 커버 측정 시작 |
| 5 | Trend 2신호, rescore, Q4\~Q6, PPR 캐시, 도구 7종(graph.\*) 구현 + 단위 테스트, `trend.board` | 도구를 CLI로 호출해 답이 나온다 |
| 6 | Agent SDK runner, lead + Scout·Tracer·Verifier 정의·프롬프트, master claim graph 스키마 검증, 첫 dry-run | `claim_graph`가 저장된다 (품질 무관) |
| 7 | Thesis 10개 작성(signpost 포함) + `theses/` 로더, `thesis.list`·signpost 판정, 예측 장부·Brier, `/record` 페이지 골격 | 예측 30건 사전 등록 |
| 8 | personas.yaml·slice·editorial·렌더 템플릿·게이트·Resend 발송, Report view(web), 자기검증 꼬리, gold 300 | dry-run이 게이트를 통과하는 날이 생긴다 |
| 9 | 흐름 빠른 축: PortWatch·FRED·접속 큐·가격·예측 시장 수집기 + series 100 + naive nowcast + `flows.board` 도구, `graph.coverage`·`graph.scheduled`·`graph.followup` | 리포트에 흐름 한 줄·다루지 않은 지역·예정표가 들어간다 |
| 10 | LLM 베이스라인 질문·겹침 측정, `/ops` 뷰, health\_flow, 백업, 런북, **매일 06:00 dry-run 시작** | 연속 발행 카운트 시작 |
| 11 | 품질 반복: lead·Verifier 프롬프트, 게이트 실패 원인 제거, 비용 조정, 리포트 회귀 5건 고정 | 7일 연속 게이트 통과 |
| 12 | 베타 10명 등록·발송·webhook, 보류 경로 리허설(의도적 게이트 실패), 성공 기준 §0.3 점검 | **비공개 베타 발행 시작** |

주차별로 "주말 기준 상태"가 안 되면 다음 주로 넘기지 않고 그 주 범위를 줄인다(실현가능성 탭 §6.1 규칙). 첫 발행이 Week 12를 넘기더라도 **순서는 바꾸지 않는다** — 발행 파이프라인(8·10·12주)이 엔진 고도화보다 먼저다.

## Sources

이 문서는 설계 결정을 코드 수준으로 옮긴 것이라 외부 출처가 거의 없다. 참조한 것:

- Claude Agent SDK subagents 문서(v2 검토 탭 Sources): `AgentDefinition`·`tools`·`model`·`maxTurns`·`max_budget_usd`
- Neo4j vector index·fulltext·GDS personalized PageRank: v2 검토 탭 §2·§5의 Neo4j 문서 링크
- Claude 모델 단가: 사업성 탭 §6
- IMF PortWatch·Interconnection.fyi·Comtrade 한도: 실현가능성 탭 §1
- 라이브러리 버전(trafilatura·simhash·rapidfuzz·testcontainers·TEI)은 구현 시점에 고정하고 `uv.lock`에 둔다 — 여기서는 이름만 적었다.
