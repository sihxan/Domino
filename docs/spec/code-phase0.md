# 코드 명세서 — Phase 0

Nov 4, 2026 · @KDST

## 0. 규약과 공통 모듈 (`domino_core`)

### 0.1 코딩 규칙

- Python 3.12, `from __future__ import annotations`, 모든 공개 함수에 타입 힌트, `mypy --strict`. 예외는 `domino_core.errors`의 계층만 던진다. 로그는 `structlog` JSON, 모든 로그에 `run_id`·`flow`·`key` 바인딩.
- 외부 I/O는 `domino_core.clients`를 통해서만. 비즈니스 패키지는 `httpx`·`neo4j`·`anthropic`을 직접 import하지 않는다(테스트에서 클라이언트만 mock).
- 순수 함수 우선: 점수·검증·slice·게이트는 입력 → 출력만, DB 접근 없음.
- LLM 호출은 전부 `domino_core.clients.llm.call(...)` 하나로 — 프롬프트 버전·비용·Langfuse trace·예산 카운터가 여기서 처리된다.

### 0.2 `domino_core.errors`

```python
class DominoError(Exception): ...
class TransientError(DominoError): ...        # 재시도 대상 (네트워크·429·5xx)
class PermanentError(DominoError): ...        # 재시도 금지 (스키마·파싱·규칙 위반)
class BudgetExceeded(PermanentError): ...
@dataclass
class GateFailure: code: str; detail: str  # domino_report.gate가 import (§6.6) — report에 두면 core←report 순환
class GateFailed(PermanentError):
    def __init__(self, failures: list[GateFailure]): ...
```

### 0.3 `domino_core.ids` · `domino_core.timeutil`

```python
def new_id(prefix: Literal["doc","sty","chk","tri","ev","ent","evt","src","sl","th","sp","pred","rpt","run","rq","tr"]) -> str  # f"{prefix}_{ulid()}"
def det_id(prefix: str, *parts: str) -> str   # f"{prefix}_{sha1('|'.join(parts))[:20]}" — 결정론적 (evidence, event)

KST = ZoneInfo("Asia/Seoul")
def now_utc() -> datetime
def report_date(now: datetime | None = None) -> date          # KST 기준 오늘
def asof_for(report_date: date) -> datetime                   # settings.report_hour_kst(기본 6)시 KST → UTC — report_flow cron도 같은 값에서 생성
def to_kst(dt: datetime) -> datetime
```

### 0.4 `domino_core.clients.llm`

```python
@dataclass
class LLMResult(Generic[T]):
    parsed: T; raw: str; input_tokens: int; output_tokens: int; cost_usd: float; cache_read: int

def call(
    *, task: str,                       # "extract" | "classify" | "judge" | "linker" | "editorial" | "baseline" | "overlap"
    model: str, system: str, user: str,
    schema: type[T] | None = None,     # Pydantic → structured output
    temperature: float = 0.0, max_tokens: int = 2048,
    budget_key: str | None = None,     # "budget:extract:2026-11-04"
    cache_system: bool = True,         # prompt caching
    trace: dict | None = None,         # langfuse metadata (prompt_version 등)
) -> LLMResult[T]:
    """429/5xx → TransientError, 스키마 불일치 → PermanentError, 예산 초과 → BudgetExceeded"""
```

비용은 모델별 단가표(`pricing.yaml`, 설정)로 계산해 Redis `INCRBYFLOAT budget_key`. `cost_usd`는 `extraction_runs`·`report_runs`에 기록.

### 0.5 `domino_core.clients.embed`

```python
def embed(texts: list[str]) -> list[list[float]]   # TEI /embed, 배치 32, 1024-dim, L2 정규화, 실패 시 TransientError
```

### 0.6 공통 Pydantic 모델 (`domino_core.models`)

```python
class SourceSpec(BaseModel):
    id: str; country: str; region: Literal["NA","EU","CN","EA","ME","EM"]; belts: list[str] = []
    publisher: str; grade: Literal["A","B","C"]; state_controlled: bool = False; wire: str | None = None
    kind: Literal["rss","html","edgar","dart","fred","eia","portwatch","interconnection","predmarket","gdelt"]
    url: str; cadence_min: int = 15; parser: str; lang: str; enabled: bool = True

class RawDoc(BaseModel):
    url: str; title: str | None; published_at: datetime | None; html: str; fetched_at: datetime; source_id: str

class Document(BaseModel):
    id: str; source_id: str; url: str; url_hash: str; title: str | None; published_at: datetime | None
    lang: str; doc_type: str; text: str; text_sha: str; simhash: int; snapshot_key: str; snapshot_sha: str
    story_id: str | None = None; wire_origin: str | None = None

class Chunk(BaseModel):
    id: str; doc_id: str; idx: int; char_start: int; char_end: int; text: str; n_tokens: int
```

추출·그래프·리포트 모델은 각 패키지 §에서 정의하고 `domino_core`는 모르게 둔다(의존 방향).

## 1. `domino_ingest`

### 1.1 소스 레지스트리 (`sources/<cc>.yaml`)

```yaml
# sources/us.yaml
- id: src_us_fed_press
  country: US
  region: NA
  belts: [power, semis]
  publisher: Federal Reserve
  grade: A
  kind: rss
  url: https://www.federalreserve.gov/feeds/press_all.xml
  cadence_min: 30
  parser: RssCrawler
  lang: en
- id: src_us_edgar_8k
  country: US
  region: NA
  belts: [power, semis]
  publisher: SEC EDGAR
  grade: A
  kind: edgar
  url: https://efts.sec.gov/LATEST/search-index?q=%22lead%20time%22%20OR%20%22supply%20constraint%22&forms=8-K,10-Q
  cadence_min: 60
  parser: EdgarFullTextCrawler
  lang: en
```

`wires.yaml`은 통신사 이름과 byline 정규식 목록(`reuters: ["^\\(Reuters\\)", "Reporting by .* Editing by"]`, `yonhap: ["\\(서울=연합뉴스\\)"]` …). 로더 `load_sources(dir) -> list[SourceSpec]`는 YAML을 검증하고 `sources` 테이블에 upsert(id 기준).

### 1.2 크롤러 인터페이스

```python
class Crawler(Protocol):
    def fetch(self, spec: SourceSpec, since: datetime) -> list[RawDoc]: ...

class RssCrawler:        # feedparser → 각 entry.link를 fetch_html로 본문 수집
class HtmlListCrawler:   # 목록 페이지 CSS selector(spec.url#selector) → 링크 → fetch_html
class EdgarFullTextCrawler:   # efts 검색 API JSON → 각 filing URL → 본문 (Archives) 
class DartCrawler:       # OpenDART list.json(bgn_de=since) → document.xml → 텍스트
class FredReleaseCrawler:     # 보도자료 RSS만 (시계열은 signals.flows가)
class GdeltDiscoveryCrawler:  # 15분 GKG CSV → 필터 → RawDoc(url만, html="") → 이후 fetch_html

def fetch_html(url: str, *, timeout: float = 20) -> tuple[str, dict]:
    """robots.txt 확인, 도메인별 세마포어(1), 2초 간격, UA 고정. 반환 (html, headers). 4xx→PermanentError, 5xx/timeout→TransientError"""
```

### 1.3 normalizer

```python
def normalize(raw: RawDoc, spec: SourceSpec) -> Document:
    """
    1) doc_id = new_id("doc"); snapshot_key = f"raw/{yyyy}/{mm}/{doc_id}.html", snapshot_sha = sha256(html) → S3 put (불변, 덮어쓰기 금지).
       snapshot 저장 경로는 여기 하나뿐 — skipped 문서도 이 단계를 먼저 거친다
    2) text, published_at = trafilatura.extract(html, include_comments=False, favor_precision=True) + metadata
    3) text가 300자 미만이면 PermanentError("too_short")  → status=skipped (예외가 doc_id·snapshot_key·snapshot_sha를 싣고 insert_skipped가 그대로 쓴다)
    4) lang = spec.lang 또는 langdetect (혼합 문서는 spec 우선)
    5) doc_type: edgar/dart→FILING; kind ∈ {rss, html} ∧ spec.grade == "A" ∧ spec.wire is None → RELEASE; 그 외 NEWS
    6) simhash = simhash64(shingles(text[:2000], n=3))
    """
```

### 1.4 story dedupe · 전재 판정

```python
def simhash64(tokens: Iterable[str]) -> int          # 표준 Charikar, 64-bit, hash=blake2b 8바이트
def hamming(a: int, b: int) -> int

def assign_story(doc: Document, recent: list[StoryHead], threshold: int = 6) -> str:
    """recent = 최근 72h Story의 (id, simhash). 최소 hamming ≤ threshold면 그 story_id, 아니면 new_id('sty')"""

def detect_wire(doc: Document, same_story_docs: list[Document], wires: WireRules) -> str | None:
    """
    1) byline/첫 300자에 wires 정규식 매칭 → 그 통신사
    2) 같은 story의 다른 문서와 text[:300] simhash hamming ≤ 3 → 먼저 수집된 문서의 publisher가 통신사면 그 이름, 아니면 그 publisher를 wire_origin으로
    3) 없으면 None
    """
```

