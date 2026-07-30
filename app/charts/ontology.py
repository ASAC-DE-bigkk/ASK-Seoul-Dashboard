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
from copy import deepcopy
from pathlib import Path
from typing import Any

from .querybuilder import allowed_filter_ops

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
    "hr": ("sequence", {}),     # 시각(0~23) — citydata forecast/anomaly 의 시간대 진행축
    "hour": ("sequence", {}),
    "dow": ("sequence", {}),    # 요일(1~7) — citydata demographics 의 요일 진행축
    "gu": ("geo_gu", {}),
    "gu_name": ("geo_gu", {}),
    "gu_code": ("geo_gu_code", {}),
    "admin_dong": ("geo_dong", {}),
    "admin_dong_name": ("geo_dong", {}),
    "admin_dong_code": ("geo_dong_code", {}),
    "legal_code": ("geo_legal_code", {}),
    "legal_dong": ("geo_legal_dong", {}),
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
    # 숫자지만 합산할 측정값이 아닌 구조 축/좌표
    "hr": ("sequence", {"granularity": "hour_of_day"}),
    "hour": ("sequence", {"granularity": "hour_of_day"}),
    "dow": ("sequence", {"granularity": "day_of_week"}),
    "rank": ("ordinal", {}),
    "rank_no": ("ordinal", {}),
    "nx": ("id", {}),
    "ny": ("id", {}),
    "precip_type": ("category", {}),
}

ID_PATTERNS = re.compile(r"(_hash$|_id$|^id$|^mgtno$|^opnsfteamcode$|_uri$|_url$)")
# 저카디널리티 개방(실측 기반 자율성): 스냅샷 approx_distinct 가 이 값 이하인 measure 는
# 값(집계 대상)이면서 **축(groupby)으로도** 쓸 수 있다 — 등급·층수·요일형 숫자 컬럼이
# '숫자형=측정값' 규칙에 갇혀 축이 되지 못하던 한계를 실측으로 푼다(선언 아님).
GROUPABLE_MAX_DISTINCT = 50
NUMERIC_TYPES = ("bigint", "integer", "int", "smallint", "tinyint", "double", "real", "decimal", "float")
TIME_DATE_PATTERN = re.compile(r"(^date$|_date$)")
TIME_AT_PATTERN = re.compile(r"(^time_bucket$|_at$)")
STRUCTURAL_ORDINAL_PATTERN = re.compile(r"(^rank_|_rank$)")
CODE_PATTERN = re.compile(r"(_code$|_cd$|^is_|^has_|_flag$)")
NON_ADDITIVE_PATTERN = re.compile(
    r"(rate|ratio|share|pct|percent|average|avg|mean|median|p50|p90|"
    r"\blq\b|score|index|temperature|^temp_|humidity|wind|speed|travel_time|"
    r"duration|operating_days|operating_hours|gap_days|_days$|_hours$|"
    r"rank|z_score|std|occupancy|congestion|pm10|pm25|fine_dust|air_quality|"
    r"precip_prob|distance|coverage|_idx($|_)|_per_)"
)
MAX_VALUE_PATTERN = re.compile(r"(^max_|_max($|_)|_peak($|_)|^peak_|^tmx($|_))")
MIN_VALUE_PATTERN = re.compile(r"(^min_|_min($|_)|^tmn($|_))")
DURATION_UNIT_PATTERN = re.compile(r"(duration|latency|elapsed)_min$")
# 재고(stock)성 측정값 — 어느 한 시점의 '수위'라 **시간축과 함께 합산하면 이중계산**된다
# (Kimball semi-additive). 공간·범주로는 합산해도 옳다: 구별 active_cnt 합 = 서울 active_cnt.
# 좁고 확실한 이름만 넣는다 — 넓히면 진짜 flow(시간 합산이 옳은 건수)를 오분류해 정상 차트를
# 막는다. 실측(112 소스): 29개 이름만 잡고 **시드 차트 0건**을 깨뜨린다.
# 주의: cumulative_safe 는 race 차트 누적 옵션 전용 allowlist(4개 flow 테이블)라 가산성
# 모델이 아니다 — 그것으로 semi-additive 를 판정하면 진짜 flow 가 대량 오분류된다.
STOCK_MEASURE_PATTERN = re.compile(
    r"(^stock_|_stock($|_)|^active_cnt$|_active_cnt$|_active$|^active_rows$|"
    r"^total_cnt$|^seat_count$|capacity|^occ_|occupancy|inventory|balance|"
    r"(^|_)ppltn$|^business_open_count$|^business_count$|^license_total$|"
    r"_lot_cnt$|_available$|^available_count$|^total_chargers$|"
    r"_rack_total($|_)|_parking_total($|_)|_spot_cnt$|_station_cnt$)"
)
# 가산 가능한 축(dimension) 종류 — semi-additive 는 시간축만 빠진다.
ADDITIVE_OVER_ALL = ["time", "sequence", "category", "geo"]
ADDITIVE_OVER_STOCK = ["category", "geo"]
CUMULATIVE_SAFE_FIELDS: dict[str, set[str]] = {
    "gold_license_churn_yearly": {"opened", "closed", "net_change"},
    "gold_license_flow_daily": {"cnt"},
    "gold_license_flow_monthly": {"cnt"},
    "gold_license_flow_yearly": {"cnt"},
}
TECHNICAL_TIME_PATTERN = re.compile(
    r"(revision|published|collected|issued|refreshed|created|updated|load)"
)
ANALYSIS_TIME_PATTERN = re.compile(
    r"(event|forecast|hour|time_bucket|snapshot|game|boxoffice)"
)
UNSAFE_GENERIC_MEASURE_PATTERN = re.compile(r"(wind_dir|bearing|azimuth)")
# category(TMP/REH/POP/...)에 따라 단위가 바뀌는 long-form 값. 현재 차트 계약은
# 필수 category 고정을 표현하지 못하므로 wide Gold를 쓰고 이 필드는 슬롯에서 제외한다.
MIXED_UNIT_MEASURE_FIELDS = {
    "fcst_value_num",
    "value_num",
    "value_lower_bound",
    "value_upper_bound",
}

