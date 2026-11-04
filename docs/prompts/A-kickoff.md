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