`n_publishers`는 SQL로: `count(distinct coalesce(wire_origin, publisher))`. `stories.title`은 최고 등급 문서의 제목, 그 문서에 제목이 없으면 본문 앞 120자(`raw_documents.title`은 nullable 그대로).

### 1.5 chunker

```python
def chunk(doc: Document, *, max_tokens: int = 512, overlap: int = 64) -> list[Chunk]:
    """
    문단(\n\n) 경계 우선 → 문장 경계(kss/한국어, pysbd/영어) → 토큰 상한. tiktoken cl100k로 토큰 수 근사.
    char_start/char_end는 원문 text 기준 (grounding offset이 문서 좌표계가 되도록).
    """
```

### 1.6 ingest flow 의사코드

```python
@flow(name="ingest", timeout_seconds=600)
def ingest_flow(now: datetime | None = None):
    for spec, state in due_sources(now):               # cadence 도래(now - last_ok_at ≥ cadence_min - 1분) + enabled; state = SourceState(last_ok_at, fail_count)
        try:
            raws = with_retry(lambda: CRAWLERS[spec.parser]().fetch(spec, since=state.last_ok_at or now - 24h))
                                                         # TransientError면 지수 백오프(2·4초)로 총 3회 시도, PermanentError는 즉시 실패
        except DominoError: mark_fail(spec); continue    # Transient·Permanent 모두 그 소스만 건너뜀, fail_count += 1 (연속 5회면 enabled=false)
        for raw in raws:
            if exists(url_hash(raw.url)): continue       # 멱등
            try: doc = normalize(raw, spec)
            except PermanentError as e: insert_skipped(raw, e); continue
            doc.story_id = assign_story(doc, recent_story_heads(72h))
            doc.wire_origin = detect_wire(doc, story_docs(doc.story_id), WIRES)
            insert_document(doc); upsert_story(doc)
            for c in chunk(doc): insert_chunk(c)
            set_status(doc.id, "chunked")
        mark_ok(spec)
    extract_flow()                                       # 서브플로
```

## 2. `domino_extract`

### 2.1 스키마 최종판 (`schema.py`, `PROMPT_VERSION = "ext-1.0"`)

```python
EntityType = Literal["INDICATOR","ASSET","INDUSTRY","TECHNOLOGY","POLICY","COMPANY","COMMODITY","INFRA","STRUCTURAL"]
Direction  = Literal["UP","DOWN","VOLATILITY","NEUTRAL"]
RelKind    = Literal["CAUSAL","CORRELATION","SPECULATIVE","CONDITIONAL"]
CausalType = Literal["DIRECT","ENABLING","BOTTLENECK","SUBSTITUTION"]
Horizon    = Literal["ultra_short","short","long"]
ClaimType  = Literal["fact","attribution"]

class EntityRef(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="원문 표현 그대로. 번역·정규화 금지")
    type: EntityType
    direction: Direction
    region: str = Field(default="GL", description="ISO-2 국가 또는 GL(글로벌). 원문이 특정 국가 지표면 그 국가")

class CausalTriple(BaseModel):
    cause: EntityRef
    effect: EntityRef
    mechanism: str = Field(max_length=200)
    targets: list[EntityRef] = Field(default_factory=list, max_length=3)
    relation_kind: RelKind
    causal_type: CausalType
    polarity: Literal["POSITIVE","NEGATIVE"]
    horizon: Horizon = Field(description="원문 시제·표현으로: 즉각/수주=ultra_short, 수개월/분기=short, 수년/구조=long")
    lag_days: int | None = None
    impact: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1, description="원문의 단정 강도")
    claim_type: ClaimType = Field(description="fact=결과·사실 서술, attribution=누가 주장·발표했다는 서술")
    chain_position: int | None = None
    valid_at_hint: str | None = Field(default=None, description="원문에 인과 성립 시점이 있으면 ISO date, 없으면 null")
    quote: str = Field(max_length=300)

class EventHint(BaseModel):
    title: str = Field(max_length=120)
    occurred_at: str | None = None            # ISO date
    category: Literal["MACRO","TECH","POLICY","CORPORATE","GEO"]
    country: str = "GL"
    actors: list[str] = Field(default_factory=list, max_length=4)   # 원문 표현
    action_type: str | None = None            # CAMEO 대분류 명 (MAKE_STATEMENT, CONSULT, ... , FIGHT)
    scheduled_at: str | None = None           # 예정 사건이면 ISO date

class ExtractionResult(BaseModel):
    event: EventHint | None
    triples: list[CausalTriple] = Field(max_length=10)
    no_causal_content: bool

    @model_validator(mode="after")
    def _consistency(self):
        if self.no_causal_content and self.triples: raise ValueError("no_causal_content with triples")
        return self
```

### 2.2 시스템 프롬프트 전문 (`prompts/extract_system.md`, 캐시 대상)

```markdown
You are a causal-relation extraction engine for macroeconomic, geopolitical and technology text.
Your output is consumed by a program. Output ONLY a JSON object valid against the provided schema. No prose.

## Task
Extract every statement in the input of the form [cause] --(mechanism)--> [effect] --> [affected targets]
that the TEXT ITSELF asserts or reports. Never add links from your own knowledge.
If the text contains no causal statement: {"event": <event or null>, "triples": [], "no_causal_content": true}.

## Hard rules
1. `quote` MUST be a verbatim substring of the input: same characters, same spacing. It is checked by string search.
2. `cause.name` / `effect.name` are copied as written. No translation, no expansion, no canonicalization.
3. `relation_kind`
   - CAUSAL: the text asserts A causes / leads to / drives / forces B.
   - CORRELATION: A and B move together / are associated; no mechanism asserted.
   - SPECULATIVE: "could", "may", "is expected to", analyst forecasts, 전망, 가능성, 우려.
   - CONDITIONAL: "if A then B".
   Never upgrade CORRELATION or SPECULATIVE to CAUSAL.
4. `causal_type`: DIRECT (A changes B) · ENABLING (A makes B possible/larger) · BOTTLENECK (demand for B exceeds supply;
   use when the text mentions shortage, lead time, capacity limit, sole supplier, waiting list) · SUBSTITUTION (A replaces B).
5. `horizon`: ultra_short = days/weeks or "immediately"; short = months/quarters; long = years or structural ("by 2030", "향후 10년").
   If the text gives no timing, infer from the pair: INDICATOR→ASSET short; STRUCTURAL→anything long; otherwise short.
6. `claim_type`: attribution when the sentence reports what a government, company or person SAID/ANNOUNCED/CLAIMED;
   fact when it reports what HAPPENED or IS.
7. `confidence` reflects the text's certainty, not yours: will/은 0.9, likely/전망 0.6, could/가능성 0.4, some argue 0.3.
8. `impact`: 0.1–0.3 one company/product; 0.4–0.6 one industry or asset class; 0.7–1.0 multiple industries or the macro economy.
9. `targets`: only entities the text names as affected. Empty is fine.
10. `chain_position`: only when one passage narrates A→B→C explicitly; number the links 1,2,3. Otherwise null.
11. `event`: fill when the chunk reports a specific dated occurrence (announcement, decision, incident, data release).
    `actors` are the named parties as written. `scheduled_at` only for a future dated event.
12. At most 10 triples. Prefer fewer well-grounded triples. Korean and English inputs are both valid; keep `name` and `quote`
    in the input language.

## Examples
### Example 1 (English, explicit chain)
Input: "The rapid scaling of frontier AI models has pushed U.S. data-center power demand up sharply, and utilities say grid connections now take four to five years. That bottleneck is driving orders for large transformers and renewed interest in small modular reactors, with Siemens Energy and NuScale among the beneficiaries."
Output:
{"event": null, "no_causal_content": false, "triples": [
 {"cause":{"name":"rapid scaling of frontier AI models","type":"TECHNOLOGY","direction":"UP","region":"GL"},
  "effect":{"name":"U.S. data-center power demand","type":"INDICATOR","direction":"UP","region":"US"},
  "mechanism":"Larger models require more compute, raising data-center electricity consumption","targets":[],
  "relation_kind":"CAUSAL","causal_type":"ENABLING","polarity":"POSITIVE","horizon":"short","lag_days":null,
  "impact":0.7,"confidence":0.9,"claim_type":"fact","chain_position":1,"valid_at_hint":null,
  "quote":"The rapid scaling of frontier AI models has pushed U.S. data-center power demand up sharply"},
 {"cause":{"name":"U.S. data-center power demand","type":"INDICATOR","direction":"UP","region":"US"},
  "effect":{"name":"grid connections","type":"INFRA","direction":"DOWN","region":"US"},
  "mechanism":"Grid capacity cannot absorb new load, so connection lead times stretch to four to five years","targets":[],
  "relation_kind":"CAUSAL","causal_type":"BOTTLENECK","polarity":"NEGATIVE","horizon":"long","lag_days":null,
  "impact":0.6,"confidence":0.8,"claim_type":"attribution","chain_position":2,"valid_at_hint":null,
  "quote":"utilities say grid connections now take four to five years"},
 {"cause":{"name":"grid connections","type":"INFRA","direction":"DOWN","region":"US"},
  "effect":{"name":"orders for large transformers","type":"INDUSTRY","direction":"UP","region":"US"},
  "mechanism":"Connection bottleneck forces grid build-out that requires transformer equipment",
  "targets":[{"name":"Siemens Energy","type":"COMPANY","direction":"UP","region":"DE"}],
  "relation_kind":"CAUSAL","causal_type":"BOTTLENECK","polarity":"POSITIVE","horizon":"short","lag_days":null,
  "impact":0.5,"confidence":0.8,"claim_type":"fact","chain_position":3,"valid_at_hint":null,
  "quote":"That bottleneck is driving orders for large transformers"}]}

### Example 2 (Korean, speculative + correlation — must NOT be CAUSAL)
Input: "미 10년물 국채 금리가 4.5%를 넘어서면서 원·달러 환율도 동반 상승했다. 일부 애널리스트는 금리가 이 수준을 유지할 경우 국내 성장주 밸류에이션에 부담이 될 수 있다고 전망했다."
Output:
{"event":{"title":"미 10년물 국채 금리 4.5% 상회","occurred_at":null,"category":"MACRO","country":"US","actors":[],"action_type":null,"scheduled_at":null},
 "no_causal_content": false, "triples": [
 {"cause":{"name":"미 10년물 국채 금리","type":"INDICATOR","direction":"UP","region":"US"},
  "effect":{"name":"원·달러 환율","type":"INDICATOR","direction":"UP","region":"KR"},
  "mechanism":"동반 상승했다고만 서술, 메커니즘 미제시","targets":[],
  "relation_kind":"CORRELATION","causal_type":"DIRECT","polarity":"POSITIVE","horizon":"ultra_short","lag_days":null,
  "impact":0.6,"confidence":0.5,"claim_type":"fact","chain_position":null,"valid_at_hint":null,
  "quote":"미 10년물 국채 금리가 4.5%를 넘어서면서 원·달러 환율도 동반 상승했다"},
 {"cause":{"name":"미 10년물 국채 금리","type":"INDICATOR","direction":"UP","region":"US"},
  "effect":{"name":"국내 성장주 밸류에이션","type":"ASSET","direction":"DOWN","region":"KR"},
  "mechanism":"할인율 상승이 미래 현금흐름 가치를 낮춤","targets":[],
  "relation_kind":"SPECULATIVE","causal_type":"DIRECT","polarity":"NEGATIVE","horizon":"short","lag_days":null,
  "impact":0.5,"confidence":0.4,"claim_type":"attribution","chain_position":null,"valid_at_hint":null,
  "quote":"금리가 이 수준을 유지할 경우 국내 성장주 밸류에이션에 부담이 될 수 있다고 전망했다"}]}

### Example 3 (no causal content)
Input: "삼성전자는 3분기 매출 79조원, 영업이익 9조원을 기록했다고 공시했다."
Output: {"event":{"title":"삼성전자 3분기 실적 공시","occurred_at":null,"category":"CORPORATE","country":"KR","actors":["삼성전자"],"action_type":"MAKE_STATEMENT","scheduled_at":null},"triples":[],"no_causal_content":true}
```

