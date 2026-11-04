# Domino

매일 06:00 KST에 **근거와 흐름이 붙은 인사이트 리포트**를 발행하는 시스템입니다. 공식 기관·통계·공시 같은 1차 출처를 모아 인과 관계(`원인 → 결과`)를 추출하고, 그 근거 인용문을 각주로 단 리포트를 만듭니다.

지금은 **Phase 0(발행 시작)** 을 12주 계획으로 만들고 있습니다. Phase 0의 목표는 비공개 베타 10명에게 매일 06:00 리포트를 연속 발행하는 것입니다.

> 이 문서는 개요입니다. 무엇을 어떻게 만드는지는 명세가 정합니다. 명세와 코드가 다르면 명세가 맞습니다.

## 현재 상태

| 주차 | 목표("주말 기준 상태") | 상태 |
| --- | --- | --- |
| Week 1 | `make up` 후 문서가 Postgres에 쌓인다 | 완료 — 리뷰 [docs/review/week-1.md](docs/review/week-1.md) |
| Week 2 | 수집이 스케줄로 돌고, 트리플이 `extraction_runs`에 쌓인다 | 진행 중 — [docs/tasks/week-2.md](docs/tasks/week-2.md) |

지금 동작하는 것은 수집 단계입니다. 5개국(미국·한국·일본·대만·독일) 공공기관 RSS 15개를 수집하고, 본문 추출·중복 판정(SimHash)·chunk 분할을 거쳐 Postgres에 저장합니다. 추출(LLM)·그래프·리포트는 이후 주차에 만듭니다.

## 파이프라인 한 바퀴

1. **수집** (15분마다) — 소스 → 원문 snapshot(minio) → 본문 정규화 → Story 배정·통신사 전재 판정 → chunk
2. **추출** — chunk → LLM 구조화 추출 → 원문 인용 확인(grounding) → 검증 → 트리플
3. **그래프** — 엔티티 정규화(Resolver) → Neo4j bi-temporal 그래프, 독립 출처 2개 이상이면 승격
4. **야간 신호** (04:30) — 신뢰도 재계산, 추세, 빠른 시계열, signpost 판정
5. **리포트** (06:00) — 리포트 agent가 claim graph 생성 → 페르소나별 편집 → 게이트(각주 100% 등) → 발송. 게이트를 통과하지 못하면 보류합니다.

자세한 단계·멱등 키·실패 정책은 [tech-phase0.md §5](docs/spec/tech-phase0.md)에 있습니다.

## 레포 구조

```text
packages/
  domino_core/      설정·ID·시간·에러·로깅, 외부 I/O 클라이언트(db·redis·s3·http·neo4j)
  domino_ingest/    소스 레지스트리, 크롤러, normalizer, simhash·story, chunker, store
  domino_extract/   추출 스키마·프롬프트, grounding, validator (Week 2)
  domino_resolve/   엔티티 정규화 (Week 3)
  domino_graph/     Neo4j DDL·쿼리·GraphRepo
  domino_signals/   추세·signpost·nowcast (Week 5~)
  domino_agent/     리포트 agent와 도구 (Week 5~6)
  domino_report/    claim graph → 발행물, 게이트, 발송 (Week 8)
  domino_eval/      gold set 회귀, 예측 장부 (Week 2~)
flows/              Prefect flow (ingest_flow.py, deploy.py)
sources/            국가별 소스 YAML + wires.yaml(통신사 byline)
migrations/         alembic (Postgres)
docs/
  spec/             명세(tech·code)와 결정 기록(decisions.md)
  tasks/            주차별 작업 목록
  review/           주말 리뷰
  prompts/          Claude Code 프롬프트 세트(A~E)
  runbook.md        운영 작업
```

패키지 의존은 한 방향으로만 흐릅니다: `core ← ingest ← extract ← resolve ← graph ← signals ← agent ← report ← eval`. `import-linter`가 `make test`에서 이를 검사합니다.

## 시작하기 (로컬)

