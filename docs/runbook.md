# Runbook (Phase 0, 로컬 compose)

자주 하는 운영 작업. 증상별 조치 표는 `docs/spec/tech-phase0.md` §6.6.

## deploy — 스케줄 등록

`make up && make deploy` — `make up`이 worker 이미지를 다시 빌드하고(최신 코드가 `/app`에 구워짐), `make deploy`가 worker 컨테이너 안에서 `flows/deploy.py`를 실행해 deployment를 서버에 등록한다. 확인: `PREFECT_API_URL=http://localhost:4200/api uv run prefect deployment ls`에 `ingest/ingest`, 상세는 `prefect deployment inspect ingest/ingest`(cron `*/15 * * * *` Asia/Seoul, concurrency limit 1·`CANCEL_NEW`). 로컬 Mac이 꺼져 있거나 Docker가 멈추면 스케줄 수집도 멈춘다.

## 코드 변경 후 재배포

worker는 이미지에 구운 `/app` 코드를 실행하므로 flow·패키지 코드를 바꾸면 반영되지 않는다. `make up`(이미지 재빌드, worker 재생성) 뒤 `make deploy`를 다시 실행한다(멱등 — 같은 deployment를 갱신). 스케줄 run 확인은 5분 이상 간격으로 가볍게: `PREFECT_API_URL=http://localhost:4200/api uv run prefect flow-run ls --flow-name ingest --limit 3`.

## KR lookback — 한국어 입력 확보 (결정 W2-3, 1회만)

스케줄이 정상으로 도는 것을 확인한 뒤 한 번만 실행한다(KR 3소스가 최근 60일치를 받아 온다. 한 run에 다 못 받아도 소스당 120초 예산 안에서 여러 run에 걸쳐 수렴).

```sql
UPDATE sources SET last_ok_at = now() - interval '60 days' WHERE country = 'KR' AND enabled;
```

다음 스케줄 run 뒤 `select count(*) from chunks c join raw_documents d on d.id = c.doc_id where d.lang = 'ko'`로 한국어 chunk 수를 본다. 15 미만이면 gold 보충 경로(week-2 T15).