사용자 메시지: `<document source="{publisher}" grade="{grade}" published_at="{published_at}" lang="{lang}" state_controlled="{bool}">\n{chunk_text}\n</document>`.

### 2.3 사전 분류 (`classifier.py`)

```python
CAUSAL_HINTS = {"en": ["because","due to","driven by","leads to","led to","as a result","impact","effect","shortage","lead time","pushes","forces","caus"],
                "ko": ["때문","영향","따라","결과","이어","초래","부족","리드타임","압박","밀어","끌어"]}

def classify(chunk: Chunk, lang: str) -> bool:
    if not any(h in chunk.text.lower() for h in CAUSAL_HINTS[lang]): return False
    r = llm.call(task="classify", model=settings.model_extract, system=CLASSIFY_SYSTEM, user=chunk.text, max_tokens=2,
                 budget_key=f"budget:classify:{today}")
    return r.raw.strip().upper().startswith("Y")
```

`CLASSIFY_SYSTEM`: "Answer Y if the text asserts or reports that one thing causes, drives, enables, constrains or replaces another (any language). Otherwise N. Answer with one letter."

### 2.4 grounding (`grounding.py`)

```python
def norm(s: str) -> str: return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip()

def ground(quote: str, text: str) -> tuple[int, int] | None:
    """반환은 원문 text(정규화 전) 좌표. 정규화 후 find → 성공 시 역매핑(오프셋 테이블). 실패 시 rapidfuzz.fuzz.partial_ratio_alignment ≥ 95로 재탐색."""
```

오프셋 역매핑: 정규화 과정에서 삭제·치환된 문자 위치를 배열로 기록해 정규화 좌표 → 원문 좌표로 환산. 단위 테스트에 전각·공백 중복·BOM 케이스 포함.

### 2.5 validator (`validator.py`)

```python
class ValidatedTriple(CausalTriple):
    chunk_id: str; char_start: int; char_end: int
    is_correlation_only: bool
    dropped_reason: str | None = None                     # 내부용, 저장 안 함

HEDGE = load_yaml("hedge_lexicon.yaml")   # {en: [could, may, might, expected to, likely, forecast, potential], ko: [전망, 가능성, 우려, 예상, 관측, 듯]}

def validate(res: ExtractionResult, chunk: Chunk, src: SourceSpec) -> tuple[list[ValidatedTriple], list[DropReason]]:
    out, drops = [], []
    for t in res.triples:
        g = ground(t.quote, chunk.text)
        if g is None: drops.append(("grounding", t)); continue
        if norm(t.cause.name) == norm(t.effect.name): drops.append(("self_ref", t)); continue
        rk = t.relation_kind
        if rk == "CAUSAL" and any(h in t.quote.lower() for h in HEDGE[src.lang]): rk = "SPECULATIVE"
        ct = "attribution" if (src.state_controlled and t.claim_type == "fact" and looks_attributive(t.quote)) else t.claim_type
        out.append(ValidatedTriple(**t.model_dump(exclude={"relation_kind","claim_type"}), relation_kind=rk, claim_type=ct,
                   chunk_id=chunk.id, char_start=chunk.char_start+g[0], char_end=chunk.char_start+g[1],
                   is_correlation_only=(rk != "CAUSAL")))   # evidence_id는 resolve 뒤 GraphTriple에서 (§3.3)
    if len(res.triples) and len(drops)/len(res.triples) > 0.3: raise PermanentError("grounding_rate_low")   # chunk 재추출 1회
    return out, drops
```

### 2.6 extract flow 의사코드

```python
@flow(name="extract", timeout_seconds=1200)
def extract_flow():
    for chunk in new_chunks(limit=600):
        key = (chunk.id, PROMPT_VERSION)
        if run_exists(key): continue
        if not classify(chunk, lang): set_status(chunk, "dropped"); continue
        try:
            r = llm.call(task="extract", model=settings.model_extract, system=EXTRACT_SYSTEM, user=wrap(chunk, src),
                         schema=ExtractionResult, budget_key=f"budget:extract:{today}", trace={"prompt_version": PROMPT_VERSION})
            triples, drops = validate(r.parsed, chunk, src)
        except PermanentError as e:                     # 1회 재시도 후 포기
            triples, drops = retry_once_or_drop(chunk, e)
        except BudgetExceeded: break
        record_run(chunk, r, triples, drops)
        resolved = [resolve_triple(t) for t in triples]   # §3
        graph.upsert_chunk(resolved, chunk, src)          # 한 트랜잭션
        if r.parsed.event: graph.upsert_event(r.parsed.event, chunk, resolved)
        set_status(chunk, "extracted")
```

## 3. `domino_resolve` + `domino_graph`

### 3.1 Resolver (`resolver.py`)

```python
@dataclass
class ResolveDecision:
    entity_id: str; created: bool; decided_by: Literal["rule","embedding","llm","new"]; similarity: float | None; queued: bool

def resolve(ref: EntityRef, *, repo: GraphRepo, ctx_quote: str) -> ResolveDecision:
    key = (norm(ref.name).lower(), ref.type, ref.region)
    if hit := repo.find_entity_exact(*key):                       # 1) 정확 일치 (name,type,region) 또는 alias 정확 일치
        return ResolveDecision(hit.id, False, "rule", 1.0, False)
    vec = embed([f"{ref.name} ({ref.type}, {ref.region})"])[0]
    cands = repo.vector_search_entities(vec, k=5, type=ref.type, region_in=[ref.region, "GL"])
    if cands and cands[0].score >= settings.resolver_auto_cos:    # 2) 자동 병합
        repo.add_alias(cands[0].id, ref.name, "embedding", cands[0].score)
        return ResolveDecision(cands[0].id, False, "embedding", cands[0].score, False)
    if cands and cands[0].score >= settings.resolver_judge_cos:   # 3) LLM judge
        verdict = judge(ref, cands[:3], ctx_quote)
        if verdict.decision == "same":
            repo.add_alias(verdict.target_id, ref.name, "llm", cands[0].score)
            return ResolveDecision(verdict.target_id, False, "llm", cands[0].score, False)
        if verdict.decision == "unsure":
            eid = repo.create_entity(ref, vec); enqueue_review("resolver_unsure", ref, cands[:3], ctx_quote, eid)
            return ResolveDecision(eid, True, "new", cands[0].score, True)
    eid = repo.create_entity(ref, vec)                             # 4) 신규
    return ResolveDecision(eid, True, "new", cands[0].score if cands else None, False)
```

