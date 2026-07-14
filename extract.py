"""카탈로그 스냅샷 추출기 — dbt 아티팩트 + Trino 실측을 JSON 하나로 박제한다.

사상: "계산은 파이프라인이 미리, API는 얇게". 이 스크립트가 W3 본작업에서
Airflow 태스크(또는 transform 후속 스텝)가 될 부분의 데모 축소판이다.

소스 3종:
  1. dbt manifest.json  — 모델 설명·컬럼 설명·contract·계보(depends_on)
  2. dbt catalog.json   — 물리 컬럼 타입 (docs generate 산출물)
  3. Trino (docker compose exec 경유) — 행수·기간·quality_status 분포·샘플 5행

실행:  python extract.py   →  snapshot/catalog_snapshot.json
전제:  sample/ 스택 기동 중, dbt target/ 에 manifest.json + catalog.json 존재.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
SAMPLE_DIR = HERE.parent / "sample"
TARGET_DIR = SAMPLE_DIR / "dbt" / "domains" / "culture" / "target"
OUT_PATH = HERE / "snapshot" / "catalog_snapshot.json"

MAX_SAMPLE_TEXT = 120  # 샘플 셀 문자열 절단 길이 (UI 가독성)


def trino_rows(sql: str) -> list[dict]:
    """Trino CLI(JSON 출력 = 행마다 JSON 객체 한 줄)를 실행해 dict 리스트로 반환."""
    cmd = [
        "docker", "compose", "exec", "-T", "trino",
        "trino", "--output-format", "JSON", "--execute", sql,
    ]
    proc = subprocess.run(
        cmd, cwd=SAMPLE_DIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
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

        row_count = trino_rows(f"SELECT count(*) AS c FROM {rel}")[0]["c"]

        date_col = next((c["name"] for c in columns if c["type"].startswith(("date", "timestamp"))), None)
        date_range = None
        if date_col:
            r = trino_rows(
                f"SELECT cast(min({date_col}) AS varchar) AS mn, cast(max({date_col}) AS varchar) AS mx FROM {rel}"
            )[0]
            date_range = {"column": date_col, "min": r["mn"], "max": r["mx"]}

        sample = [
            {k: truncate_cell(v) for k, v in row.items()}
            for row in trino_rows(f"SELECT * FROM {rel} LIMIT 5")
        ]

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
            "relation": rel.replace('"', ""),
            "description": node.get("description", ""),
            "tags": node.get("tags", []),
            "contract_enforced": bool(node.get("config", {}).get("contract", {}).get("enforced")),
            "materialized": node.get("config", {}).get("materialized", ""),
            "on_table_exists": node.get("config", {}).get("on_table_exists")
                               or node.get("config", {}).get("extra", {}).get("on_table_exists"),
            "row_count": row_count,
            "date_range": date_range,
            "columns": columns,
            "quality": quality,
            "lineage": upstream_layers(uid, nodes),
            "sample": sample,
        })

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "domain": "culture",
        "dbt_project": manifest.get("metadata", {}).get("project_name", ""),
        "table_count": len(tables),
        "tables": tables,
    }
    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ wrote {OUT_PATH} ({OUT_PATH.stat().st_size:,} bytes, tables={len(tables)})")


if __name__ == "__main__":
    main()