필요한 것: Docker Desktop, [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
uv sync
```

```bash
cp .env.example .env
```

`.env`에 값을 채웁니다. `.env.example`에는 키 이름만 있습니다. 비밀 값은 `.env`에만 두고 커밋하지 않습니다.

```bash
make up
```

```bash
make migrate
```

```bash
make seed
```

```bash
make deploy
```

| 명령 | 하는 일 |
| --- | --- |
| `make up` | 인프라 컨테이너를 띄웁니다(postgres·neo4j·redis·minio·embedder·prefect·langfuse). worker 이미지도 다시 빌드합니다. |
| `make down` | 컨테이너를 내립니다. 볼륨(데이터)은 남깁니다. |
| `make migrate` | Postgres 마이그레이션 + Neo4j DDL. 여러 번 실행해도 결과가 같습니다. |
| `make seed` | `sources/`의 소스를 DB에 적재합니다. |
| `make deploy` | Prefect에 수집 스케줄(15분마다)을 등록합니다. 코드를 바꾸면 `make up` 뒤 다시 실행합니다. |
| `make test` | ruff + mypy --strict + import-linter + pytest(unit·integration). integration은 Docker가 필요합니다. |
| `make golden` | gold set 회귀. LLM을 실제로 호출해 비용이 듭니다. 프롬프트·모델을 바꿀 때만 씁니다. |

Prefect UI는 http://127.0.0.1:4200, Langfuse는 http://127.0.0.1:3001 입니다. 운영 작업은 [docs/runbook.md](docs/runbook.md)를 보세요.

### 알아둘 점

- **Apple Silicon:** embedder(TEI)는 arm64 이미지가 없어 amd64 에뮬레이션으로 돕니다. 뜨는 데 시간이 걸립니다.
- **레포 위치:** iCloud와 동기화되는 폴더(예: `~/Desktop`)에 두면 `.venv`·`.git` I/O가 막혀 테스트가 멈춥니다. `~/dev` 같은 동기화 밖 폴더를 쓰세요.
- **스케줄 수집:** 이 Mac과 Docker가 켜져 있는 동안에만 돕니다.

## 개발 방식

이 레포는 Claude Code로 개발하고, 주 단위로 아래 순서를 반복합니다. 프롬프트 전문과 사용 순서는 [docs/prompts/README.md](docs/prompts/README.md)에 있습니다.

| 단계 | 프롬프트 | 모델 | 산출물 |
| --- | --- | --- | --- |
| 월요일 킥오프 | A | Opus | `docs/tasks/week-N.md` |
| 구현 (세션당 태스크 1~3개) | B | Sonnet (태스크에 opus 배정 시 Opus) | 코드·테스트·커밋 |
| 주말 리뷰 (읽기 전용) | C | Opus | `docs/review/week-N.md` |
| 같은 태스크 2회 실패 시 디버그 | D | Opus | 원인·수정 |
| 프롬프트 튜닝 (Week 2·6·11) | E | Opus | 지표 전후 비교 |

규칙(모든 세션이 자동으로 읽음)은 [CLAUDE.md](CLAUDE.md)에 있습니다. 핵심은 다음과 같습니다.

- **명세가 진실의 원천입니다:** [tech-phase0.md](docs/spec/tech-phase0.md)(범위·토폴로지·DDL·파이프라인·운영·12주 순서)와 [code-phase0.md](docs/spec/code-phase0.md)(시그니처·프롬프트·스키마·게이트).
- **명세에 없는 결정**은 가장 보수적인 쪽을 택하고 [decisions.md](docs/spec/decisions.md)에 한 줄로 남깁니다.
- **테스트 먼저**, 태스크 단위 커밋, `make test` 초록이 완료 조건입니다.
- LLM 호출은 `domino_core.clients.llm.call` 하나로만, 외부 I/O는 `domino_core.clients`를 통해서만 합니다.
- Neo4j에서는 삭제하지 않습니다. 변경은 새 레코드와 `expired_at`·`invalid_at`으로 표시합니다.
- 투자 권유 표현은 프롬프트·템플릿·테스트 데이터 어디에도 쓰지 않습니다. 리포트는 정보 제공 목적입니다.

## 스택

Python 3.12 · uv · Pydantic v2 · SQLAlchemy 2 (Core) · alembic · Postgres 16 · Neo4j 5 Community (+GDS) · Redis · minio · Prefect 3 · structlog · pytest · testcontainers · (예정) claude-agent-sdk · FastAPI · Next.js 15