### 3.2 judge 프롬프트 (`prompts/resolver_judge.md`, Sonnet 5.5, structured `{decision: same|different|unsure, target_id: str|null, reason: str}`)

```markdown
You decide whether a newly extracted entity mention refers to the SAME real-world entity as one of the candidates.
Same = identical referent (e.g., "HBM" and "고대역폭메모리"; "US 10Y yield" and "미 10년물 국채 금리").
Different = distinct referent even if related (e.g., "금리" in Korea vs "interest rates" in the US; "HBM" vs "HBM3E" — a product generation is a distinct entity).
unsure = you cannot tell from the given context.
Context sentence (where the mention appeared): {ctx_quote}
New mention: name={name}, type={type}, region={region}
Candidates: [{id, name, type, region, aliases, description, recent_evidence[3]}]
Answer with JSON only.
```

### 3.3 `GraphRepo` (`domino_graph/repo.py`) — 메서드 목록과 쿼리 파일

| 메서드 | 쿼리 | 비고 |
| --- | --- | --- |
| \`find\_entity\_exact(name\_norm, type, region) -> Entity | None\` | `q_entity_exact.cypher` (이름 또는 alias) |
| `vector_search_entities(vec, k, type, region_in) -> list[Scored]` | `CALL db.index.vector.queryNodes('entity_embedding', $k, $vec)` + 필터 | `canonical_of IS NULL`만 |
| `create_entity(ref, vec) -> str` | `CREATE (:Entity {...})` | `(name,type,region)` UNIQUE 충돌 시 기존 반환 |
| `add_alias(entity_id, alias, decided_by, sim)` | `MATCH ... SET e.aliases = e.aliases + $alias` + `ALIAS_OF` | — |
| `upsert_chunk(triples: list[GraphTriple], chunk, src)` | Q2 반복 + Q3 (단일 트랜잭션) | `GraphTriple` = ValidatedTriple + cause\_id/effect\_id/target\_ids + source/story 필드 + evidence\_embedding + `evidence_id = det_id("ev", chunk_id, quote, cause_id, effect_id, direction)` (resolve 뒤 계산 — 같은 quote가 받치는 엣지마다 Evidence 1개) |
| `upsert_event(hint, chunk, triples) -> str` | `MERGE (ev:Event {id: det_id('evt', story_id, title_norm)})` + `TRIGGERED` (triples의 cause 엔티티들) | `salience`는 story의 `n_publishers`/10 clip 1.0 |
| `promote(edge_id)` | Q3 | upsert 안에서 호출 |
| `rescore_all(asof)` | `q_rescore.cypher` — Evidence·Source 집계 후 Python `confidence()` 적용, `SET` | 야간 |
| `search_entities(query_vec, query_text, k, asof)` | vector top-k + fulltext top-k → RRF (k=60) | 도구 `graph.search_entities` |
| `trace_chain(seed_ids, asof, horizons, max_hops, min_conf, limit)` | Q4 | 도구 |
| `get_evidence(edge_ids, max_per_edge)` | `MATCH (ev:Evidence) WHERE ev.edge_id IN $ids ... -[:FROM]->(s)` 등급 A 우선·최신 | 도구 |
| `diff(since, until)` | Q6 | 도구 |
| `conflicts(asof)` | Q5 | 야간 → 검토 큐 |
| `events_between(since, until, region=None)` | `MATCH (e:Event) WHERE e.occurred_at ...` | Scout·coverage |
| `scheduled_events(until)` | `scheduled_at` 조회 | 예정표 |
| `storyline_for_event(event_id)` / `storyline_candidates(vec, k)` / `link_event(event_id, sl_id, position, delta, by)` | `IN_STORYLINE` | Linker |

모든 읽기 메서드는 `asof: datetime` 인자를 **필수**로 받고, 쿼리 파일에 `ASOF_EDGE` 토큰이 있어야 로드된다(`asof.py`의 `load_query(name)`가 검사).

### 3.4 Storyline Linker (`domino_graph/storyline.py`)

```python
def link_new_events(repo: GraphRepo, since: datetime) -> None:
    for ev in repo.unlinked_events(since):
        cands = repo.storyline_candidates(embed([ev.title + " " + " ".join(ev.actors)])[0], k=5, status="active")
        rule_hit = next((c for c in cands if len(set(ev.actors) & set(c.key_entities)) >= 2 and c.last_event_at >= ev.occurred_at - 60d), None)
        if rule_hit:
            repo.link_event(ev.id, rule_hit.id, position=rule_hit.n_events+1, delta="confirmed", by="rule"); continue
        v = llm.call(task="linker", model=settings.model_judge, system=LINKER_SYSTEM, user=linker_payload(ev, cands), schema=LinkerVerdict,
                     budget_key=f"budget:judge:{today}")
        if v.parsed.decision == "same":
            repo.link_event(ev.id, v.parsed.storyline_id, position=..., delta=v.parsed.delta, by="llm")
        else:  # new | unsure — Phase 0은 신규 생성 안 함
            enqueue_review("storyline_unsure", ev, cands, v.parsed)