DOMAIN_LABELS: dict[str, str] = {
    "all": "전체",
    "culture": "문화",
    "commerce": "상권",
    "traffic": "교통",
    "weather": "날씨",
    "citydata": "도시데이터",
    "transit": "대중교통",
}

# ── 식별↔표시 동반 컬럼 (코드로 세고, 한글로 보여준다) ────────────
# 원칙(#geo-identity): 같은 소스에 코드/이름(또는 en/ko) 쌍이 공존하면 **식별·집계는
# 코드**가 정본이고 이름은 표시 전용이다 — 동명이인 지역(신사동: 강남구·관악구)이 이름
# 그룹핑에서 하나로 합산되는 것을 막는다. 이 규칙은 세 곳이 나눠 진다:
#   extract.py  — 쌍의 distinct 값을 실측해 스냅샷 code_labels 로 박제
#   Registry    — 필드에 id_field(표시→식별)/label_field(식별→표시) 메타 부여,
#                 meta.value_labels 로 코드→한글 사전 서빙(중복 동명은 '신사동·강남구')
#   프론트 app.js — 쿼리 시 이름 바인딩을 id_field 로 승격(GROUP BY = 코드),
#                 render.js vlabel 이 코드를 한글로 표기

# MOIS(행안부) 서울 자치구 코드 — geo.js 의 MOIS_GU 와 동일 사전(수정 시 양쪽 동기).
MOIS_GU: dict[str, str] = {
    "11110": "종로구", "11140": "중구", "11170": "용산구", "11200": "성동구", "11215": "광진구",
    "11230": "동대문구", "11260": "중랑구", "11290": "성북구", "11305": "강북구", "11320": "도봉구",
    "11350": "노원구", "11380": "은평구", "11410": "서대문구", "11440": "마포구", "11470": "양천구",
    "11500": "강서구", "11530": "구로구", "11545": "금천구", "11560": "영등포구", "11590": "동작구",
    "11620": "관악구", "11650": "서초구", "11680": "강남구", "11710": "송파구", "11740": "강동구",
}


def companion_pairs(column_names) -> list[tuple[str, str]]:
    """(식별 컬럼, 표시 컬럼) 쌍 — 이름 규칙만으로 판정한다(테이블 하드코딩 없음).

    - `X_code` ↔ 첫 공존 후보 `X` / `X_name` / `X_dong` / `X_dong_name`
      (admin_dong_code↔admin_dong, gu_code↔gu, legal_code↔legal_dong)
    - `X` ↔ `X_ko` (major↔major_ko, category↔category_ko, dataset↔dataset_ko)
    """
    cols = set(column_names)
    pairs: list[tuple[str, str]] = []
    for col in cols:
        if col.endswith("_code"):
            base = col[: -len("_code")]
            for cand in (base, f"{base}_name", f"{base}_dong", f"{base}_dong_name"):
                if cand in cols and cand != col:
                    pairs.append((col, cand))
                    break
        elif f"{col}_ko" in cols:
            pairs.append((col, f"{col}_ko"))
    return sorted(pairs)


