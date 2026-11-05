이번 주는 Week 4다. 다음을 읽어라:
- docs/spec/tech-phase0.md §8.2의 Week 4 행 ("소스 15개국 45개, GDELT 발견, EDGAR·DART·FRED·EIA 파서, 통신사 전재, Storyline 시드 20 + Linker 규칙, gold 100" / 주말 기준 상태: A급 일일 커버 측정 시작)
- docs/spec/code-phase0.md §1 (ingest), §3.4 (Storyline Linker), §7.1 (gold 포맷)
- docs/tasks/week-3.md 미완료, docs/review/week-3.md, docs/spec/decisions.md

이번 주에 명세에 없는 추가 요구 3건이 있다. 태스크로 만들고 decisions.md에 명세 변경으로 기록하라:

(1) 보일러플레이트·첨부파일 문제. Week 2 gold 추출에서 한국 정부 소스의 chunk 20개 중 18개가 "자세한 내용은 첨부파일을 참고" 템플릿 또는 표였다.
  - normalizer에 템플릿 탐지: 문단 중복률 > 50% 또는 첨부 안내 패턴(ko·en) → status=skipped, 사유 'template'.
  - 첨부파일 본문 추출: 보도자료 페이지의 PDF·HWP 링크를 따라가 pymupdf / hwp5txt로 text를 채운다. 첨부가 본문이면 snapshot은 첨부 바이트, doc_type은 RELEASE 유지. 실패 시 skipped 'attachment_unreadable'.
  - sources/*.yaml에 `body: inline|attachment` 필드 추가(기본 inline).
(2) C급·인플루언서 층. sources/influencers.yaml(계정 50개 큐레이션: handle, platform, domain, author_type ∈ {insider, analyst, pundit}, reach, grade C)와 수집기 SubstackCrawler(RSS)·RedditCrawler(공식 API)·NaverBlogCrawler. YouTube·X는 이번 주 제외.
  - 승격 규칙 변경(Q3): 독립 출처 2개 중 A 또는 B가 ≥ 1 없으면 confirmed 불가. C급 Evidence는 claim_type=attribution 강제. insider 플래그 계정은 attribution claim에 한해 B급 가중.
  - 리포트에서 C급은 "주목받는 주장" 라벨로만, ③·④ 근거로는 불가 (code §6.6 게이트에 검사 추가).
(3) gold 소스 재선정. gold 100은 "본문이 페이지에 있는" 소스에서만 뽑는다: 한은 통화정책방향·의사록, 기재부 그린북, Fed FOMC statement·연설, BLS CPI 본문, BoJ Outlook. 라벨은 사람 2인 블라인드(W2-13), LLM 초안은 누락 대조용만.

할 일: docs/tasks/week-4.md를 작성해라. 코드는 쓰지 마라. 형식·규칙은 A 템플릿과 같다.
추가 규칙:
- 이번 주 총량이 주 25시간을 넘는다. 우선순위는 (1) 템플릿·첨부 > 15개국 소스 확대 > GDELT 발견 > 통신사 전재 > Storyline 시드·Linker > (2) C급 층 > gold 100. 넘치는 것은 "버릴 순서"에 적어 Week 5로.
- 각 태스크 완료 기준은 테스트 이름 또는 make 명령. 소스 확대는 "15개국 각각 A급 1 + B급 2, 전부 200 응답 + robots 허용 + 본문 300자 이상인 문서 ≥ 1건"으로.
- 리스크: hwp5txt 설치·라이선스, Reddit API 승인, 15개국 포털 파서 유지보수. 각각 1시간 안에 확인하는 방법을 적어라.
