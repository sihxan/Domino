"""Phase 0 schema: tech-phase0.md section 3 (3.1-3.5), verbatim.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

INGEST = """
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
"""

EXTRACT_REVIEW = """
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
"""

SERIES_SIGNALS = """
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
"""

THESIS_LEDGER = """
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
"""

REPORT_PUBLISH = """
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
"""


def upgrade() -> None:
    conn = op.get_bind()
    for block in (INGEST, EXTRACT_REVIEW, SERIES_SIGNALS, THESIS_LEDGER, REPORT_PUBLISH):
        conn.exec_driver_sql(block)


def downgrade() -> None:
    conn = op.get_bind()
    conn.exec_driver_sql("DROP TABLE llm_baseline_checks")
    conn.exec_driver_sql("DROP TABLE job_runs")
    conn.exec_driver_sql("DROP TABLE deliveries")
    conn.exec_driver_sql("DROP TABLE subscribers")
    conn.exec_driver_sql("DROP TABLE report_outputs")
    conn.exec_driver_sql("DROP TABLE report_runs")
    conn.exec_driver_sql("DROP TABLE predictions")
    conn.exec_driver_sql("DROP TABLE signposts")
    conn.exec_driver_sql("DROP TABLE theses")
    conn.exec_driver_sql("DROP TABLE nowcasts")
    conn.exec_driver_sql("DROP TABLE trends")
    conn.exec_driver_sql("DROP TABLE series_points")
    conn.exec_driver_sql("DROP TABLE series")
    conn.exec_driver_sql("DROP TABLE review_queue")
    conn.exec_driver_sql("DROP TABLE extraction_errors")
    conn.exec_driver_sql("DROP TABLE extraction_runs")
    conn.exec_driver_sql("DROP TABLE chunks")
    conn.exec_driver_sql("DROP TABLE stories")
    conn.exec_driver_sql("DROP TABLE raw_documents")
    conn.exec_driver_sql("DROP TABLE sources")
    conn.exec_driver_sql("DROP TYPE doc_type")
    conn.exec_driver_sql("DROP TYPE source_grade")
