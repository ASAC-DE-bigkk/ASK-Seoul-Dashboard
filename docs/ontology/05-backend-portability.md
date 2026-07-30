# 05 — 백엔드 이식성: Trino(R2)에서 검증한 온톨로지가 D1·RDB 에서도 동일한가?

지금까지 실증은 **Trino(R2 gold)** 로 했다. 같은 형태의 데이터를 **Cloudflare D1(SQLite)·RDB**
로 옮겨도 온톨로지가 동일하게 동작하는가? **그렇다 — 실증했다.** 온톨로지(role·스펙·계약)는
**백엔드 중립**이고, 방언(dialect)·실행기만 갈아끼우면 된다.

## 왜 이식되나 (설계)
- **온톨로지 = 스냅샷만 읽는 백엔드 중립 레지스트리.** 소스가 어느 DB 에 사는지는 스냅샷의
  `datasource`(연결 이름)·`backend`(방언 키)가 말한다(`backends.py` 도크스트링).
- **스펙은 방언과 무관.** `run_query`/`querybuilder.build` 이 받는 스펙(dims·measures·filters…)은
  동일하고, `resolve_dialect(source.backend)` 가 방언별 SQL 을 조립한다.
- **불변식:** trino 방언 출력은 종전과 **byte-동일**(캐시 키 보존). 다른 방언은 CAST·LIMIT·식별자
  인용·리터럴 이스케이프만 프로파일로 분기.

## 지원 방언 (querybuilder `DIALECTS`)
| backend | 집계 CAST | LIMIT | 실행기(backends) | 비고 |
|---|---|---|---|---|
| `trino` | `double` | `limit` | trino.py(캐시·stale 폴백) | 기본(R2 gold) |
| `postgres`(+cockroachdb/redshift 별칭) | `double precision` | `limit` | psycopg/psycopg2, `read_only=on` | |
| `sqlite`(= **Cloudflare D1**) | `real` | `limit` | URI `mode=ro` + `PRAGMA query_only` | 파일 SQLite |
| `mysql`/`mariadb` | `double` | `limit` | pymysql/connector, ANSI_QUOTES·READ ONLY | |
| `oracle` | `binary_double` | `fetch first` | oracledb, `SET TRANSACTION READ ONLY` | GROUP BY 식 반복 |
| `mssql` | `float` | `top` | pyodbc/pymssql, 계정 SELECT 권한 | LIKE `[` 이스케이프 |
| `duckdb` | (trino 별칭) | `limit` | duckdb read_only | try_cast 검증 계열 |

## 실증 — 같은 온톨로지 스펙, 두 백엔드, 동일 결과
```
spec = {dims:["event_type"], measures:[{field:"cnt", agg:"sum", alias:"total"}], order desc}

Trino 방언 :  select "event_type" as "event_type", cast(sum("cnt") as double) as "total"
              from "iceberg_dev"."commerce"."gold_license_flow_monthly" group by 1 order by "total" desc limit 100
SQLite(D1) :  select "event_type" as "event_type", cast(sum("cnt") as real)   as "total"
              from "flow_yearly" group by 1 order by "total" desc limit 100

Trino  rows: opened 2,783,157 · closed 1,654,011
SQLite rows: opened 2,783,157 · closed 1,654,011
IDENTICAL_RESULT = true          (mode: live / live)
```
> 절차: Trino 에서 (연도×event_type) 상세를 뽑아 SQLite 테이블 `flow_yearly` 로 복제 → 소스 dict 의
> `backend/datasource/relation` 만 교체(필드 role 은 동일) → 같은 스펙 실행. 두 방언 SQL 은
> **CAST(double↔real)·relation** 만 다르고 논리·결과는 완전 일치. `describe_source`/`run_query`/
> `list_metrics` 등 도구 표면도 그대로 동작한다.

## Cloudflare D1 = SQLite 방언
- D1 은 SQLite 호환이므로 **방언은 `sqlite` 그대로**(질의 형태·CAST·LIMIT 동일).
- 유일한 통합 작업: D1 은 HTTP API 로 접속하므로 `backends.py` 에 **D1 실행기 한 개**만 추가하면 된다
  (`_execute_sqlite` 와 동형, 파일 대신 HTTP). **온톨로지·스펙·SQL·도구 계약은 무변경.**
- 즉 "R2→Trino 에서 검증한 것"이 D1 로 옮겨도 **온톨로지 레벨에선 0 변경**, 실행기 레벨에서만 어댑터.

## MCP·RAG 개발 시 어떻게 쓰이나 (이식성 관점)
| 시나리오 | 구성 | 가치 |
|---|---|---|
| **MCP 서버가 여러 백엔드 소스 노출** | 스냅샷에 소스별 `datasource`/`backend` 지정 → 한 서버가 Trino gold + D1 서빙 + RDB 를 동시 서빙, AI 는 백엔드를 모름 | 3 |
| **엣지 서빙(D1)로 저지연 조회** | 자주 쓰는 집계를 D1 로 미러 → 같은 스펙, `mode=live`, Trino 부하·지연 회피 | 2 |
| **RAG 그라운딩** | `ontology_manifest()`/`describe_source`/`value_labels` 를 임베딩·검색 → 어느 백엔드 소스든 동일 계약으로 인용 | 2 |
| **오프라인/CI 재현** | 스냅샷 + SQLite 로 온톨로지·질의 계약을 DB 없이 회귀(`tests/test_database_portability.py` 결) | 2 |
| **개발 로컬 = 프로덕션 방언 검증** | 로컬 SQLite 로 스펙을 짜고 프로덕션 Trino 로 실행 — 스펙 불변 | 1 |

> 요약: **온톨로지는 데이터가 어디 사는지에 독립**이다. Trino(R2)에서 증명한 계약·안전·라벨·롤업이
> D1·RDB 로 그대로 이식되며, MCP/RAG 는 그 위에서 백엔드를 몰라도 되는 단일 의미 계약을 얻는다.
