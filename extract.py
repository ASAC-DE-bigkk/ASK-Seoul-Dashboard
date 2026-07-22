"""카탈로그 스냅샷 추출기 — dbt 아티팩트 + Trino 실측을 JSON 하나로 박제한다.

사상: "계산은 파이프라인이 미리, API는 얇게". 이 스크립트가 W3 본작업에서
Airflow 태스크(또는 transform 후속 스텝)가 될 부분의 데모 축소판이다.

도메인 2계층:
  - culture (rich)  — dbt manifest/catalog.json 보유 → 설명·contract·계보·quality까지
  - 그 외 (basic)   — Trino 실측만(스키마·행수·기간·샘플). dbt 아티팩트가 없어서
                       설명/계약/계보는 비어 있다 = "메타데이터 채무"가 화면에 그대로 보인다.

소스 3종:
  1. dbt manifest.json  — 모델 설명·컬럼 설명·contract·계보(depends_on)
  2. dbt catalog.json   — 물리 컬럼 타입 (docs generate 산출물)
  3. Trino (docker compose exec 경유) — 행수·기간·quality_status 분포·샘플 5행

실행:  python extract.py   →  snapshot/catalog_snapshot.json
전제:  sample/ 스택 기동 중, culture dbt target/ 에 manifest.json + catalog.json 존재.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent


def resolve_sample_dir(here: Path) -> Path:
    """Dashboard standalone checkout과 sample/dashboard submodule 배치를 모두 지원한다."""
    candidates = (here.parent, here.parent / "sample")
    for candidate in candidates:
        if (candidate / "docker-compose.yml").is_file() and (candidate / "dbt").is_dir():
            return candidate
    return here.parent / "sample"


SAMPLE_DIR = resolve_sample_dir(HERE)
TARGET_DIR = SAMPLE_DIR / "dbt" / "domains" / "culture" / "target"
DBT_DOMAINS_DIR = SAMPLE_DIR / "dbt" / "domains"
OUT_PATH = HERE / "snapshot" / "catalog_snapshot.json"

# 외부 공개 분류(#269). 소스 오브 트루스 = dbt 모델 config.meta.external.
#   도메인이 자기 yml 에 meta.external 을 달면 그 값이 우선한다. 없으면 아래 내부 목록으로 판정.
#   → 외부 카탈로그는 external=true 만 노출(예: slo_daily 파이프라인 지표는 외부 비공개).
INTERNAL_GOLD = {
    "gold_culture_slo_daily",             # 파이프라인 SLO 운영 지표 — 외부 비공개(내부/팀 전용)
    "gold_culture_movie_boxoffice_daily", # KOBIS 공개 API 재포장 — Q&A 메트릭만
    "gold_culture_location_daily",        # activity_by_dong(동 스카폴드) 의 구 롤업 — 내부
    "gold_culture_sports_schedule",       # 수동 seed(SLA 불가) — event_schedule 소스로 강등
    "gold_culture_reservation_daily",     # 서울시 공공예약 재노출 — event_schedule 흡수
}


def is_external(node: dict, name: str) -> bool:
    """외부 공개 여부: dbt meta.external 우선, 없으면 내부 목록으로 판정."""
    meta = node.get("config", {}).get("meta", {})
    if "external" in meta:
        return bool(meta["external"])
    return name not in INTERNAL_GOLD

# basic 도메인 description 을 끌어올 dbt 프로젝트(모델명 전역 유일 → 병합 lookup).
# traffic·weather 는 하나의 dbt 프로젝트(traffic_weather)로 합쳐져 있다.
BASIC_MANIFEST_PROJECTS = ("commerce", "citydata", "traffic_weather", "transit")

MAX_SAMPLE_TEXT = 120  # 샘플 셀 문자열 절단 길이 (UI 가독성)

# ── 공개 D1 서빙 API (sample/serving, ASAC-DAG#445) ─────────────────
# citydata_serving_export DAG 가 D1 에 올린 골드 = 카탈로그 화면에서 '실데이터 조회' 링크를
# 걸 대상. 목록의 정본은 그 DAG(FAST_TABLES+DAILY_TABLES) — 여기 12종은 그 사본이다.
# by_time(서빙 보류)·hourly 3종·demographics 는 아직 미적재(다음 확장 시 목록에 추가).
SERVING_API_BASE = "https://ask-seoul-citydata-api.dy950328.workers.dev"
SERVED_TABLES = {
    "gold_citydata_place_latest", "gold_citydata_place_scorecard", "gold_citydata_hot_commerce",
    "gold_citydata_ppltn_trend", "gold_citydata_ppltn_anomaly", "gold_citydata_ppltn_forecast",
    "gold_citydata_ppltn_x_commerce_dong", "gold_citydata_charger_availability",
    "gold_citydata_ppltn_daily", "gold_citydata_cmrcl_daily",
    "gold_citydata_purchasing_power_daily", "gold_citydata_ppltn_x_culture_daily",
}

# ── 무스키마 행 단위 품질 규칙 (basic 도메인) ─────────────────────────
# culture 는 silver 에 quality_status 컬럼을 박지만(공간 매칭 정밀도),
# citydata 는 공간축이 seed 사전매핑이라 그 라벨이 무의미하다. 대신 도메인이
# 의미 있는 행 단위 품질(핵심 신호 결측·부분 결측)을 CASE 식으로 선언하면
# extractor 가 추출 시점에 즉석 분류한다 — silver 스키마 변경 없음.
# 라벨 어휘: ok / partial* (부분 결측) / missing_core (핵심 결측) — 화면 Q_META 와 동기.
BASIC_QUALITY_RULES: dict[str, str] = {
    "silver_citydata_ppltn": (
        "CASE WHEN area_ppltn_min IS NULL OR area_ppltn_max IS NULL OR area_congest_lvl IS NULL "
        "THEN 'missing_core' WHEN male_ppltn_rate IS NULL OR ppltn_rate_20 IS NULL "
        "THEN 'partial_segment' ELSE 'ok' END"
    ),
    "silver_citydata_air": (
        "CASE WHEN pm25 IS NULL OR pm10 IS NULL THEN 'missing_core' "
        "WHEN air_idx IS NULL OR temperature IS NULL THEN 'partial' ELSE 'ok' END"
    ),
    "silver_citydata_cmrcl": (
        "CASE WHEN payment_count IS NULL OR cmrcl_lvl IS NULL THEN 'missing_core' "
        "WHEN rate_20 IS NULL OR male_rate IS NULL THEN 'partial_segment' ELSE 'ok' END"
    ),
    "silver_citydata_sbike": (
        "CASE WHEN parking_count IS NULL OR rack_count IS NULL THEN 'missing_core' "
        "WHEN spot_longitude IS NULL OR spot_latitude IS NULL THEN 'partial_geo' ELSE 'ok' END"
    ),
    "silver_citydata_charger": (
        "CASE WHEN charger_stat IS NULL THEN 'missing_core' "
        "WHEN output_kw IS NULL OR stat_longitude IS NULL THEN 'partial' ELSE 'ok' END"
    ),
    # 30분 승하차 NULL 은 새벽 1~4시에 집중(2~4시 100%) = 심야 운행 중단의 정상 결측.
    # 실측(2026-07-18): 시간대별 NULL 분포로 확인 — 수집기간 차이 아님.
    "silver_citydata_transit_ppltn": (
        "CASE WHEN gton_30min_max IS NULL AND gtoff_30min_max IS NULL THEN "
        "(CASE WHEN hour(observed_at) BETWEEN 1 AND 4 THEN 'no_service' ELSE 'missing_core' END) "
        "WHEN station_count IS NULL OR station_count = 0 THEN 'partial' ELSE 'ok' END"
    ),
}

# dbt 아티팩트가 없는 도메인 — Trino 실측만으로 basic 카탈로그 구성. 라벨 → dev 스키마.
OTHER_DOMAINS = {
    "commerce": "commerce",
    "traffic": "traffic",
    "weather": "weather",
    "citydata": "seoul_citydata",
    "transit": "transit",
}


def trino_rows(sql: str, timeout: int = 300) -> list[dict]:
    """Trino CLI(JSON 출력 = 행마다 JSON 객체 한 줄)를 실행해 dict 리스트로 반환."""
    cmd = [
        "docker", "compose", "exec", "-T", "trino",
        "trino", "--output-format", "JSON", "--execute", sql,
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=SAMPLE_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"trino timeout({timeout}s): {sql[:120]}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"trino failed: {proc.stderr.strip()[:300]}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def load_artifacts() -> tuple[dict, dict]:
    manifest = json.loads((TARGET_DIR / "manifest.json").read_text(encoding="utf-8"))
    catalog = json.loads((TARGET_DIR / "catalog.json").read_text(encoding="utf-8"))
    return manifest, catalog


def node_lookup(manifest: dict) -> dict:
    """unique_id → 노드 (models + sources, 패키지 무관)."""
    return {**manifest["nodes"], **manifest["sources"]}


def upstream_layers(uid: str, nodes: dict) -> dict[str, list[str]]:
    """BFS로 상류 전체를 걷어 레이어별 이름 목록으로 묶는다."""
    seen, queue, layers = set(), [uid], {"silver": [], "intermediate": [], "dim_seed": [], "bronze_source": []}
    while queue:
        cur = queue.pop(0)
        for dep in nodes.get(cur, {}).get("depends_on", {}).get("nodes", []):
            if dep in seen or dep not in nodes:
                continue
            seen.add(dep)
            queue.append(dep)
            name = nodes[dep]["name"]
            if name.startswith("silver_"):
                layers["silver"].append(name)
            elif name.startswith("int_"):
                layers["intermediate"].append(name)
            elif dep.startswith("source.") or name.startswith("bronze_"):
                layers["bronze_source"].append(name)
            else:  # dim_admin_dong(asac_axes), seed 등
                layers["dim_seed"].append(name)
    return {k: sorted(set(v)) for k, v in layers.items() if v}


def quality_parents(uid: str, nodes: dict) -> list[str]:
    """가장 가까운 silver 조상 중 quality_status 컬럼을 가진 모델의 unique_id."""
    seen, queue, hits = set(), [uid], []
    while queue:
        cur = queue.pop(0)
        for dep in nodes.get(cur, {}).get("depends_on", {}).get("nodes", []):
            if dep in seen or dep not in nodes:
                continue
            seen.add(dep)
            node = nodes[dep]
            if node["name"].startswith("silver_"):
                if "quality_status" in node.get("columns", {}):
                    hits.append(dep)
                continue  # silver 에서 멈춤 (그 위는 bronze)
            queue.append(dep)
    return hits


def truncate_cell(value):
    if isinstance(value, str) and len(value) > MAX_SAMPLE_TEXT:
        return value[:MAX_SAMPLE_TEXT] + "…"
    return value


def measure(rel: str, columns: list[dict]) -> tuple[int, dict | None, list[dict]]:
    """행수·시간축 범위·샘플 5행 — rich/basic 두 경로가 공유하는 Trino 실측."""
    row_count = trino_rows(f"SELECT count(*) AS c FROM {rel}")[0]["c"]

    date_col = next((c["name"] for c in columns if c["type"].startswith(("date", "timestamp"))), None)
    date_range = None
    if date_col:
        r = trino_rows(
            f'SELECT cast(min("{date_col}") AS varchar) AS mn, cast(max("{date_col}") AS varchar) AS mx FROM {rel}'
        )[0]
        date_range = {"column": date_col, "min": r["mn"], "max": r["mx"]}

    try:  # 샘플은 느리거나 실패해도 테이블 자체는 유지 (count/스키마는 이미 확보)
        sample = [
            {k: truncate_cell(v) for k, v in row.items()}
            for row in trino_rows(f"SELECT * FROM {rel} LIMIT 5", timeout=60)
        ]
    except RuntimeError as exc:
        print(f"  ! sample skip: {exc}")
        sample = []
    return row_count, date_range, sample


def test_gates(manifest: dict) -> dict[str, list[str]]:
    """모델 uid → 그 모델에 걸린 dbt 테스트 라벨 목록 (예 'unique_grain', 'not_null(area_cd)').
    실행 결과가 아니라 **정의된 게이트**다 — CI/DAG 에서 매 run 검증되는 계약의 가시화."""
    gates: dict[str, list[str]] = {}
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "test":
            continue
        attached = node.get("attached_node")
        if not attached:
            continue
        tm = node.get("test_metadata") or {}
        label = tm.get("name") or node.get("name", "test")
        column = (tm.get("kwargs") or {}).get("column_name")
        gates.setdefault(attached, []).append(f"{label}({column})" if column else label)
    return {uid: sorted(set(v)) for uid, v in gates.items()}


def load_basic_meta() -> dict:
    """basic 도메인 모델의 description·컬럼설명·tags·contract·serving_tier·테스트게이트 를
    각 도메인 manifest 에서 병합해 name → 메타 dict 로. 모델명 전역 유일 전제
    (gold_citydata_*·gold_traffic_* 등). manifest 는 도메인 dbt 를
    `dbt deps && dbt parse` 하면 생긴다(gitignore 산출물)."""
    lookup: dict[str, dict] = {}
    for proj in BASIC_MANIFEST_PROJECTS:
        path = DBT_DOMAINS_DIR / proj / "target" / "manifest.json"
        if not path.exists():
            print(f"  ! manifest 없음(설명 스킵): {proj}")
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        all_nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}
        gates = test_gates(manifest)
        for uid, node in manifest.get("nodes", {}).items():
            if node.get("resource_type") != "model":
                continue
            cfg = node.get("config", {})
            lookup[node["name"]] = {
                "description": node.get("description", ""),
                "columns": {
                    c: (meta.get("description", "") or "")
                    for c, meta in node.get("columns", {}).items()
                },
                "tags": node.get("tags", []),
                "contract_enforced": bool(cfg.get("contract", {}).get("enforced")),
                "materialized": cfg.get("materialized", ""),
                "external": bool(cfg.get("meta", {}).get("external", True)),
                # 서빙 tier·갱신주기 — config.meta.serving_tier / refresh (citydata D1 서빙 상세, drawer 표시).
                #   카드 pill 은 external(#269) — external=카탈로그 노출, serving_tier=D1 적재 tier(별개).
                "serving_tier": (cfg.get("meta") or {}).get("serving_tier"),
                "refresh": (cfg.get("meta") or {}).get("refresh"),
                "tests": gates.get(uid, []),
                # 계보 — culture rich 와 같은 upstream_layers 재사용 (도메인 manifest 내 한정)
                "lineage": upstream_layers(uid, all_nodes),
            }
    return lookup


def basic_quality(silver_names: list[str], schema: str, cache: dict) -> list[dict]:
    """계보상 상류 silver 중 품질 규칙이 선언된 것의 즉석 분포. 실패는 스킵(품질은 부가정보)."""
    out = []
    for name in silver_names:
        expr = BASIC_QUALITY_RULES.get(name)
        if not expr:
            continue
        if name not in cache:
            try:
                dist = trino_rows(
                    f"SELECT {expr} AS quality_status, count(*) AS c "
                    f"FROM iceberg_dev.{schema}.{name} GROUP BY 1 ORDER BY 2 DESC"
                )
                cache[name] = {"table": name, "distribution": {d["quality_status"]: d["c"] for d in dist}}
            except RuntimeError as exc:
                print(f"  ! quality skip({name}): {exc}")
                cache[name] = None
        if cache[name]:
            out.append(cache[name])
    return out


def extract_basic_domain(domain: str, schema: str, meta_lookup: dict) -> list[dict]:
    """dbt 계보 기반으로 description·컬럼설명·tags·contract·serving_tier·테스트게이트를 채우고,
    품질 규칙이 선언된 도메인은 상류 silver 의 행 단위 품질도 즉석 계측한다."""
    quality_cache: dict[str, dict | None] = {}
    names = sorted(
        r["Table"] for r in trino_rows(f"SHOW TABLES FROM iceberg_dev.{schema}")
        if r["Table"].startswith("gold_")
        and "__dbt_" not in r["Table"]          # dbt 임시/백업 테이블 제외
        and not r["Table"].startswith("recovery_")
    )
    tables = []
    for name in names:
        rel = f"iceberg_dev.{schema}.{name}"
        meta = meta_lookup.get(name, {})
        col_desc = meta.get("columns", {})
        print(f"→ [{domain}] {name}")
        try:
            cols = trino_rows(f"SHOW COLUMNS FROM {rel}")
            columns = [
                {
                    "name": c["Column"],
                    "type": c["Type"],
                    # manifest 설명 우선, 없으면 테이블 물리 COMMENT(persist_docs) 폴백.
                    "description": col_desc.get(c["Column"]) or (c.get("Comment") or ""),
                }
                for c in cols
            ]
            row_count, date_range, sample = measure(rel, columns)
        except RuntimeError as exc:
            print(f"  ! skip: {exc}")
            continue
        tables.append({
            "name": name,
            "domain": domain,
            # 타 도메인 basic: 기본 외부 공개. 각 도메인이 dbt meta.external 로 내부 마트를 표시하면 반영됨.
            "external": bool(meta.get("external", True)),
            "relation": rel,
            "description": meta.get("description", ""),
            "tags": meta.get("tags", []),
            "contract_enforced": meta.get("contract_enforced", False),
            "materialized": meta.get("materialized", ""),
            "serving_tier": meta.get("serving_tier"),
            "refresh": meta.get("refresh"),
            "tests": meta.get("tests", []),
            "served_url": f"{SERVING_API_BASE}/data/{name}" if name in SERVED_TABLES else None,
            "on_table_exists": None,
            "row_count": row_count,
            "date_range": date_range,
            "columns": columns,
            "quality": basic_quality(meta.get("lineage", {}).get("silver", []), schema, quality_cache),
            "lineage": meta.get("lineage", {}),
            "sample": sample,
        })
    return tables


def write_snapshot(snapshot: dict) -> None:
    """완성된 JSON만 원자적으로 교체해 중간 실패 시 기존 snapshot을 보존한다."""
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=OUT_PATH.parent,
            prefix=f".{OUT_PATH.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.chmod(0o644)
        os.replace(temp_path, OUT_PATH)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def merge_basic_domains(
    snapshot: dict,
    replacements: dict[str, list[dict]],
    observed_at: str,
) -> dict:
    """기존 snapshot의 비대상 domain을 보존하고 지정 basic domain만 교체한다."""
    if not replacements:
        raise ValueError("at least one basic domain replacement is required")
    unknown = set(replacements) - set(OTHER_DOMAINS)
    if unknown:
        raise ValueError(f"unknown basic domains: {sorted(unknown)}")
    for domain, tables in replacements.items():
        if not tables:
            raise ValueError(f"refusing to replace {domain} with an empty table set")
        if any(table.get("domain") != domain for table in tables):
            raise ValueError(f"replacement contains a table from another domain: {domain}")

    merged = deepcopy(snapshot)
    replaced_domains = set(replacements)
    tables = [
        table
        for table in merged.get("tables", [])
        if table.get("domain") not in replaced_domains
    ]
    for domain in OTHER_DOMAINS:
        tables.extend(replacements.get(domain, []))

    domain_order = {"culture": 0, **{
        domain: index
        for index, domain in enumerate(OTHER_DOMAINS, start=1)
    }}
    tables.sort(key=lambda table: (
        domain_order.get(table.get("domain", ""), len(domain_order)),
        table.get("name", ""),
    ))

    previous_generated_at = merged.get("generated_at", observed_at)
    previous_domains = merged.get("domains", {})
    domain_generated_at = {
        domain: previous_generated_at
        for domain in previous_domains
    }
    domain_generated_at.update(merged.get("domain_generated_at", {}))
    domain_generated_at.update({
        domain: observed_at
        for domain in replacements
    })

    domains: dict[str, int] = {}
    for table in tables:
        domain = table.get("domain", "")
        domains[domain] = domains.get(domain, 0) + 1

    merged.update({
        "generated_at": observed_at,
        "domain_generated_at": domain_generated_at,
        "domains": domains,
        "table_count": len(tables),
        "tables": tables,
        "refresh": {
            "mode": "partial_basic",
            "source_system": "trino",
            "catalog": "iceberg_dev",
            "domains": list(replacements),
            "observed_at": observed_at,
        },
    })
    return merged


def refresh_basic_domains(domains: list[str]) -> None:
    """culture artifact 없이도 현재 Trino의 지정 basic domain만 안전하게 갱신한다."""
    if not OUT_PATH.is_file():
        raise FileNotFoundError(f"base snapshot does not exist: {OUT_PATH}")
    unique_domains = list(dict.fromkeys(domains))
    metadata = load_basic_meta()
    replacements: dict[str, list[dict]] = {}
    for domain in unique_domains:
        tables = extract_basic_domain(domain, OTHER_DOMAINS[domain], metadata)
        if not tables:
            raise RuntimeError(f"no Gold tables discovered for basic domain: {domain}")
        replacements[domain] = tables

    observed_at = datetime.now(timezone.utc).isoformat()
    current = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    snapshot = merge_basic_domains(current, replacements, observed_at)
    write_snapshot(snapshot)
    print(
        f"✓ refreshed {','.join(unique_domains)} in {OUT_PATH} "
        f"({OUT_PATH.stat().st_size:,} bytes, tables={len(snapshot['tables'])})"
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    manifest, catalog = load_artifacts()
    nodes = node_lookup(manifest)
    golds = {
        uid: n for uid, n in manifest["nodes"].items()
        if n.get("resource_type") == "model" and n["name"].startswith("gold_")
    }
    print(f"gold models: {len(golds)}")

    quality_cache: dict[str, dict] = {}  # silver uid → 분포 (골드끼리 공유)
    rich_gates = test_gates(manifest)
    tables = []
    for uid, node in sorted(golds.items(), key=lambda kv: kv[1]["name"]):
        name, rel = node["name"], node["relation_name"]
        print(f"→ {name}")
        cat_cols = catalog["nodes"].get(uid, {}).get("columns", {})
        columns = [
            {
                "name": c,
                "type": meta.get("type", ""),
                "description": node.get("columns", {}).get(c, {}).get("description", ""),
            }
            for c, meta in sorted(cat_cols.items(), key=lambda kv: kv[1].get("index", 0))
        ]
        if not columns:  # catalog.json 이 모델 빌드보다 오래된 경우 — Trino 실측 폴백
            columns = [
                {
                    "name": c["Column"],
                    "type": c["Type"],
                    "description": node.get("columns", {}).get(c["Column"], {}).get("description", ""),
                }
                for c in trino_rows(f"SHOW COLUMNS FROM {rel}")
            ]

        row_count, date_range, sample = measure(rel, columns)

        quality = []
        for silver_uid in quality_parents(uid, nodes):
            if silver_uid not in quality_cache:
                silver = nodes[silver_uid]
                dist = trino_rows(
                    f"SELECT quality_status, count(*) AS c FROM {silver['relation_name']} GROUP BY 1 ORDER BY 2 DESC"
                )
                quality_cache[silver_uid] = {
                    "table": silver["name"],
                    "distribution": {d["quality_status"]: d["c"] for d in dist},
                }
            quality.append(quality_cache[silver_uid])

        tables.append({
            "name": name,
            "domain": "culture",
            "external": is_external(node, name),
            "relation": rel.replace('"', ""),
            "description": node.get("description", ""),
            "tags": node.get("tags", []),
            "contract_enforced": bool(node.get("config", {}).get("contract", {}).get("enforced")),
            "materialized": node.get("config", {}).get("materialized", ""),
            "serving_tier": (node.get("config", {}).get("meta") or {}).get("serving_tier"),
            "refresh": (node.get("config", {}).get("meta") or {}).get("refresh"),
            "tests": rich_gates.get(uid, []),
            "served_url": f"{SERVING_API_BASE}/data/{name}" if name in SERVED_TABLES else None,
            "on_table_exists": node.get("config", {}).get("on_table_exists")
                               or node.get("config", {}).get("extra", {}).get("on_table_exists"),
            "row_count": row_count,
            "date_range": date_range,
            "columns": columns,
            "quality": quality,
            "lineage": upstream_layers(uid, nodes),
            "sample": sample,
        })

    basic_meta = load_basic_meta()
    for domain, schema in OTHER_DOMAINS.items():
        tables.extend(extract_basic_domain(domain, schema, basic_meta))

    domains = {}
    for t in tables:
        domains[t["domain"]] = domains.get(t["domain"], 0) + 1

    generated_at = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "generated_at": generated_at,
        "domain_generated_at": {
            domain: generated_at
            for domain in domains
        },
        "domain": "all",
        "domains": domains,
        "dbt_project": manifest.get("metadata", {}).get("project_name", ""),
        "table_count": len(tables),
        "tables": tables,
    }
    write_snapshot(snapshot)
    print(f"✓ wrote {OUT_PATH} ({OUT_PATH.stat().st_size:,} bytes, tables={len(tables)})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ASK SEOUL catalog snapshot extractor")
    parser.add_argument(
        "--refresh-basic-domain",
        action="append",
        choices=tuple(OTHER_DOMAINS),
        default=[],
        help="전체 culture artifact 없이 지정 basic domain만 현재 Trino에서 갱신",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.refresh_basic_domain:
        refresh_basic_domains(args.refresh_basic_domain)
    else:
        main()
