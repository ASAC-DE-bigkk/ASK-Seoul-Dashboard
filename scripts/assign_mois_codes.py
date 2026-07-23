"""seoul_dong.json(행정동 지도 자산)에 MOIS 행정동코드(mois_code)를 부여한다.

배경: 자산의 code 는 KOSTAT(통계청, 2013) 체계라 gold 데이터의 MOIS(행안부) 코드와
호환되지 않는다 — 그래서 지금까지 행정동 지도는 이름 매칭만 가능했고, 동명이 겹치는
행정동(신사동: 강남구·관악구)은 '모호'로 제외됐다. 이 스크립트가 각 폴리곤에 MOIS
10자리 코드를 박아 코드 매칭을 가능하게 한다.

매칭 원리(코드 체계가 달라 코드끼리는 못 잇는다):
  1. MOIS 우주 = Trino `bronze_ref_admin_dong` (sgg_name, admin_dong_code, admin_dong_name).
  2. 자산 KOSTAT 코드 앞 5자리 = 구 프리픽스. 프리픽스별 동명 집합을 각 구의 MOIS
     동명 집합과 대조해 구를 판별한다(하드코딩 없는 자기서술 매칭).
  3. (구, 정규화 동명) → MOIS 코드. 동명 정규화: 공백 제거·'제N동/가'의 '제' 탈락·
     구분점(·/,) 통일.
  4. (구, 동명)이 ref 에서 코드 여러 개(개편 이력)면: gold 관측 코드 우선 → revised_date
     최신 우선.

실행(dashboard 루트, sample 스택 기동 중):
    python scripts/assign_mois_codes.py            # 자산 갱신 + 리포트
    python scripts/assign_mois_codes.py --dry-run  # 리포트만
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # dashboard/
ASSET = HERE / "app" / "static" / "charts" / "geo" / "seoul_dong.json"
SAMPLE = next(
    (c for c in (HERE.parent, HERE.parent / "sample")
     if (c / "docker-compose.yml").is_file()),
    HERE.parent,
)

REF_SQL = (
    "SELECT DISTINCT sgg_name, admin_dong_code, admin_dong_name, revised_date "
    "FROM iceberg_dev.commerce.bronze_ref_admin_dong"
)
GOLD_SQL = (
    "SELECT DISTINCT admin_dong_code "
    "FROM iceberg_dev.commerce.gold_license_dong_summary"
)


def trino_rows(sql: str) -> list[dict]:
    proc = subprocess.run(
        ["docker", "compose", "exec", "-T", "trino",
         "trino", "--output-format", "JSON", "--execute", sql],
        cwd=SAMPLE, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"trino failed: {proc.stderr.strip()[:300]}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def norm(name: str) -> str:
    """동명 정규화 — 표기 차이만 흡수한다(의미 병합 없음)."""
    s = unicodedata.normalize("NFC", str(name)).replace(" ", "")
    s = re.sub(r"([가-힣])제(\d)", r"\1\2", s)   # 면목제3동 → 면목3동
    s = re.sub(r"[·,]", ".", s)                  # 종로1·2·3·4가동 → 종로1.2.3.4가동
    return s


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ref = trino_rows(REF_SQL)
    gold_codes = {r["admin_dong_code"] for r in trino_rows(GOLD_SQL)}
    gj = json.loads(ASSET.read_text(encoding="utf-8"))
    feats = gj["features"]

    # (구, 정규화 동명) → 후보 [(code, revised, name)]
    by_gu_name: dict[tuple[str, str], list[tuple[str, str, str]]] = defaultdict(list)
    gu_names: dict[str, set[str]] = defaultdict(set)
    for r in ref:
        key = (r["sgg_name"], norm(r["admin_dong_name"]))
        by_gu_name[key].append(
            (r["admin_dong_code"], r.get("revised_date") or "", r["admin_dong_name"])
        )
        gu_names[r["sgg_name"]].add(norm(r["admin_dong_name"]))

    # KOSTAT 구 프리픽스 → 구 이름 (동명 집합 겹침 최대)
    prefix_names: dict[str, set[str]] = defaultdict(set)
    for f in feats:
        prefix_names[str(f["properties"]["code"])[:5]].add(norm(f["properties"]["name"]))
    prefix_gu: dict[str, str] = {}
    for prefix, names in sorted(prefix_names.items()):
        best_gu, best_hit = None, 0
        for gu, mois in gu_names.items():
            hit = len(names & mois)
            if hit > best_hit:
                best_gu, best_hit = gu, hit
        ratio = best_hit / max(1, len(names))
        if best_gu is None or ratio < 0.5:
            raise SystemExit(
                f"구 판별 실패: KOSTAT prefix {prefix} (overlap {best_hit}/{len(names)})"
            )
        prefix_gu[prefix] = best_gu

    matched, unmatched = 0, []
    used_codes: set[str] = set()
    for f in feats:
        p = f["properties"]
        gu = prefix_gu[str(p["code"])[:5]]
        cands = by_gu_name.get((gu, norm(p["name"])), [])
        if not cands:
            p.pop("mois_code", None)
            unmatched.append(f"{gu} {p['name']}")
            continue
        # 개편 이력 등 복수 후보: gold 관측 코드 우선 → revised_date 최신
        cands = sorted(
            cands, key=lambda c: (c[0] in gold_codes, c[1], c[0]), reverse=True
        )
        p["mois_code"] = cands[0][0]
        used_codes.add(cands[0][0])
        matched += 1

    mois_only = sorted(
        {(r["sgg_name"], r["admin_dong_name"]) for r in ref
         if r["admin_dong_code"] not in used_codes and r["admin_dong_code"] in gold_codes}
    )

    print(f"자산 폴리곤: {len(feats)} — 매칭 {matched} · 미매칭 {len(unmatched)}")
    if unmatched:
        print("  [자산에만 있음(2013 폐지·개편 추정) — 코드 미부여, 이름 매칭은 유지]")
        for item in unmatched:
            print(f"    - {item}")
    if mois_only:
        print(f"  [gold 데이터에 있으나 자산 폴리곤 없음(2013 이후 신설) — 지도 미표시 {len(mois_only)}건]")
        for gu, name in mois_only:
            print(f"    - {gu} {name}")
    dup = defaultdict(list)
    for f in feats:
        if "mois_code" in f["properties"]:
            dup[f["properties"]["mois_code"]].append(f["properties"]["name"])
    dupes = {k: v for k, v in dup.items() if len(v) > 1}
    if dupes:
        raise SystemExit(f"코드 중복 배정(버그): {dupes}")

    if args.dry_run:
        print("(dry-run — 자산 미변경)")
        return
    # 원본 포맷 보존: feature 당 한 줄 (diff 리뷰 가능성 유지)
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    body = ",\n".join(dump(f) for f in feats)
    head = dump({k: v for k, v in gj.items() if k != "features"})[1:-1]
    ASSET.write_text(
        "{" + (head + "," if head else "") + '"features":[' + body + "]}\n",
        encoding="utf-8",
    )
    print(f"✓ {ASSET.relative_to(HERE)} 갱신 완료")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
