"""온톨로지(시맨틱) 레지스트리 — 컬럼이 아니라 '의미역(role)'로 도표와 소스를 잇는다.

사상: 도표는 특정 컬럼명에 결합하지 않는다. 각 도표 타입은 "필요 슬롯"(예: 막대 = 축 1 + 측정값 1)만
선언하고, 소스의 각 필드는 이름·타입 규칙으로 role(time/geo_*/category/measure/…)을 부여받는다.
슬롯과 role 이 맞으면 어떤 소스든 어떤 도표로든 렌더 가능 — 컬럼이 바뀌면 role 재추론으로 흡수하고,
저장된 바인딩 필드가 사라지면 같은 role 의 다른 필드로 폴백한다(프론트 동작).

소스 목록은 카탈로그 스냅샷(snapshot/catalog_snapshot.json)에서 자동 파생하므로
gold 테이블이 늘거나 컬럼이 변해도 이 파일을 고칠 필요가 없다. CURATED 는 라벨·기본도표 힌트만 얹는다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).parents[2] / "snapshot" / "catalog_snapshot.json"

# ── role 어휘 ────────────────────────────────────────────────
#  time      시간축(granularity: year|month|month_of_year|date|datetime)
#  sequence  수치형 진행축(경과연차 등) — 선/막대의 x 축으로 사용 가능
#  geo_gu / geo_gu_code / geo_dong / geo_dong_code / geo_sido / geo_country
#  geo_lat / geo_lng   좌표(포인트 지도)
#  category  범주 축     measure  집계 대상 수치     id  식별자(도표 축으로 비권장)

NAME_ROLES: dict[str, tuple[str, dict]] = {
    "ym": ("time", {"granularity": "month"}),
    "y": ("time", {"granularity": "year"}),
    "cohort_y": ("time", {"granularity": "year"}),
    "month_of_year": ("time", {"granularity": "month_of_year"}),
    "observed_date": ("time", {"granularity": "date"}),
    "years_elapsed": ("sequence", {}),
    "gu": ("geo_gu", {}),
    "gu_name": ("geo_gu", {}),
    "gu_code": ("geo_gu_code", {}),
    "admin_dong": ("geo_dong", {}),
    "admin_dong_name": ("geo_dong", {}),
    "admin_dong_code": ("geo_dong_code", {}),
    "legal_code": ("geo_legal_code", {}),
    "legal_dong_name": ("geo_legal_dong", {}),
    "sido": ("geo_sido", {}),
    "sido_name": ("geo_sido", {}),
    "province": ("geo_sido", {}),
    "country": ("geo_country", {}),
    "country_name": ("geo_country", {}),
    "grid_lat": ("geo_lat", {}),
    "lat": ("geo_lat", {}),
    "latitude": ("geo_lat", {}),
    "grid_lng": ("geo_lng", {}),
    "lng": ("geo_lng", {}),
    "lon": ("geo_lng", {}),
    "longitude": ("geo_lng", {}),
}

ID_PATTERNS = re.compile(r"(_hash$|_id$|^id$|^mgtno$|^opnsfteamcode$|_uri$|_url$)")
NUMERIC_TYPES = ("bigint", "integer", "int", "smallint", "tinyint", "double", "real", "decimal", "float")

# ── 도표 타입 = 슬롯 계약 (서버가 정본, /meta 로 프론트와 공유) ──
AXIS_ROLES = ["category", "time", "sequence", "geo_gu", "geo_dong", "geo_sido", "geo_country"]
CHART_TYPES: dict[str, dict] = {
    "stat": {
        "label": "스탯 카드", "icon": "stat",
        "slots": [{"name": "value", "label": "값", "accepts": ["measure"], "required": True}],
    },
    "bar": {
        "label": "막대", "icon": "bar",
        "slots": [
            {"name": "axis", "label": "축", "accepts": AXIS_ROLES, "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
            {"name": "series", "label": "시리즈(누적)", "accepts": ["category", "time", "sequence", "geo_gu", "geo_sido"], "required": False},
        ],
        "options": {"horizontal": False, "stacked": False, "top_n": 20},
    },
    "line": {
        "label": "선", "icon": "line",
        "slots": [
            {"name": "axis", "label": "시간축", "accepts": ["time", "sequence", "category"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
            {"name": "series", "label": "시리즈", "accepts": ["category", "time", "sequence", "geo_gu", "geo_sido"], "required": False},
        ],
        "options": {"area": False, "smooth": False},
    },
    "pie": {
        "label": "원형", "icon": "pie",
        "slots": [
            {"name": "axis", "label": "분류", "accepts": ["category", "geo_gu", "geo_dong", "geo_sido"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "options": {"donut": True, "top_n": 12},
    },
    "scatter": {
        "label": "산점도", "icon": "scatter",
        "slots": [
            {"name": "x", "label": "X", "accepts": ["measure"], "required": True},
            {"name": "y", "label": "Y", "accepts": ["measure"], "required": True},
            {"name": "axis", "label": "점 단위", "accepts": AXIS_ROLES, "required": True},
        ],
        "options": {"top_n": 300},
    },
    "heatmap": {
        "label": "히트맵", "icon": "heatmap",
        "slots": [
            {"name": "x", "label": "X 축", "accepts": ["category", "time", "sequence"], "required": True},
            {"name": "y", "label": "Y 축", "accepts": ["category", "geo_gu", "geo_dong", "time"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "options": {"top_n": 30},
    },
    "table": {
        "label": "테이블", "icon": "table",
        "slots": [
            {"name": "axis", "label": "행 축", "accepts": AXIS_ROLES + ["geo_gu_code", "geo_dong_code", "id"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "options": {"top_n": 50},
    },
    "map_seoul": {
        "label": "지도 · 서울 자치구", "icon": "map",
        "slots": [
            {"name": "region", "label": "자치구", "accepts": ["geo_gu", "geo_gu_code"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "geo": "seoul_gu",
    },
    "map_seoul_dong": {
        # 자산 코드가 KOSTAT 체계라 MOIS 행정동코드와 호환 불가 → 이름 role 만 허용
        "label": "지도 · 서울 행정동", "icon": "map",
        "slots": [
            {"name": "region", "label": "행정동", "accepts": ["geo_dong"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "geo": "seoul_dong",
    },
    "map_seoul_legal": {
        "label": "지도 · 서울 법정동", "icon": "map",
        "slots": [
            {"name": "region", "label": "법정동", "accepts": ["geo_legal_code", "geo_legal_dong"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "geo": "seoul_legal_dong",
    },
    "map_korea": {
        "label": "지도 · 대한민국", "icon": "map",
        "slots": [
            {"name": "region", "label": "시도", "accepts": ["geo_sido"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "geo": "korea_sido",
    },
    "map_world": {
        "label": "지도 · 세계", "icon": "map",
        "slots": [
            {"name": "region", "label": "국가", "accepts": ["geo_country"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "geo": "world",
    },
    "map_points": {
        "label": "지도 · 좌표 밀도", "icon": "map",
        "slots": [
            {"name": "lat", "label": "위도", "accepts": ["geo_lat"], "required": True},
            {"name": "lng", "label": "경도", "accepts": ["geo_lng"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "geo": "seoul_gu",
        "options": {"top_n": 4000},
    },
}

# 값 표기 사전 — 코드값을 화면 라벨로 (원본 값은 그대로 보존, 표시만 바꾼다)
VALUE_LABELS: dict[str, dict[str, str]] = {
    "event_type": {"opened": "개업", "closed": "폐업"},
    "major": {"health": "보건위생", "culture": "문화체육", "industry": "산업경제", "environment": "환경"},
    "age_band": {"0_lt1y": "1년 미만", "1_1to3y": "1~3년", "2_3to5y": "3~5년",
                 "3_5to10y": "5~10년", "4_10to20y": "10~20년", "5_ge20y": "20년 이상"},
}

# 라벨·기본 도표 힌트만 얹는 큐레이션 — 구조(role) 자체는 자동 추론이 정본
CURATED: dict[str, dict] = {
    "gold_license_dong_summary": {
        "label": "동별 상권 요약",
        "default_chart": {"type": "map_seoul", "bindings": {"region": "gu", "value": "business_open_count"}, "agg": "sum"},
    },
    "gold_license_flow_monthly": {
        "label": "월별 개·폐업 흐름",
        "default_chart": {"type": "line", "bindings": {"axis": "ym", "value": "cnt", "series": "event_type"}, "agg": "sum"},
    },
    "gold_license_flow_yearly": {"label": "연별 개·폐업 흐름"},
    "gold_license_flow_daily": {"label": "일별 개·폐업 흐름"},
    "gold_license_dong_category_matrix": {
        "label": "동 × 업종 매트릭스",
        "default_chart": {"type": "pie", "bindings": {"axis": "major", "value": "active_cnt"}, "agg": "sum"},
    },
    "gold_license_gu_specialization": {
        "label": "자치구 특화 업종(LQ)",
        "default_chart": {"type": "bar", "bindings": {"axis": "gu", "value": "lq"}, "agg": "max"},
    },
    "gold_license_seasonality": {
        "label": "개·폐업 계절성",
        "default_chart": {"type": "heatmap", "bindings": {"x": "month_of_year", "y": "category", "value": "cnt"}, "agg": "sum"},
    },
    "gold_license_cohort_survival": {
        "label": "창업 코호트 생존율",
        "default_chart": {"type": "line", "bindings": {"axis": "years_elapsed", "value": "survival_rate", "series": "cohort_y"}, "agg": "avg"},
    },
    "gold_license_lifespan": {
        "label": "업종 수명 분포",
        "default_chart": {"type": "bar", "bindings": {"axis": "category", "value": "early_close_ratio"}, "agg": "avg"},
    },
    "gold_license_stock_age_band": {
        "label": "업력 밴드(신상 vs 노포)",
        "default_chart": {"type": "bar", "bindings": {"axis": "age_band", "value": "active_cnt", "series": "major"}, "agg": "sum"},
    },
    "gold_license_geo_grid": {
        "label": "상권 밀집 격자(500m)",
        "default_chart": {"type": "map_points", "bindings": {"lat": "grid_lat", "lng": "grid_lng", "value": "active_cnt"}, "agg": "sum"},
    },
    "gold_license_churn_yearly": {
        "label": "연별 교체율·신생비",
        "default_chart": {"type": "line", "bindings": {"axis": "y", "value": "opened", "series": "event_type"}, "agg": "sum"},
    },
    "gold_license_status_transition": {"label": "영업상태 전이"},
    "gold_license_status_duration": {"label": "상태 체류 기간"},
    "gold_license_change_activity": {"label": "변경 이벤트 활동"},
    "gold_license_data_quality": {"label": "데이터 품질 요약"},
    "gold_license_multi_site": {"label": "다점포 운영"},
    "gold_license_address_succession": {"label": "주소 승계(자리 대물림)"},
    "gold_license_phone_succession": {"label": "전화번호 승계"},
    "gold_detail_area_profile": {"label": "면적 프로파일"},
    "gold_detail_uptae_mix": {"label": "업태 구성"},
    "gold_env_facility_operation": {"label": "환경시설 가동 현황"},
}


def _label_from_desc(name: str, desc: str) -> str:
    """컬럼 설명 첫 구절을 짧은 한글 라벨로. 없으면 컬럼명 그대로."""
    if not desc:
        return name
    head = re.split(r"[.—]", desc, maxsplit=1)[0].strip()
    return head[:28] if head else name


def infer_role(name: str, sql_type: str) -> tuple[str, dict]:
    lowered = name.lower()
    if lowered in NAME_ROLES:
        return NAME_ROLES[lowered]
    if ID_PATTERNS.search(lowered):
        return "id", {}
    base = sql_type.split("(")[0].lower()
    if base in ("date",):
        return "time", {"granularity": "date"}
    if base.startswith("timestamp"):
        return "time", {"granularity": "datetime"}
    if base in NUMERIC_TYPES:
        return "measure", {}
    return "category", {}


def _supports(roles: set[str]) -> list[str]:
    out = []
    for ctype, spec in CHART_TYPES.items():
        ok = True
        for slot in spec["slots"]:
            if slot["required"] and not (roles & set(slot["accepts"])):
                ok = False
                break
        if ok:
            out.append(ctype)
    return out


class Registry:
    """스냅샷 → 소스/필드/role. 스냅샷 파일이 갱신되면 자동 재적재(mtime 감시)."""

    def __init__(self, snapshot_path: Path = SNAPSHOT_PATH):
        self._path = snapshot_path
        self._mtime: float | None = None
        self._sources: dict[str, dict] = {}
        self._generated_at: str = ""

    def _build(self) -> None:
        snap = json.loads(self._path.read_text(encoding="utf-8"))
        self._generated_at = snap.get("generated_at", "")
        sources: dict[str, dict] = {}
        for t in snap["tables"]:
            curated = CURATED.get(t["name"], {})
            fields = []
            for c in t["columns"]:
                role, extra = infer_role(c["name"], c.get("type", ""))
                fields.append({
                    "name": c["name"],
                    "type": c.get("type", ""),
                    "role": role,
                    "label": _label_from_desc(c["name"], c.get("description", "")),
                    "desc": c.get("description", ""),
                    **extra,
                })
            roles = {f["role"] for f in fields}
            sources[t["name"]] = {
                "name": t["name"],
                "domain": t.get("domain", ""),
                "relation": t["relation"],
                "label": curated.get("label", _label_from_desc(t["name"], t.get("description", ""))),
                "description": t.get("description", ""),
                "row_count": t.get("row_count", 0),
                "date_range": t.get("date_range"),
                "fields": fields,
                "supports": _supports(roles),
                "default_chart": curated.get("default_chart"),
            }
        self._sources = sources

    def _fresh(self) -> None:
        mtime = self._path.stat().st_mtime
        if mtime != self._mtime:
            self._build()
            self._mtime = mtime

    @property
    def generated_at(self) -> str:
        self._fresh()
        return self._generated_at

    def sources(self, domain: str | None = None) -> list[dict]:
        self._fresh()
        out = list(self._sources.values())
        if domain and domain != "all":
            out = [s for s in out if s["domain"] == domain]
        return out

    def get(self, name: str) -> dict | None:
        self._fresh()
        return self._sources.get(name)

    def meta(self) -> dict:
        self._fresh()
        domains: dict[str, int] = {}
        for s in self._sources.values():
            domains[s["domain"]] = domains.get(s["domain"], 0) + 1
        return {
            "generated_at": self._generated_at,
            "chart_types": CHART_TYPES,
            "value_labels": VALUE_LABELS,
            "domains": domains,
        }


registry = Registry()