def _disambiguated_labels(raw: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """코드→표시 사전에서 중복 표시명을 구명 접미사로 유일화한다.

    geo.js 자산 표시(`신사동·강남구`)와 같은 규칙: 같은 필드 안에서 표시명이 코드
    여러 개에 걸치고 코드 앞 5자리가 MOIS 구코드면 `이름·구명`. 구를 못 찾으면
    원명 유지(이중 표기보다 정직한 중복).
    """
    out: dict[str, dict[str, str]] = {}
    for field, mapping in raw.items():
        counts: dict[str, int] = {}
        for label in mapping.values():
            counts[label] = counts.get(label, 0) + 1
        resolved = {}
        for code, label in mapping.items():
            gu = MOIS_GU.get(str(code)[:5])
            resolved[code] = f"{label}·{gu}" if counts[label] > 1 and gu else label
        out[field] = resolved
    return out

# ── 도표 타입 = 슬롯 계약 (서버가 정본, /meta 로 프론트와 공유) ──
AXIS_ROLES = ["category", "time", "sequence", "ordinal", "geo_gu", "geo_dong", "geo_sido", "geo_country"]
CHART_TYPES: dict[str, dict] = {
    "stat": {
        "label": "스탯 카드", "icon": "stat",
        "slots": [{"name": "value", "label": "값", "accepts": ["measure"], "required": True}],
    },
    "bar": {
        "label": "막대", "icon": "bar",
        "slots": [
            # binnable: 숫자 measure 를 구간 폭(bins[슬롯])과 함께 축으로 허용 — 히스토그램형
            {"name": "axis", "label": "축", "accepts": AXIS_ROLES, "required": True, "binnable": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
            {"name": "series", "label": "시리즈(누적)", "accepts": ["category", "time", "sequence", "geo_gu", "geo_sido"], "required": False},
        ],
        "options": {"horizontal": False, "stacked": False, "top_n": 20},
        "agg_constraints": [
            {"when": {"stacked": True}, "allowed": ["sum", "count"]},
        ],
    },
    "line": {
        "label": "선", "icon": "line",
        "slots": [
            {"name": "axis", "label": "시간축", "accepts": ["time", "sequence"], "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
            {"name": "series", "label": "시리즈", "accepts": ["category", "time", "sequence", "geo_gu", "geo_sido"], "required": False},
        ],
        "options": {"area": False, "smooth": False},
    },
    "race": {
        # 타임랩스 — 시간 프레임을 영상처럼 재생하며 축 항목들의 순위 변화를 보여준다
        "label": "타임랩스 경주", "icon": "race",
        "slots": [
            {"name": "time", "label": "시간축", "accepts": ["time", "sequence"], "required": True},
            {"name": "axis", "label": "경주 축",
             "accepts": ["category", "geo_gu", "geo_gu_code", "geo_dong",
                         "geo_dong_code", "geo_sido", "geo_country"],
             "required": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "options": {"cumulative": False, "top_n": 12, "interval_ms": 800},
        "agg_constraints": [
            {"when": {"cumulative": True}, "allowed": ["sum", "count"]},
        ],
    },
    "pie": {
        "label": "원형", "icon": "pie",
        "slots": [
            {"name": "axis", "label": "분류", "accepts": ["category", "geo_gu", "geo_dong", "geo_sido"], "required": True, "binnable": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "options": {"donut": True, "top_n": 12},
        "aggs": ["sum", "count"],
    },
    "scatter": {
        "label": "산점도", "icon": "scatter",
        "slots": [
            {"name": "x", "label": "X", "accepts": ["measure"], "required": True},
            {"name": "y", "label": "Y", "accepts": ["measure"], "required": True},
            {"name": "axis", "label": "점 단위", "accepts": AXIS_ROLES, "required": True},
        ],
        "options": {"top_n": 300},
        "aggs": ["sum", "avg", "min", "max"],
    },
    "heatmap": {
        "label": "히트맵", "icon": "heatmap",
        "slots": [
            {"name": "x", "label": "X 축", "accepts": ["category", "time", "sequence", "ordinal"], "required": True, "binnable": True},
            {"name": "y", "label": "Y 축",
             "accepts": ["category", "geo_gu", "geo_gu_code", "geo_dong",
                         "geo_dong_code", "time"],
             "required": True, "binnable": True},
            {"name": "value", "label": "값", "accepts": ["measure"], "required": True},
        ],
        "options": {"top_n": 30},
    },
    "table": {
        "label": "테이블", "icon": "table",
        "slots": [
            {"name": "axis", "label": "행 축", "accepts": AXIS_ROLES + ["geo_gu_code", "geo_dong_code", "id"], "required": True, "binnable": True},
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
        # 자산 원 code 는 KOSTAT 체계지만 scripts/assign_mois_codes.py 가 각 폴리곤에
        # MOIS 행정동코드(mois_code)를 병기 → 코드 role 매칭 지원. 이름 바인딩도 프론트가
        # id_field 승격으로 코드 집계한다(동명 신사동 강남·관악 분리).
        "label": "지도 · 서울 행정동", "icon": "map",
        "slots": [
            {"name": "region", "label": "행정동", "accepts": ["geo_dong", "geo_dong_code"], "required": True},
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

# count(*)는 물리 measure 컬럼이 없어도 유효한 파생 측정값이다. 값 슬롯을 가진
# 도표는 count 집계에서만 해당 슬롯을 생략할 수 있다(산점도 X/Y에는 적용되지 않음).
for _chart_spec in CHART_TYPES.values():
    for _slot in _chart_spec["slots"]:
        if _slot["name"] == "value":
            _slot["count_optional"] = True

# 값 표기 사전(정적 큐레이션) — 코드값을 화면 라벨로 (원본 값은 보존, 표시만 바꾼다).
# 스냅샷 code_labels(데이터 실측)가 기본 사전이고, 여기 항목이 같은 키를 덮어쓴다.
VALUE_LABELS: dict[str, dict[str, str]] = {
    "event_type": {"opened": "개업", "closed": "폐업"},
    "major": {"health": "보건위생", "culture": "문화체육", "industry": "산업경제", "environment": "환경"},
    "age_band": {"0_lt1y": "1년 미만", "1_1to3y": "1~3년", "2_3to5y": "3~5년",
                 "3_5to10y": "5~10년", "4_10to20y": "10~20년", "5_ge20y": "20년 이상"},
    # 동 매핑 불가(마스킹 주소) 코드 — dong_category_matrix 의 coalesce('UNK')
    "admin_dong_code": {"UNK": "미상"},
    # 자치구 코드는 MOIS 표준 체계라 **큐레이션이 실측보다 정본**이다 — 일부 gold 의
    # 실측 code_labels 가 어긋나 있어(11680 을 '중구'로 표기하는 등) 그대로 두면
    # 지도·축 표기가 잘못된 구 이름으로 나간다(SHARE §7.2 잘못된 지역 매칭 = Critical).
    "gu_code": {**MOIS_GU, "UNK": "미상"},
}

# 라벨·기본 도표 힌트만 얹는 큐레이션 — 구조(role) 자체는 자동 추론이 정본
CURATED: dict[str, dict] = {
    "gold_culture_activity_by_dong": {
        "default_chart": {
            "type": "line",
            "bindings": {"axis": "event_date", "value": "activities_count"},
            "agg": "sum",
        },
    },
    "gold_traffic_incident_current_by_admin_dong_hourly": {
        "default_chart": {
            "type": "bar",
            "bindings": {"axis": "admin_dong", "value": "incident_count"},
            "agg": "sum",
        },
    },
    "gold_weather_daily_by_admin_dong": {
        "default_chart": {
            "type": "line",
            "bindings": {"axis": "forecast_date", "value": "temp_avg_c"},
            "agg": "avg",
        },
    },
    "gold_citydata_ppltn_by_time": {
        "default_chart": {
            "type": "line",
            "bindings": {"axis": "event_at", "value": "avg_ppltn"},
            "agg": "avg",
        },
    },
    "gold_transit_dong_hourly": {
        "default_chart": {
            "type": "table",
            "bindings": {"axis": "admin_dong_code", "value": "subway_arrival_cnt"},
            "agg": "sum",
        },
    },
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
        "default_chart": {"type": "line", "bindings": {"axis": "y", "value": "opened"}, "agg": "sum"},
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
    # 스냅샷 물리 타입이 varchar여도 이름이 명시적인 날짜/시각이면 시간축이다.
    if TIME_DATE_PATTERN.search(lowered):
        return "time", {"granularity": "date"}
    if TIME_AT_PATTERN.search(lowered):
        return "time", {"granularity": "datetime"}
    if STRUCTURAL_ORDINAL_PATTERN.search(lowered):
        return "ordinal", {}
    # 숫자형 코드/불리언 표지는 합계를 내면 의미가 바뀌므로 범주로 분류한다.
    if CODE_PATTERN.search(lowered):
        return "category", {}
    base = sql_type.split("(")[0].lower()
    if base in ("date",):
        return "time", {"granularity": "date"}
    if base.startswith("timestamp"):
        return "time", {"granularity": "datetime"}
    if base in NUMERIC_TYPES:
        return "measure", {}
    return "category", {}


def _measure_semantics(source_name: str, name: str, role: str) -> dict[str, Any]:
    """측정값의 기본 집계와 가산성을 보수적으로 표시한다.

    count/amount 계열은 합계를 허용하되, 비율·온도·속도·순위·기간 같은 값은
    합계 선택지에서 제외한다. 이는 추천용 힌트인 동시에 querybuilder의 안전 계약이다.
    """
    if role != "measure":
        return {}
    lowered = name.lower()
    preferred = "sum"
    additive = True
    if DURATION_UNIT_PATTERN.search(lowered):
        preferred, additive = "avg", False
    elif MAX_VALUE_PATTERN.search(lowered):
        preferred, additive = "max", False
    elif MIN_VALUE_PATTERN.search(lowered):
        preferred, additive = "min", False
    elif NON_ADDITIVE_PATTERN.search(lowered):
        preferred, additive = "avg", False
    allowed = ["sum", "avg", "min", "max", "count", "count_distinct"]
    if not additive:
        allowed.remove("sum")
    stock = additive and bool(STOCK_MEASURE_PATTERN.search(lowered))
    if stock:
        # 재고성은 sum 을 **허용**하되(구별 합계는 옳다) 기본 제안은 avg 로 둔다 —
        # 자동 추천·자동 바인딩은 축을 가리지 않고 preferred_agg 를 집으므로, sum 을 기본으로
        # 두면 시간축과 짝지어졌을 때 빌더가 거부하는 조합을 온톨로지 스스로 추천하게 된다.
        preferred = "avg"
    return {
        "preferred_agg": preferred,
        "additive": additive,
        "additive_over": list(
            ADDITIVE_OVER_STOCK if stock else ADDITIVE_OVER_ALL if additive else []
        ),
        "allowed_aggs": allowed,
        "cumulative_safe": bool(
            additive and name in CUMULATIVE_SAFE_FIELDS.get(source_name, set())
        ),
    }


def _recommendation_priority(name: str, role: str) -> int:
    """낮을수록 자동 추천에서 우선한다. 기술 메타데이터는 뒤로 보낸다."""
    lowered = name.lower()
    if role == "time":
        if TECHNICAL_TIME_PATTERN.search(lowered):
            return 90
        if ANALYSIS_TIME_PATTERN.search(lowered):
            return 10
        return 30
    if role == "measure":
        if re.search(
            r"(raw_object|audited|unmapped|max_page|coverage|base_n|sample_count|"
            r"transform|dag_run|grid_distance|source_coordinate)",
            lowered,
        ):
            return 90
        if re.search(r"(average|avg|mean)", lowered):
            return 10
        if re.search(r"(count|cnt|total|amount|amt)", lowered):
            return 20
        return 30
    return 40


def _is_chartable(name: str, role: str) -> bool:
    """슬롯에 직접 놓아도 단위와 분석 의미가 고정되는 필드만 허용한다."""
    lowered = name.lower()
    if lowered in MIXED_UNIT_MEASURE_FIELDS:
        return False
    # 방향각은 359°와 1°의 산술평균이 180°가 되는 원형 데이터라 일반 집계가 안전하지 않다.
    if role == "measure" and UNSAFE_GENERIC_MEASURE_PATTERN.search(lowered):
        return False
    # 적재/수집 메타시각은 필터에는 쓸 수 있지만 분석 시간축으로 노출하지 않는다.
    if role == "time" and TECHNICAL_TIME_PATTERN.search(lowered):
        return False
    return True


def field_matches_slot(field: dict, slot: dict) -> bool:
    """슬롯 배정 규칙의 단일 정본 — role 일치 또는 저카디널리티 groupable 개방.

    groupable(실측 distinct ≤ GROUPABLE_MAX_DISTINCT 인 measure)은 category 를 받는
    슬롯에 한해 축으로 허용한다. 서버 검증(router)과 추천(compatible_bindings),
    프론트 후보 목록이 모두 이 규칙 하나를 공유해야 계약이 갈라지지 않는다.
    """
    if field["role"] in slot["accepts"]:
        return True
    return bool(field.get("groupable")) and "category" in slot["accepts"]


def compatible_bindings(
    fields: list[dict], chart_spec: dict, *, count_mode: bool = False
) -> dict[str, str] | None:
    """필수 슬롯마다 서로 다른 실재 필드를 배정할 수 있을 때 한 조합을 반환한다."""
    required = [
        slot
        for slot in chart_spec["slots"]
        if slot.get("required")
        and not (count_mode and slot.get("count_optional"))
    ]
    candidates = {
        slot["name"]: [
            field["name"]
            for field in fields
            if field_matches_slot(field, slot) and field.get("chartable", True)
        ]
        for slot in required
    }
    # 후보가 적은 슬롯부터 풀면 scatter/heatmap 같은 중복 role 계약도 빠르게 판정된다.
    ordered = sorted(required, key=lambda slot: len(candidates[slot["name"]]))

    def bind(index: int, used: set[str], result: dict[str, str]) -> dict[str, str] | None:
        if index == len(ordered):
            return result
        slot_name = ordered[index]["name"]
        for field_name in candidates[slot_name]:
            if field_name in used:
                continue
            found = bind(
                index + 1,
                used | {field_name},
                {**result, slot_name: field_name},
            )
            if found is not None:
                return found
        return None

    return bind(0, set(), {})


def _supports(
    fields: list[dict], *, row_count: int = 0, date_range: dict | None = None
) -> list[str]:
    supported = [
        chart_type
        for chart_type, spec in CHART_TYPES.items()
        if compatible_bindings(fields, spec) is not None
        or compatible_bindings(fields, spec, count_mode=True) is not None
    ]
    has_sequence = any(
        field.get("chartable", True) and field["role"] == "sequence"
        for field in fields
    )
    one_time_point = bool(
        date_range
        and date_range.get("min") is not None
        and date_range.get("min") == date_range.get("max")
    )
    if row_count < 2:
        supported = [
            chart_type
            for chart_type in supported
            if chart_type not in {"bar", "line", "area", "pie", "scatter", "heatmap", "race"}
        ]
    elif one_time_point and not has_sequence:
        supported = [
            chart_type
            for chart_type in supported
            if chart_type not in {"line", "area", "race"}
        ]
    return supported


class Registry:
    """스냅샷 → 소스/필드/role. 스냅샷 파일이 갱신되면 자동 재적재(mtime 감시)."""

    def __init__(self, snapshot_path: Path = SNAPSHOT_PATH):
        self._path = snapshot_path
        self._mtime: float | None = None
        self._sources: dict[str, dict] = {}
        self._generated_at: str = ""
        self._code_labels: dict[str, dict[str, str]] = {}
        self._source_code_labels: dict[str, dict[str, dict[str, str]]] = {}

    def _build(self) -> None:
        snap = json.loads(self._path.read_text(encoding="utf-8"))
        self._generated_at = snap.get("generated_at", "")
        # 스냅샷 출처·신선도(전역) — 소비자가 '얼마나 최신인지·통계가 완전한지'를 알고 답하도록.
        # refresh.mode=partial_basic 이면 상당수 컬럼에 통계가 없다(추정·구간 제안이 보수적).
        self._provenance = {
            "generated_at": self._generated_at,
            "refresh_mode": (snap.get("refresh") or {}).get("mode"),
            "observed_at": (snap.get("refresh") or {}).get("observed_at"),
            "source_system": (snap.get("refresh") or {}).get("source_system"),
            "domain_generated_at": snap.get("domain_generated_at") or {},
        }
        # 스냅샷 실측 코드→표시 사전. **소스별로 따로 보관**하는 것이 정본이다 —
        # 필드명(gu_code·admin_dong_code·category…)은 소스마다 다른 코드 체계를 담을 수 있어
        # 전역으로 병합하면 마지막에 읽힌 테이블이 다른 소스의 라벨을 덮어쓴다(실측: 같은
        # 필드·같은 코드에 서로 다른 라벨이 붙는 충돌 210건 — 11680 이 '강남구'가 아니라
        # '중구'로 표기되는 등 잘못된 지역 표기가 생겼다).
        raw_labels: dict[str, dict[str, str]] = {}
        per_source: dict[str, dict[str, dict[str, str]]] = {}
        for t in snap["tables"]:
            for field, mapping in (t.get("code_labels") or {}).items():
                clean = {str(k): str(v) for k, v in mapping.items()}
                per_source.setdefault(t["name"], {})[field] = clean
                # 전역 병합본은 /api/v1/charts/meta 하위호환 폴백으로만 남긴다.
                raw_labels.setdefault(field, {}).update(clean)
        self._code_labels = _disambiguated_labels(raw_labels)
        self._source_code_labels = {
            name: _disambiguated_labels(mapping) for name, mapping in per_source.items()
        }
        sources: dict[str, dict] = {}
        for t in snap["tables"]:
            # 승격 게이트는 '실측' 사전만 본다 — 정적 VALUE_LABELS(UNK 등 부분 사전)만으로
            # 승격을 켜면 라벨 없는 코드가 축에 생으로 노출된다(스냅샷 퇴화 시 안전장치).
            # **이 소스의** 사전만 본다: 옆 소스에 라벨이 있다고 승격하면 이 소스의 축이
            # 라벨 없는 생코드로 노출된다.
            labeled = set(self._source_code_labels.get(t["name"], {}))
            curated = CURATED.get(t["name"], {})
            fields = []
            for c in t["columns"]:
                role, extra = infer_role(c["name"], c.get("type", ""))
                distinct = c.get("distinct_count")
                fields.append({
                    "name": c["name"],
                    "type": c.get("type", ""),
                    "role": role,
                    "label": _label_from_desc(c["name"], c.get("description", "")),
                    "desc": c.get("description", ""),
                    **extra,
                    **_measure_semantics(t["name"], c["name"], role),
                    "recommendation_priority": _recommendation_priority(c["name"], role),
                    "chartable": _is_chartable(c["name"], role),
                    # granularity(time)·role 을 함께 넘긴다 — last_n 등 op 목록이 필드
                    # 메타에 의존한다(부분 dict 를 넘기면 조용히 누락된다)
                    "allowed_filter_ops": allowed_filter_ops({"role": role, **extra}),
                    # 실측 통계(extract) — 저카디널리티 groupby 개방·구간화 기본 폭 제안의 근거
                    **({"distinct_count": int(distinct)} if distinct is not None else {}),
                    **({"min": c["min"]} if c.get("min") is not None else {}),
                    **({"max": c["max"]} if c.get("max") is not None else {}),
                    "groupable": bool(
                        role == "measure" and distinct is not None
                        and 0 < int(distinct) <= GROUPABLE_MAX_DISTINCT
                    ),
                })
            # 동반 필드 메타 — 표시 필드에 id_field(집계 식별을 코드로 승격), 식별 필드에
            # label_field. 승격은 코드 라벨 사전이 있을 때만 허용(축이 생코드로 노출 방지).
            by_name = {f["name"]: f for f in fields}
            for ident, disp in companion_pairs(by_name):
                ident_f, disp_f = by_name[ident], by_name[disp]
                if "measure" in (ident_f["role"], disp_f["role"]):
                    continue  # 수치 측정값 쌍은 표시/식별 관계가 아니다
                ident_f["label_field"] = disp
                if ident in labeled:
                    disp_f["id_field"] = ident
            sources[t["name"]] = {
                "name": t["name"],
                "domain": t.get("domain", ""),
                # 다중 백엔드(2026-07-23): 소스가 사는 DB — querybuilder 방언·실행 라우팅의 근거.
                # 미지정(기존 gold 스냅샷)은 trino (하위호환 기본값).
                "datasource": t.get("datasource", "trino"),
                "backend": t.get("backend", "trino"),
                "object_type": t.get("object_type", "table"),
                "relation": t["relation"],
                "label": curated.get("label", _label_from_desc(t["name"], t.get("description", ""))),
                "description": t.get("description", ""),
                "row_count": t.get("row_count", 0),
                "date_range": t.get("date_range"),
                "fields": fields,
                "supports": _supports(
                    fields,
                    row_count=t.get("row_count", 0),
                    date_range=t.get("date_range"),
                ),
                "default_chart": curated.get("default_chart"),
                # 계보·품질·계약 메타(스냅샷 원본에 있으나 지금까지 버려졌다). lineage 는
                # 33/112 에만 있으므로 '의존성 없음'과 '수집 안 됨'을 구분해 실어 보낸다.
                "materialized": t.get("materialized"),
                "contract_enforced": bool(t.get("contract_enforced")),
                "lineage": t.get("lineage") or None,
                "quality": t.get("quality") or None,
                "tags": t.get("tags") or [],
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

    @property
    def provenance(self) -> dict:
        """스냅샷 출처·신선도 — 소비자가 낡음/부분통계를 알고 답하게 한다."""
        self._fresh()
        return dict(self._provenance)

    def sources(self, domain: str | None = None) -> list[dict]:
        self._fresh()
        out = list(self._sources.values())
        if domain and domain != "all":
            out = [s for s in out if s["domain"] == domain]
        return out

    def get(self, name: str) -> dict | None:
        self._fresh()
        return self._sources.get(name)

    def value_labels_for(self, source_name: str,
                         overrides: dict | None = None) -> dict[str, dict[str, str]]:
        """이 소스의 코드→표시 사전(정본). 전역 병합본이 아니라 **소스 자신의 실측 사전**을
        기본층으로 쓰고, 그 위에 정적 큐레이션(VALUE_LABELS)·사용자 오버라이드를 얹는다.

        같은 필드명이라도 소스마다 코드 체계가 다를 수 있어(gu_code·admin_dong_code·category)
        전역 병합본을 쓰면 다른 소스의 라벨이 새어 들어온다.
        """
        self._fresh()
        source = self._sources.get(source_name)
        field_names = {f["name"] for f in source["fields"]} if source else set()
        labels: dict[str, dict[str, str]] = {
            field: dict(mapping)
            for field, mapping in self._source_code_labels.get(source_name, {}).items()
            if not field_names or field in field_names
        }
        for field, mapping in VALUE_LABELS.items():
            if field_names and field not in field_names:
                continue
            labels.setdefault(field, {}).update(deepcopy(mapping))
        for field, mapping in (overrides or {}).items():
            if isinstance(mapping, dict) and (not field_names or field in field_names):
                labels.setdefault(field, {}).update(
                    {str(key): str(value)[:80] for key, value in mapping.items()}
                )
        return labels

    def meta(self, overrides: dict | None = None) -> dict:
        self._fresh()
        overrides = overrides or {}
        hidden = set(overrides.get("hidden_chart_types", []))
        # 선택 가능 계약과 기존 저장물 렌더 계약을 분리한다. 사용자가 숨긴 타입도
        # 기존 레이아웃에서는 계속 해석할 수 있어야 한다.
        chart_contracts = deepcopy(CHART_TYPES)
        chart_types = deepcopy(CHART_TYPES)
        for key in hidden:
            chart_types.pop(key, None)
        for key, label in overrides.get("chart_label_overrides", {}).items():
            if key in chart_contracts and isinstance(label, str):
                chart_contracts[key]["label"] = label[:80]
                if key in chart_types:
                    chart_types[key]["label"] = label[:80]
        # 기본층 = 스냅샷 실측 code_labels, 그 위에 정적 큐레이션(VALUE_LABELS), 맨 위에 사용자 오버라이드
        value_labels = {field: dict(mapping) for field, mapping in self._code_labels.items()}
        for field, mapping in VALUE_LABELS.items():
            value_labels.setdefault(field, {}).update(deepcopy(mapping))
        for field, mapping in overrides.get("value_label_overrides", {}).items():
            if isinstance(mapping, dict):
                value_labels.setdefault(field, {}).update(
                    {str(key): str(value)[:80] for key, value in mapping.items()}
                )
        domains: dict[str, int] = {}
        for s in self._sources.values():
            domains[s["domain"]] = domains.get(s["domain"], 0) + 1
        default_domain = str(overrides.get("default_domain", "all"))
        if default_domain != "all" and default_domain not in domains:
            default_domain = "all"
        # 데이터소스 도메인(외부 DB 연결 이름)은 고정 사전에 없다 — 이름 그대로 라벨 폴백
        domain_labels = {**DOMAIN_LABELS}
        for d in domains:
            domain_labels.setdefault(d, d)
        return {
            "generated_at": self._generated_at,
            "chart_types": chart_types,
            "chart_contracts": chart_contracts,
            "value_labels": value_labels,
            "domains": domains,
            "domain_labels": domain_labels,
            "default_domain": default_domain,
        }


registry = Registry()