```

`LINKER_SYSTEM`: "Given a new dated event and candidate ongoing storylines (title, summary, last 3 events), decide: same (belongs to one storyline; return its id and delta ∈ escalated|de-escalated|confirmed|reversed with the evidence sentence), new (no candidate fits), or unsure. JSON only."

### 3.5 Thesis 로더 (`theses/*.json` → Postgres)

```json
{"id": "th_ai_power_bottleneck", "title": "AI 컴퓨트 확장은 2030년까지 미국 전력망 접속을 구조적 병목으로 만든다",
 "statement": "...", "layer": "L2", "horizon_end": "2030-12-31", "status": "active", "confidence": 0.6,
 "drivers": ["ent_ai_compute_buildout", "ent_us_grid_queue"], "chain_edge_ids": [], "analog_ids": [],
 "signposts": [
   {"id": "sp_ai_power_1", "statement": "미국 대형 변압기 수입(HS 8504) 12개월 합이 2027-06까지 전년 대비 +15% 이상",
    "metric_kind": "series", "metric_ref": "census.hs8504.imports.12m_yoy", "op": ">=", "threshold": 0.15, "due": "2027-06-30", "falsifies": false},
   {"id": "sp_ai_power_3", "statement": "[반증] 접속 큐 중앙값 대기 기간이 2028년 전에 24개월 미만",
    "metric_kind": "series", "metric_ref": "interconnection.queue.median_wait_months", "op": "<", "threshold": 24, "due": "2028-12-31", "falsifies": true}
 ]}
```

`load_theses()`는 JSON 스키마 검증(`falsifies` ≥ 1, `metric_ref`가 `series.id` 또는 trend subject로 해석 가능) 후 `theses`·`signposts` upsert, 그리고 signpost마다 `predictions` 사전 등록(없을 때만).

## 4. `domino_signals`

### 4.1 점수 함수 (`scoring.py`, 순수 함수, 단위 테스트 100%)

```python
GRADE_W = {"A": 1.0, "B": 0.7, "C": 0.35}
KAPPA   = {"CAUSAL": 1.0, "CONDITIONAL": 0.7, "SPECULATIVE": 0.5, "CORRELATION": 0.0}
TAU     = {"structural": 365.0, "market": 90.0}      # cause/effect 타입에 INFRA·TECHNOLOGY·STRUCTURAL 포함 → structural

def edge_confidence(evidences: list[EvidenceRow], relation_kind: str, tau_kind: str, today: date) -> float:
    """evidences: 독립 출처별 1개로 이미 집계된 행 [(grade, llm_confidence, claim_type, extracted_at)].
    attribution claim은 state_controlled 출처에서 A급, 그 외 fact claim은 C급으로 취급(red-team §4.4)."""
    if not evidences: return 0.0
    prod = 1.0
    for e in evidences:
        g = GRADE_W[e.effective_grade]
        prod *= (1.0 - g * e.llm_confidence)
    base = 1.0 - prod                                        # noisy-OR
    if all(e.effective_grade == "C" for e in evidences): base = min(base, 0.6)
    days = (today - max(e.extracted_at.date() for e in evidences)).days
    decay = math.exp(-days / TAU[tau_kind])
    return round(base * decay * KAPPA[relation_kind], 4)

def path_impact(edges: list[EdgeRow], delta: float = 0.85) -> float:
    p = 1.0
    for e in edges: p *= e.confidence * e.impact
    return p * delta ** (len(edges) - 1)

def noisy_or(ps: Iterable[float]) -> float:
    q = 1.0
    for p in ps: q *= (1 - p)
    return 1 - q

def event_score(magnitude: float, path_impacts_2hop: list[float], novelty: float) -> float:
    return magnitude * sum(path_impacts_2hop) * novelty

def event_score_structural(base: float, trend_transition: bool, signpost_hit: bool, falsifying_hit: bool) -> float:
    return base * (1 + 0.3*trend_transition + 0.5*signpost_hit + 0.8*falsifying_hit)

def brier(p: float, outcome: int) -> float: return (p - outcome) ** 2
```

### 4.2 Trend (`trend.py`) — Phase 0은 2신호

```python
@dataclass
class TrendRow: subject_kind: str; subject_id: str; window: int; z_freq: float|None; z_mom: float|None; trend: float; direction: str; regime: str; persistence: int; transition: str|None; top_events: list

def z_event_freq(daily_counts: np.ndarray, window: int) -> float:
    """daily_counts: 최근 90+window일의 독립출처 가중 Event 수. EWMA(half-life=window/2) 현재값 vs 90일 기준선 평균·표준편차."""
    ewma = pd.Series(daily_counts).ewm(halflife=window/2).mean().iloc[-1]
    base = daily_counts[:-window]
    return float((ewma - base.mean()) / (base.std() + 1e-6))

def z_momentum(series: pd.Series, lookback: int) -> float:
    """series: 일간(또는 월간→일간 ffill). lookback일 수익률을 1년 수익률 분포로 표준화."""
    r = series.pct_change(lookback).dropna()
    if len(r) < 30: return float("nan")
    return float((r.iloc[-1] - r.mean()) / (r.std() + 1e-9))

def compose(zs: dict[str, float]) -> float:
    w = {"freq": .35, "mom": .30, "attn": .20, "narr": .15}
    avail = {k: v for k, v in zs.items() if v is not None and not math.isnan(v)}
    tot = sum(w[k] for k in avail)
    return sum(w[k]*v for k, v in avail.items()) / tot if tot else 0.0

def regime(t: float) -> str: return "extreme" if abs(t) >= 2 else "elevated" if abs(t) >= 1 else "normal"

def compute_trends(repo, pg, computed_for: date) -> list[TrendRow]:
    rows = []
    for subj in subjects(repo):                       # Storyline(active) + Entity(degree ≥ 3)
        for window in (7, 30):
            zf = z_event_freq(event_counts(repo, subj, days=90+window), window)
            zm = z_momentum(series_for(pg, subj), lookback=window) if subj.series_id else None
            t = compose({"freq": zf, "mom": zm})
            prev = pg.prev_trend(subj, window, computed_for - 1d)
            direction = "rising" if t > 0.3 else "falling" if t < -0.3 else "flat"
            persistence = prev.persistence + 1 if prev and prev.direction == direction else 1
            rg = regime(t); transition = f"{prev.regime}->{rg}" if prev and prev.regime != rg else None
            rows.append(TrendRow(subj.kind, subj.id, window, zf, zm, t, direction, rg, persistence, transition, top_events(repo, subj, 7)))
    pg.upsert_trends(rows); return rows
```

리포트 자격: `regime in (elevated, extreme) and persistence >= 5` 또는 `transition is not None`.

### 4.3 세계 흐름 모니터 — 빠른 축 수집기 (`flows/`)

```python
class SeriesCollector(Protocol):
    provider: str
    def collect(self, spec: SeriesSpec, since: date) -> list[Point]   # Point(observed_at, value)

class PortWatchCollector:      # ArcGIS FeatureServer Daily_Chokepoints_Data: where=portid=<id>&outFields=date,n_total,n_tanker,n_cargo, 1000행 페이징
class FredCollector:           # /fred/series/observations?series_id=&observation_start=&realtime_start= (vintage 보존: available_at = realtime_start)
class EiaCollector:            # v2 API, series_id 그대로
class EdgarRpoCollector:       # companyfacts JSON → us-gaap:RevenueRemainingPerformanceObligation (주문잔고 대리), 분기
class InterconnectionCollector:# interconnection.fyi 집계 페이지(무료 뷰)에서 ISO별 queued GW·건수 (HTML, 일간) — 전체 export 아님
class PredictionMarketCollector: # Polymarket/Kalshi 공개 API, 지정 market id의 마감 확률
class PriceCollector:          # yfinance/Polygon 일봉 종가
```

`series`는 `sources/series.yaml`에 100개 선언(`id, axis=fast, provider, provider_key, unit, freq, entity_id, belt, region`). 수집은 `nightly_flow`에서 provider별 병렬, 실패 provider는 건너뛰고 알림. `available_at = now()`가 기본이고 FRED만 vintage를 쓴다.

### 4.4 naive nowcast (`nowcast.py`)

```python
def nowcast(series: pd.Series, freq: str, horizons: tuple[int,...] = (1, 3)) -> list[Forecast]:
    """모델 2개를 항상 함께 기록: naive(마지막 값), seasonal_naive(주기 전 값; D→7, M→12).
    Phase 1에서 ETS/ARIMA 추가. 반환 [(h, model, yhat, lo, hi)] — lo/hi는 과거 h-step 오차의 10/90 분위."""

def resolve_nowcasts(pg, today: date) -> None:
    """made_on + h가 도래한 행에 actual을 채우고 MAE·방향 적중을 집계 뷰 v_nowcast_skill에 반영."""
```

### 4.5 signpost 판정 (`signposts.py`)

```python
def evaluate_signposts(pg, repo, today: date) -> list[SignpostResult]:
    for sp in pg.pending_signposts(due_lte=today):
        val = metric_value(pg, repo, sp.metric_kind, sp.metric_ref, asof=today)   # series 최신 / trend.trend / event_count(storyline, 30d)
        if val is None:
            if today > sp.due + 7d: pg.resolve_signpost(sp.id, status="miss" if not sp.falsifies else "pending", value=None, note="no_data"); pg.resolve_prediction(sp.id, outcome=None)
            continue
        hit = OPS[sp.op](val, sp.threshold)
        pg.resolve_signpost(sp.id, "hit" if hit else "miss", val)
        pg.resolve_prediction(origin="signpost", origin_id=sp.id, outcome=int(hit))
        pg.update_thesis_status(sp.thesis_id)        # 시간 지평 탭 §4.2 규칙: hit≥2→strengthening, falsifying hit→weakening, miss≥2→weakening, 2 falsifying→refuted
```

### 4.6 LLM 베이스라인 질문 생성 (`baseline.py`)

```python
def make_questions(claim_graph: dict) -> list[Question]:
    """item마다 1개: f"{asof_date} 기준으로, {item['block1_fact']} 이것이 {item['block3_target']}에 어떤 영향을 주고 어디까지 파급되나? 근거와 함께 답하라." """

def ask_baseline(q: Question) -> str:
    # Claude Sonnet 5.5 + web_search tool (anthropic SDK), max 1,500 tok; 두 번째 모델은 OPENAI_API_KEY 있을 때만

def overlap(item: dict, answer: str) -> float:
    """Haiku: item의 ③·④ claim을 문장 단위로 나눠 각 claim이 answer에 포함(의미상)되는지 0/1 → 평균."""
```

결과는 `llm_baseline_checks`에, 리포트 단위 겹침률은 `report_outputs.baseline_overlap`에.

## 5. `domino_agent`

### 5.1 도구 계약 (`tools/registry.py`) — Phase 0 11개

도구는 `claude_agent_sdk`의 in-process MCP 서버(`create_sdk_mcp_server` + `@tool`)로 등록한다. 모든 도구는 `asof` 필수, 응답은 토큰 상한을 넘기면 잘라서 `truncated: true` + `next_cursor`. 결과가 비면 `hint` 필드에 다음 행동을 넣는다. 입력·출력은 Pydantic으로 검증하고 실패는 `{"error": {code, message}}`로 돌려준다(예외를 agent에 던지지 않는다).

| 도구 | 입력 | 출력 (상한 tok) | 구현 |
| --- | --- | --- | --- |
| `graph.search_entities` | `query:str, asof:str, types?:list, region?:str, top_k:int≤10` | `[{id,name,type,region,degree,description}]` (600) | `GraphRepo.search_entities` (vector+BM25 RRF) |
| `graph.trace_chain` | \`seed\_ids:list, asof, direction:'downstream' | 'upstream', max\_hops:int≤3, min\_conf:float=0.5, horizons?:list, limit:int≤30\` | `[{seed_id,target_id,node_ids,node_names,edges[{id,mechanism,confidence,impact,horizon,evidence_count,stat_support}],hops,path_impact,order}]` (2,500) |
| `graph.get_evidence` | `edge_ids:list, max_per_edge:int≤3` | `[{edge_id,evidence_id,quote,claim_type,source{publisher,grade,published_at,url,country},snapshot_sha}]` (1,500) | A급·최신 우선 |
| `graph.diff` | `since:str, until:str, seed_ids?:list` | `{new_confirmed[], invalidated[], moved[]}` (1,500) | Q6 |
| `graph.explain_conflict` | `entity_pair:[a,b], asof` | `{edges[2], evidence[2][2]}` | Q5 범위 |
| `graph.storyline` | `event_id:str` | `{storyline{id,title,summary,state}, position, delta, prev_event{id,title,occurred_at}}` 또는 `{storyline:null}` | `storyline_for_event` |
| `graph.coverage` | `date:str` | `[{region, top_event{id,title,country,salience}}]` 6개 지역 | `events_between` 지역별 max salience |
| `graph.scheduled` | `window_days:int≤30, asof` | `[{event_id,title,scheduled_at,country,linked_entities[]}]` | `scheduled_events` |
| `graph.followup` | `report_date:str` | 어제 리포트의 ⑤ 항목 각각 \`{watch, status: happened | not\_yet |
| `trend.board` | `window:int∈{7,30}, regime_min:str='elevated', top_k:int≤20, layer?:str` | `[{subject,kind,trend,regime,direction,persistence_days,transition,z_freq,z_mom,top_events[3],thesis_links[]}]` (2,000) | `trends` 테이블 |
| `thesis.list` | `status?:list, layer?:str` | `[{id,title,status,confidence,next_due_signpost{statement,due},drivers[]}]` | `theses`·`signposts` |

`flows.board`(흐름 빠른 축 요약: 시계열별 최근값·7일 변화·z)는 Week 9에 추가되며 계약은 `trend.board`와 동형.

### 5.2 Agent 정의 (`agents.py`)

```python
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, query, create_sdk_mcp_server

DOMINO_MCP = create_sdk_mcp_server(name="domino", version="0.1.0", tools=ALL_TOOLS)   # §5.1

AGENTS = {
  "event-scout": AgentDefinition(
      description="지정된 지역의 지난 24시간 Event를 점수화해 상위 후보를 돌려준다. Event 선정이 필요할 때 지역마다 1개씩 병렬로 호출.",
      prompt=load("prompts/scout.md"), tools=["mcp__domino__graph_diff","mcp__domino__graph_search_entities","mcp__domino__graph_coverage"],
      model="haiku"),
  "chain-tracer": AgentDefinition(
      description="seed Entity에서 3-hop 인과 경로를 추적하고 각 고리의 근거·모순을 표시한다. 선정된 Event마다 1개.",
      prompt=load("prompts/tracer.md"), tools=["mcp__domino__graph_trace_chain","mcp__domino__graph_get_evidence","mcp__domino__graph_explain_conflict","mcp__domino__graph_storyline","mcp__domino__trend_board","mcp__domino__thesis_list"],
      model="sonnet"),
  "evidence-verifier": AgentDefinition(
      description="초안이 인용한 evidence_id마다 quote가 실재하고 claim과 방향이 맞는지 판정한다. 초안 완성 후 1회.",
      prompt=load("prompts/verifier.md"), tools=["mcp__domino__graph_get_evidence"],
      model="haiku"),
}

OPTIONS = ClaudeAgentOptions(
    system_prompt=load("prompts/lead.md"),
    agents=AGENTS,
    mcp_servers={"domino": DOMINO_MCP},
    allowed_tools=["Agent","mcp__domino__graph_diff","mcp__domino__graph_coverage","mcp__domino__graph_scheduled",
                   "mcp__domino__graph_followup","mcp__domino__trend_board","mcp__domino__thesis_list","Write","Read"],
    model=settings.model_lead, max_turns=40, max_budget_usd=settings.budget_report_run,
    # 환경: CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=1, CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=6
)
```

도구 이름 규약(`mcp__<server>__<tool>`)과 `AgentDefinition`·`ClaudeAgentOptions` 필드는 구현 시 SDK 버전의 문서([subagents](https://code.claude.com/docs/en/agent-sdk/subagents.md))로 재확인한다. `Write`/`Read`는 lead의 `notes.md` 스크래치패드용(작업 디렉토리 격리).

### 5.3 runner (`runner.py`)

```python
async def run_report_agent(report_date: date) -> ClaimGraph:
    asof = asof_for(report_date)
    prompt = render("prompts/lead_task.md", asof=asof.isoformat(), report_date=report_date.isoformat(),
                    regions=REGIONS, max_events=12, output_schema=ClaimGraph.model_json_schema())
    final_text = None
    async for msg in query(prompt=prompt, options=OPTIONS):
        log_message(msg)                                     # Langfuse trace
        if is_result(msg): final_text = msg.result
    cg = ClaimGraph.model_validate_json(extract_json(final_text))   # 실패 시 PermanentError → 보류
    cg = enforce_invariants(cg)                              # 레이어 태깅·evidence 존재·상한 (§6.2)
    return cg
```

타임아웃 25분은 `asyncio.wait_for`. 초과 시 SDK에 interrupt를 보내고 lead가 그때까지의 `notes.md`로 JSON을 닫는다(프롬프트 규칙 9).

### 5.4 Lead 프롬프트 (`prompts/lead.md` — system)

```markdown
당신은 Domino의 리포트 편집장이다. 오늘(asof) 시점에서 세계의 중요한 변화를 근거와 흐름과 함께 정리한 master claim graph(JSON)를 만든다. 당신은 작성자이지 분석가가 아니다: 사실·인과·근거는 전부 도구와 subagent가 가져온 것만 쓴다.

## 절대 규칙
1. 모든 claim은 evidence_ids ≥ 1 이어야 한다. 근거 없는 문장은 kind="hypothesis"로만 쓰고 전체 2개를 넘기지 않는다.
2. 도구 응답에 없는 Entity·Event·엣지를 만들지 않는다. 기억으로 보충하지 않는다.
3. asof는 {asof}이다. 모든 도구 호출에 그대로 전달한다. 바꾸지 않는다.
4. 위임할 때는 objective · 출력 형식 · 쓸 도구 · 경계 · asof를 빠짐없이 쓴다. "관련 내용 조사해" 같은 위임은 금지.
5. Scout는 지역당 1개, 6개를 병렬로. Tracer는 선정 Event당 1개, 최대 8개. Verifier는 초안 완성 후 1회. 총 subagent ≤ 15.
6. 모순(방향 충돌, Verifier ok=false)은 숨기지 않고 item.conflicts에 넣는다.
7. 투자 권유 표현 금지: 매수·매도·추천·확실히·목표가. 기업은 "관련 기업"으로만.
8. subagent 결과를 받을 때마다 notes.md에 "확정 사실 / 열린 질문"을 갱신한다. 최종 JSON은 notes.md만 보고 쓴다.
9. 중단 신호(시간·예산)를 받으면 그때까지의 notes.md로 즉시 JSON을 닫는다. 미완성 항목은 버린다.
10. 출력은 오직 하나의 JSON 객체. 스키마는 작업 메시지에 있다.

## 절차
Plan → (thesis.list, trend.board, graph.diff로 오늘의 맥락 파악) → Scout ×6 병렬 → top-12 Event 선정(event_score, 지역당 ≥1, 카테고리당 ≥2, Trend 전환·signpost hit 가중) → Tracer ×N 병렬 → 초안(notes.md) → Verifier → 수정 → JSON.
```

### 5.5 subagent 프롬프트 요지

**scout.md**: "지역 {region}의 asof 24h 이내 Event를 `graph.diff`·`graph.coverage`로 수집하고 각 Event에 `event_score`(salience × 2-hop path\_impact 합 × novelty)를 매겨 상위 8개를 `[{event_id,title,country,category,score,why(1문장),unconfirmed:bool}]`로 반환. 단일 출처 사건은 unconfirmed=true. 800토큰 이내."

**tracer.md**: "seed {entity\_ids}에서 `graph.trace_chain`(downstream, 3-hop, min\_conf 0.5)로 경로 상위 10개를 받고, 각 경로의 엣지에 `graph.get_evidence`로 근거를 붙이고, `graph.explain_conflict`로 모순을 확인하고, `graph.storyline`으로 흐름 문장 재료를, `trend.board`·`thesis.list`로 경향·구조 연결을 찾는다. 반환: `{paths[], weakest_edge_per_path, conflicts[], storyline, trend_links[], thesis_links[]}` 2,000토큰 이내. 도구가 돌려주지 않은 엣지는 쓰지 않는다."

**verifier.md**: "입력된 `[{claim, evidence_ids}]`마다 `graph.get_evidence`로 quote를 가져와 (a) quote가 존재하고 (b) claim의 방향·대상이 quote와 일치하면 ok=true, 아니면 ok=false와 이유. 쓰기 도구 없음. `[{evidence_id, claim_idx, ok, reason}]`만 반환."

### 5.6 예산·제한

| 항목 | 값 |
| --- | --- |
| lead `max_turns` | 40 |
| subagent `maxTurns` | Scout 8 · Tracer 12 · Verifier 15 |
| 동시 subagent | 6 |
| run 예산 | $5 (`max_budget_usd`) |
| 벽시계 | 25분 (runner) |
| 모델 | lead Sonnet 5.5 (effort high), Tracer Sonnet, Scout·Verifier Haiku |

Week 6\~8 dry-run에서 비용이 $3을 넘으면 Scout top\_k 8→5, Tracer 경로 10→6으로 줄인다.

## 6. `domino_report`

### 6.1 master claim graph 스키마 (`claim_graph.py`)

```python
class Claim(BaseModel):
    text: str = Field(max_length=300)
    evidence_ids: list[str]
    kind: Literal["confirmed","hypothesis"]
    layer: Literal["L0","L1","L2","L3"]           # 코드가 재계산 (6.2)
    block: Literal[1,2,3,4,5,6]

class ChainEdge(BaseModel):
    edge_id: str; from_id: str; to_id: str; from_name: str; to_name: str
    mechanism: str; confidence: float; impact: float; horizon: str; evidence_count: int; stat_support: float | None = None

class Chain(BaseModel):
    edges: list[ChainEdge]; orders: list[int]; weakest_edge_id: str; path_impact: float

class Watch(BaseModel):
    text: str; bucket: Literal["week","quarter","later"]
    metric_ref: str | None = None; op: str | None = None; threshold: float | None = None; due: date | None = None   # 있으면 predictions 사전 등록

class Item(BaseModel):
    idx: int
    kind: Literal["event","trend","thesis"]      # Phase 1: + divergence, consensus
    anchor_id: str                                # event_id | trend_id | thesis_id
    title: str = Field(max_length=120)
    region: str; category: str
    time_span: str                                # "지금 → 2027 H1" (코드가 chain horizon에서 계산)
    block1_fact: list[Claim]                      # ①
    block2_flow: dict                             # ② {storyline:{title,position,delta,prev_event}|None, trend:{...}|None, thesis:{...}|None, analog:None}
    block3_why: Claim                             # ③ (하나)
    block4_chains: list[Chain]                    # ④
    block5_watch: list[Watch]                     # ⑤
    block6_confidence: dict                       # ⑥ {short:float, long:float|None, conflicts:[{edge_ids, note}], counter: Claim|None}
    score: float

class ClaimGraph(BaseModel):
    report_date: date; asof: datetime
    items: list[Item] = Field(max_length=12)
    coverage: list[dict]                          # graph.coverage 결과
    scheduled: list[dict]                         # graph.scheduled 7d
    followup: list[dict]                          # graph.followup (어제 ⑤ 검증)
    graph_diff_summary: dict                      # {new_confirmed, invalidated, moved}
    hypotheses_total: int
```

### 6.2 불변식 강제 (`enforce_invariants`, 코드)

1. `layer`는 lead가 쓴 값을 **무시하고** 재계산: block1·block2 → L0; chain에 INDICATOR 1-hop만 → L1; 2\~3-hop INDUSTRY·ASSET·COMMODITY → L2; COMPANY·bottleneck 관련 → L3.
2. `kind == confirmed`인 Claim은 `evidence_ids` ≥ 1, 각 id가 Neo4j에 존재(배치 조회). 아니면 `hypothesis`로 강등.
3. `hypotheses_total ≤ 2`, 초과분은 score 낮은 item부터 제거.
4. `time_span`은 chain의 최장 `horizon`으로 계산(ultra\_short→"이번 달", short→"이번 분기\~내년", long→연도).
5. Item 수 ≤ 12, 지역당 ≥ 1(가능하면), 동일 Storyline ≤ 2.
6. `block5_watch` 중 `metric_ref`가 있는 항목은 `predictions`에 사전 등록(`origin_kind=report_item`), `p`는 chain의 path\_impact를 0.3\~0.8로 clip.

### 6.3 페르소나 (`personas.yaml`) 와 slice (`slice.py`)

```yaml
world_brief:   { layer: L0, depth: brief,    items: 5, lenses: {regions: all}, include_coverage: true,  include_scheduled: 7,  numbers: none }
industry_chain:{ layer: L2, depth: standard, items: 3, lenses: {sectors: [power, semis]}, include_coverage: true, include_scheduled: 7, numbers: one }
```

```python
def slice(cg: ClaimGraph, persona: Persona) -> SlicedReport:
    items = [i for i in cg.items if lens_match(i, persona.lenses)]
    items = sorted(items, key=lambda i: -i.score)[: persona.items]
    for i in items:
        i.block1_fact = [c for c in i.block1_fact if LAYER_ORD[c.layer] <= LAYER_ORD[persona.layer]]
        if persona.layer == "L0": i.block4_chains = []; i.block3_why = compress(i.block3_why)
        if persona.layer == "L1": i.block4_chains = [trim_hops(ch, 1) for ch in i.block4_chains]
        if persona.depth == "brief": keep_blocks(i, {1, 2, 3, 5}); i.block2_flow = one_line(i.block2_flow)
    return SlicedReport(persona=persona, items=items, coverage=cg.coverage if persona.include_coverage else [],
                        scheduled=cg.scheduled[: persona.include_scheduled], followup=cg.followup)
```

레이어 누출은 구조적으로 불가능: L0 리포트에는 L1 이상 Claim이 **남아 있지 않다**.

### 6.4 editorial (`editorial.py`, Sonnet 5.5 1회, 도구 없음)

입력: `SlicedReport` JSON (≤ 8k tok). 출력 스키마: `{items: [{idx, prose: {b1, b2, b3, b4?, b5, b6?}}], header_line, tail_line}` — **evidence\_id는 입력에 있는 것만 재배열**하고 새 사실·수치 추가 금지(스키마에 수치 필드 없음, 후처리로 입력에 없는 4자리 이상 숫자가 나오면 게이트 실패).

`prompts/editorial.md` 요지: "페르소나 {layer}/{depth} 독자에게 맞는 톤(제품 설계 탭 §2.3)으로 각 항목의 블록을 산문화한다. 블록 순서: ③ 결론 → ① 사실 → ② 흐름 → ④ 체인 → ⑤ 지켜볼 것 → ⑥ 확신도·반론. 각 문장 끝에 근거 각주 `[ev_…]`를 유지한다. ②의 trend/thesis가 null이면 '연결된 경향 없음 — 단발 사건일 수 있음'을 그대로 쓴다. 숫자는 depth=brief면 쓰지 않고 '4명 중 1명' 식으로, standard면 확률 하나만. 금지어: 매수, 매도, 추천, 확실히, 목표가. 헤더 한 줄: '오늘 N건 · 흐름 전환 M · 구조 변화 K'. 꼬리 한 줄: followup 요약."

### 6.5 렌더 (`render/report.md.j2` → markdown → HTML)

```jinja
# Domino {{ persona.title }} · {{ report_date }}  (asof {{ asof_kst }})
{{ header_line }}
{% for it in items %}
### {{ loop.index }}. {{ it.title }}      시간 폭: {{ it.time_span }}
**{{ it.prose.b3 }}**
① {{ it.prose.b1 }}
② {{ it.prose.b2 }}
{% if it.prose.b4 %}④ {{ chain_text(it.block4_chains) }}{% endif %}
⑤ 이번 주: {{ it.watch.week }} · 이번 분기: {{ it.watch.quarter }} · 그 이후: {{ it.watch.later }}
{% if it.prose.b6 %}⑥ {{ it.prose.b6 }}{% endif %}
{% endfor %}
## 다루지 않은 지역
{% for c in coverage %}- {{ c.region }}: {{ c.top_event.title }} ({{ c.top_event.country }}){% endfor %}
## 예정 (7일)
{% for s in scheduled %}- {{ s.scheduled_at }} {{ s.title }}{% endfor %}
## 어제 '지켜볼 것'은
{{ tail_line }}
---
정보 제공 목적이며 투자 권유가 아닙니다. 근거: 각 [ev_…]를 클릭하면 원문 인용과 출처를 볼 수 있습니다. 적중 기록: {{ record_url }}
```

`chain_text(chains)`는 코드가 엣지 속성에서 기계적으로 만든다: `A →(즉시, 0.78, A급 2) B →(수개월, 0.61, B급 3) C`, 최소 confidence 엣지에 `  ← 가장 약한 고리 `. 각주 `[ev_…]`는 HTML에서 `/e/<evidence_id>` 링크.

### 6.6 게이트 (`gate.py`, 순수 함수)

```python
from domino_core.errors import GateFailure   # 정의는 §0.2 (code: str; detail: str)

BANNED = ["매수", "매도", "추천종목", "추천", "확실히", "목표가", "buy", "sell", "price target"]

def gate(rendered_md: str, sliced: SlicedReport, persona: Persona, evidence_ok: dict[str, bool]) -> list[GateFailure]:
    f = []
    sents = sentences(rendered_md, exclude_sections={"다루지 않은 지역","예정","어제"})
    cited = [s for s in sents if EV_RE.search(s)]
    cov = len(cited) / max(1, len(sents))
    if cov < 1.0: f.append(GateFailure("footnote_coverage", f"{cov:.2%}"))
    bad = [i for i in EV_RE.findall(rendered_md) if not evidence_ok.get(i, False)]
    if bad: f.append(GateFailure("evidence_not_verified", ",".join(bad[:5])))
    if any(b in rendered_md for b in BANNED): f.append(GateFailure("banned_phrase", "..."))
    if persona.layer == "L0" and any(c.layer != "L0" for it in sliced.items for c in it.block1_fact): f.append(GateFailure("layer_leak", ""))
    if count_hypotheses(rendered_md) > 2: f.append(GateFailure("too_many_hypotheses", ""))
    if persona.depth == "brief" and NUM_RE.search(body_only(rendered_md)): f.append(GateFailure("numbers_in_brief", ""))
    if len(rendered_md) > persona.max_chars: f.append(GateFailure("too_long", str(len(rendered_md))))
    if block_order_violated(rendered_md): f.append(GateFailure("block_order", ""))
    if dup_entities_across_items(sliced) > 2: f.append(GateFailure("entity_dup", ""))
    return f
```

`evidence_ok`는 Verifier 출력(§5.5)에서 온다. 게이트는 **수정하지 않는다** — 실패 목록만 돌려주고 flow가 보류를 결정한다.

### 6.7 배송 (`deliver.py`)

```python
def deliver(output: ReportOutput, subscribers: list[Subscriber]) -> None:
    for s in subscribers:
        if delivered(output.id, s.id): continue
        html = personalize(output.html, s)          # 'last_read_run_id' 기준 "지난 N일간 바뀐 경향" 한 줄 삽입
        msg_id = resend.send(to=s.email, subject=f"Domino {persona.title} · {date}", html=html, tags={"run": output.run_id})
        record_delivery(output.id, s.id, msg_id)
```

webhook(`/webhooks/resend`)이 `delivered/opened/clicked`를 `deliveries`에 기록. 보류 시 `deliver_hold_notice(subscribers, failures)`가 사유 한 줄을 보낸다.

## 7. `domino_eval` · `apps`

### 7.1 gold set 포맷 (`domino_eval/gold/extract_*.jsonl`)

```json
{"chunk_id": "gold_0001", "lang": "ko", "text": "...", "source": {"publisher": "연합뉴스", "grade": "B", "state_controlled": false},
 "labels": {"has_causal": true,
            "triples": [{"cause": "미 10년물 국채 금리", "effect": "원·달러 환율", "direction_effect": "UP", "relation_kind": "CORRELATION",
                         "causal_type": "DIRECT", "horizon": "ultra_short", "claim_type": "fact", "quote": "..."}]},
 "annotators": ["a","b"], "kappa": 0.82}
```

매칭 규칙: 예측 트리플과 gold 트리플은 `quote` 겹침(문자 Jaccard ≥ 0.6)과 cause/effect 이름 정규화 일치로 짝짓고, 짝지어진 것끼리 `relation_kind`·`direction`·`causal_type` 정확도를 센다. 짝이 없는 예측은 FP, 짝이 없는 gold는 FN.

### 7.2 회귀 테스트 (`regression.py`)

```python
def run_extract_regression(gold_path, model, prompt_version) -> Metrics:
    """gold 전부 → extract (실호출, 캐시된 시스템 프롬프트) → validate → 매칭 → Metrics(grounding_rate, rk_macro_f1, dir_acc, task1_f1, triples_per_chunk, conf_over_08_ratio)"""

def assert_thresholds(m: Metrics) -> None:   # 기술 명세서 §7.2 임계. 실패 시 CI 빨강
```

리포트 회귀: `run_report_regression(run_ids: list[str])` — 저장된 `claim_graph`의 `asof`로 agent를 재실행하고 Critic 루브릭(Sonnet, 6항목 0/1: 근거 없는 문장·투자 권유·순환 논리·시점 혼동·과장·가독성)을 두 결과에 매겨 평균 비교.

### 7.3 LLM 베이스라인 겹침 (`baseline_overlap.py`)

```python
def overlap_score(item_claims: list[str], baseline_answer: str) -> float:
    r = llm.call(task="overlap", model=settings.model_extract, system=OVERLAP_SYSTEM,
                 user=json.dumps({"claims": item_claims, "answer": baseline_answer}), schema=OverlapVerdict)
    return sum(r.parsed.included) / len(item_claims)
```

`OVERLAP_SYSTEM`: "For each claim, answer 1 if the answer text already states or clearly implies the same fact/causal link (same direction, same target), else 0. Paraphrase counts as included. Return {included: \[0|1,...\]}."

### 7.4 예측 장부·Brier (`ledger.py`, `brier.py`)

```python
def register(pred: Prediction) -> None               # INSERT ... ON CONFLICT DO NOTHING (사전 등록은 불변)
def resolve(pred_id: str, outcome: int | None) -> None   # outcome·resolved_at·brier 기록, 재해소 금지
def monthly_brier(month: date) -> dict               # {n, brier, calibration: [(p_bin, n, hit_rate)], unresolved_rate}
```

`/record` 페이지는 `predictions`를 그대로 보여준다(생성 시각·p·due·outcome). 정정은 새 레코드 + `corrects: <id>` 컬럼(Phase 0은 `note`에 기록).

### 7.5 FastAPI (`apps/api`)

| 메서드·경로 | 응답 | 비고 |
| --- | --- | --- |
| `GET /health` | `{ok, pg, neo4j, redis, s3, embedder}` | health\_flow·Caddy |
| `GET /reports/{date}/{persona}` | 렌더된 HTML | 베타는 토큰 링크(`?t=`), 공개는 Phase 2 |
| `GET /reports/{date}/{persona}.md` | markdown |  |
| `GET /evidence/{evidence_id}` | `{quote, char_start, char_end, source{...}, snapshot_url(서명 URL, 1h)}` | 각주 클릭 |
| `GET /record` / `GET /record.json` | 예측 장부·월별 Brier | 공개 |
| `GET /review` / `POST /review/{id}` | 검토 큐 목록·판정 \`{action: merge | separate |
| `POST /webhooks/resend` | 204 | 서명 검증 |
| `GET /ops` | `v_daily_health` + 최근 게이트 결과 | 운영자 |

### 7.6 프론트 최소 화면 (`apps/web`)

| 경로 | 내용 |
| --- | --- |
| `/r/[date]/[persona]` | 리포트 HTML(서버 렌더 그대로), 각주 클릭 시 사이드 패널에 `/evidence` 응답(quote 하이라이트 + 출처 + 원문 snapshot 링크) |
| `/record` | 예측 목록 테이블, 월별 Brier, 보정 막대 5개 |
| `/review` | 검토 큐 카드: 이름·후보 3·근거 3·버튼(merge/separate/skip) |
| `/ops` | 일일 건강 표 |

As-of slider·Chain view(React Flow)는 Phase 2. Phase 0 프론트는 **읽기와 검토**만.

### 7.7 테스트 목록 (최소)

| 파일 | 케이스 |
| --- | --- |
| `test_simhash.py` | 동일 문서 hamming 0, 전재(앞 300자 동일) ≤ 3, 무관 문서 > 20 |
| `test_chunker.py` | 문단 경계·512 상한·overlap·char 좌표 원문 일치 |
| `test_grounding.py` | 정확 일치, 공백 중복, NFKC 전각, 따옴표 변형, 부분 일치 95, 실패 None, 역매핑 좌표 |
| `test_validator.py` | hedge 강등, 자기참조 drop, state\_controlled attribution, grounding 30% 초과 시 PermanentError |
| `test_scoring.py` | noisy-OR 단조성, C급 clip 0.6, 감쇠, κ, path\_impact δ, event\_score\_structural 상한 |
| `test_trend.py` | 합성 가중 재분배, regime 경계, persistence 리셋, transition 문자열 |
| `test_asof.py` | ASOF\_EDGE 치환, 토큰 없는 쿼리 로드 거부 |
| `test_graph_integration.py` | Q2 멱등(2회 실행 Evidence 1개), Q3 승격(통신사 전재 2건 = 1), Q4 asof 필터(미래 created\_at 제외), Q6 diff |
| `test_resolver.py` | 정확 일치, 자동 병합 임계, judge mock same/different/unsure 분기, 검토 큐 생성 |
| `test_claim_graph.py` | 스키마, 불변식(레이어 재계산·hypothesis 상한·evidence 존재) |
| `test_slice.py` | L0에 L1 Claim 0개, brief 블록 집합, items 수 |
| `test_gate.py` | 각주 100%, 금지어, 레이어 누출, 숫자(brief), 블록 순서, 길이 |
| `test_ledger.py` | 사전 등록 불변, 재해소 금지, Brier·보정 |
| `test_tools.py` | 도구 11개 입력 검증·토큰 상한·빈 결과 hint·asof 누락 거부 |

이 목록이 전부 초록이고 기술 명세서 §7.2 골든 임계를 넘으면 Week 10의 dry-run을 시작할 수 있다.
