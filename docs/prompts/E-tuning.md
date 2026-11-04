대상: {추출 프롬프트 | lead·subagent 프롬프트 | editorial 프롬프트}. 현재 지표: {make golden 결과 또는 dry-run 게이트 실패 목록 붙여넣기}.

읽어라: docs/spec/code-phase0.md §{2.2 | 5.4~5.5 | 6.4}, 최근 실패 사례 20건 (extraction_errors 또는 report_runs.gate_report 또는 Langfuse trace 링크).

규칙:
- 프롬프트 변경은 PROMPT_VERSION을 올리고 (ext-1.0 → ext-1.1), 변경 전후 `make golden` 수치를 docs/review/prompt-{version}.md에 나란히 적는다.
- 한 번에 가설 하나만 바꾼다. 두 개를 동시에 바꾸면 무엇이 효과였는지 모른다.
- 지표가 임계(tech §7.2)를 넘기면 멈춘다. 더 올리려고 few-shot을 늘리지 마라 (캐시 비용).
- 추출 프롬프트의 "Hard rules" 번호와 의미는 바꾸지 마라. 예시와 표현만.
- lead 프롬프트의 "절대 규칙" 10개는 삭제·완화 금지. 추가만 가능.

보고: 바꾼 것 한 줄, 전후 지표, 남은 실패 유형 상위 3개.
