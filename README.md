# culture Data Catalog — Marketplace 데모

Snowflake Marketplace 스타일의 **데이터 카탈로그 API + 웹화면** 데모.
W3 "품질·카탈로그 API화"의 culture 단독 축소판이다.

사상: **계산은 파이프라인이 미리, API는 얇게.** 요청 시 Trino를 두드리지 않고,
추출기가 박제한 스냅샷 JSON만 서빙한다 (조회 전용 서빙 레이어).

```
extract.py ──(dbt manifest/catalog.json + Trino 실측)──▶ snapshot/catalog_snapshot.json
app/main.py(FastAPI) ──▶ /api/v1/catalog/... + / (마켓플레이스 화면) + /docs (Swagger)
```

## 실행

```bash
# 1) 스냅샷 갱신 (전제: sample/ 스택 기동 + dbt target/ 에 manifest·catalog.json)
.venv/Scripts/python extract.py

# 2) 서버
.venv/Scripts/uvicorn app.main:app --port 8765
# → http://127.0.0.1:8765  (화면) · /docs (Swagger) · /health
```

## API (전부 GET, 조회 전용)

| 엔드포인트 | 내용 |
|---|---|
| `/api/v1/catalog/tables` | published gold 6종 요약 (카드용) |
| `/api/v1/catalog/tables/{name}` | 상세: 스키마·품질·계보·샘플 |
| `/api/v1/catalog/tables/{name}/schema` | 컬럼·물리 타입·설명 |
| `/api/v1/catalog/tables/{name}/quality` | 상류 silver quality_status 분포 |
| `/api/v1/catalog/tables/{name}/sample` | 샘플 5행 |

에러는 RFC 7807(problem+json). 응답 스키마는 Pydantic `response_model` 로 고정
(= API 의 contract enforced).

## W3 본작업으로 갈 때 바뀌는 것

- extract.py → Airflow 태스크(transform 후속 스텝)로 승격, 스냅샷은 마트/serving-postgres 로
- 서버 → compose 서비스 (공유 인프라 = 팀 게이트, serving-postgres 선례 패턴)
- 도메인 1개 → 6개 (manifest 경로만 도메인별로 늘리면 됨)
- SLO 엔드포인트 추가 (#257/DBT#110 마트 완성 후)
