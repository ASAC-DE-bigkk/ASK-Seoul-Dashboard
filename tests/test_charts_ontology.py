from __future__ import annotations

import hashlib
import importlib
import json

import pytest

from app.charts import querybuilder
from app.charts.layouts import SEED_PATH
from app.charts.models import ChartConfig, SourceDetail
from app.charts.ontology import (
    CHART_TYPES,
    companion_pairs,
    compatible_bindings,
    registry,
)
from app.charts.router import _validate_charts, source_availability

charts_router = importlib.import_module("app.charts.router")


DOMAINS = {"culture", "commerce", "traffic", "weather", "citydata", "transit"}


def _contract_test_id(source_name: str, chart_type: str) -> str:
    digest = hashlib.sha256(f"{source_name}:{chart_type}".encode()).hexdigest()[:12]
    return f"contract-{chart_type}-{digest}"


def _config_for_supported(source: dict, chart_type: str) -> ChartConfig:
    spec = CHART_TYPES[chart_type]
    if chart_type != "scatter":
        bindings = compatible_bindings(source["fields"], spec, count_mode=True)
        if bindings is not None:
            return ChartConfig(
                id=_contract_test_id(source["name"], chart_type),
                type=chart_type,
                source=source["name"],
                bindings=bindings,
                agg="count",
            )
    bindings = compatible_bindings(source["fields"], spec)
    assert bindings is not None
    return ChartConfig(
        id=_contract_test_id(source["name"], chart_type),
        type=chart_type,
        source=source["name"],
        bindings=bindings,
        agg="avg",
    )


def test_every_gold_domain_and_source_has_a_real_chart_contract() -> None:
    assert DOMAINS <= {source["domain"] for source in registry.sources()}
    for domain in DOMAINS:
        sources = registry.sources(domain)
        assert sources, domain
        for source in sources:
            assert source["supports"], source["name"]
            for chart_type in source["supports"]:
                chart = _config_for_supported(source, chart_type)
                _validate_charts([chart])


def test_required_slots_use_distinct_fields() -> None:
    source = registry.get("gold_culture_activity_by_dong")
    assert source is not None
    with pytest.raises(querybuilder.SpecError, match="서로 다른 필드"):
        _validate_charts(
            [
                ChartConfig(
                    id="duplicate-scatter",
                    type="scatter",
                    source=source["name"],
                    bindings={
                        "axis": "admin_dong",
                        "x": "activities_count",
                        "y": "activities_count",
                    },
                    agg="avg",
                )
            ]
        )
    with pytest.raises(querybuilder.SpecError, match="서로 다른 필드"):
        _validate_charts(
            [
                ChartConfig(
                    id="duplicate-heatmap",
                    type="heatmap",
                    source=source["name"],
                    bindings={
                        "x": "event_date",
                        "y": "event_date",
                        "value": "activities_count",
                    },
                    agg="sum",
                )
            ]
        )


def test_measureless_schedule_uses_row_count_without_fake_field() -> None:
    source = registry.get("gold_culture_sports_schedule")
    assert source is not None
    chart = ChartConfig(
        id="sports-count",
        type="race",
        source=source["name"],
        bindings={"time": "game_date", "axis": "stadium"},
        agg="count",
        options={"cumulative": False},
    )
    _validate_charts([chart])
    sql = querybuilder.build(
        source,
        {
            "dims": ["game_date", "stadium"],
            "measures": [{"field": None, "agg": "count", "alias": "count"}],
        },
    )
    assert "count(*)" in sql
    assert "__row_count__" not in sql


def test_non_additive_and_conditional_aggregations_are_rejected() -> None:
    weather = registry.get("gold_weather_daily_by_admin_dong")
    assert weather is not None
    with pytest.raises(querybuilder.SpecError, match="sum 집계"):
        _validate_charts(
            [
                ChartConfig(
                    id="temperature-sum",
                    type="line",
                    source=weather["name"],
                    bindings={"axis": "forecast_date", "value": "temp_avg_c"},
                    agg="sum",
                )
            ]
        )
    with pytest.raises(querybuilder.SpecError, match="현재 race 옵션"):
        _validate_charts(
            [
                ChartConfig(
                    id="cumulative-average",
                    type="race",
                    source=weather["name"],
                    bindings={
                        "time": "forecast_date",
                        "axis": "admin_dong",
                        "value": "temp_avg_c",
                    },
                    agg="avg",
                    options={"cumulative": True},
                )
            ]
        )
    transit = registry.get("gold_transit_dong_hourly")
    assert transit is not None
    by_name = {field["name"]: field for field in transit["fields"]}
    assert by_name["bus_obs_cnt"]["cumulative_safe"] is False
    with pytest.raises(querybuilder.SpecError, match="중복 합산"):
        _validate_charts(
            [
                ChartConfig(
                    id="cumulative-active-stock",
                    type="race",
                    source=transit["name"],
                    bindings={
                        "time": "hour_at",
                        "axis": "admin_dong_code",
                        "value": "bus_obs_cnt",
                    },
                    agg="sum",
                    options={"cumulative": True},
                )
            ]
        )
    with pytest.raises(querybuilder.SpecError, match="pie 차트"):
        _validate_charts(
            [
                ChartConfig(
                    id="temperature-pie",
                    type="pie",
                    source=weather["name"],
                    bindings={"axis": "gu", "value": "temp_avg_c"},
                    agg="avg",
                )
            ]
        )


def test_roles_and_recommendation_metadata_cover_non_commerce_fields() -> None:
    weather = registry.get("gold_weather_daily_by_admin_dong")
    city = registry.get("gold_citydata_ppltn_forecast")
    transit = registry.get("gold_transit_dong_hourly")
    assert weather and city and transit
    by_name = lambda source: {field["name"]: field for field in source["fields"]}
    assert by_name(weather)["forecast_date"]["role"] == "time"
    assert by_name(weather)["temp_avg_c"]["preferred_agg"] == "avg"
    assert "sum" not in by_name(weather)["temp_avg_c"]["allowed_aggs"]
    assert by_name(weather)["tmx_c"]["preferred_agg"] == "max"
    assert by_name(weather)["tmn_c"]["preferred_agg"] == "min"
    assert by_name(city)["hr"]["role"] == "sequence"
    assert by_name(city)["ppltn_std"]["preferred_agg"] == "avg"
    assert "sum" not in by_name(city)["ppltn_std"]["allowed_aggs"]
    city_latest = registry.get("gold_citydata_place_latest")
    city_weather = registry.get("gold_citydata_ppltn_x_weather_hourly")
    assert city_latest and city_weather
    assert "sum" not in by_name(city_latest)["pm25"]["allowed_aggs"]
    assert "sum" not in by_name(city_weather)["precip_prob"]["allowed_aggs"]
    data_quality = registry.get("gold_license_data_quality")
    assert data_quality is not None
    for field_name in (
        "phone_coverage",
        "geo_coverage",
        "admin_dong_coverage",
        "address_coverage",
    ):
        assert by_name(data_quality)[field_name]["preferred_agg"] == "avg"
        assert "sum" not in by_name(data_quality)[field_name]["allowed_aggs"]
    culture_slo = registry.get("gold_culture_slo_daily")
    weather_place = registry.get("gold_weather_forecast_by_place")
    assert culture_slo and weather_place
    assert by_name(culture_slo)["ingest_duration_min"]["preferred_agg"] == "avg"
    assert "sum" not in by_name(weather_place)["grid_distance_m"]["allowed_aggs"]
    assert by_name(transit)["bus_congestion_avg"]["preferred_agg"] == "avg"
    long_weather = registry.get("gold_weather_forecast_by_admin_dong")
    assert long_weather is not None
    assert by_name(long_weather)["fcst_value_num"]["chartable"] is False
    weather_current = registry.get("gold_weather_current_wide_by_admin_dong")
    assert weather_current is not None
    assert by_name(weather_current)["wind_dir_deg"]["chartable"] is False
    commerce_summary = registry.get("gold_license_dong_summary")
    city_scorecard = registry.get("gold_citydata_place_scorecard")
    assert commerce_summary and city_scorecard
    assert by_name(commerce_summary)["latest_collected_at"]["chartable"] is False
    assert by_name(city_scorecard)["refreshed_at"]["chartable"] is False
    assert "line" not in commerce_summary["supports"]
    assert "race" not in city_scorecard["supports"]
    assert "category" not in CHART_TYPES["line"]["slots"][0]["accepts"]
    active_culture = registry.get("gold_culture_activity_by_dong")
    active_city = registry.get("gold_citydata_ppltn_x_culture_daily")
    proven_flow = registry.get("gold_license_flow_monthly")
    assert active_culture and active_city and proven_flow
    assert by_name(active_culture)["activities_count"]["cumulative_safe"] is False
    assert by_name(active_city)["event_count"]["cumulative_safe"] is False
    assert by_name(proven_flow)["cnt"]["cumulative_safe"] is True
    assert by_name(city)["hr"]["allowed_filter_ops"] == [
        "eq", "neq", "gt", "gte", "lt", "lte", "between", "not_between",
        "in", "not_in", "is_null", "not_null",
    ]
    # time+granularity 필드는 last_n(최근 N) 이 열린다 — 필드 메타 의존 계약
    ym_ops = {f["name"]: f for f in registry.get("gold_license_flow_monthly")["fields"]}
    assert "last_n" in ym_ops["ym"]["allowed_filter_ops"]
    # 고정폭 코드는 동등/집합/NULL 만 — 대소·패턴 차단
    assert by_name(commerce_summary)["gu_code"]["allowed_filter_ops"] == [
        "eq", "neq", "in", "not_in", "is_null", "not_null",
    ]


def test_identity_companions_promote_codes_and_serve_korean_labels() -> None:
    """식별=코드·표기=한글 계약 — 동명이동(신사동)이 이름 그룹핑으로 합산되지 않도록
    표시 필드에 id_field(코드 승격), 코드값에 value_labels(한글, 중복은 구명 접미사)."""
    assert companion_pairs(
        ["admin_dong_code", "admin_dong", "gu_code", "gu", "legal_code", "legal_dong",
         "major", "major_ko", "cnt", "ym"]
    ) == [
        ("admin_dong_code", "admin_dong"),
        ("gu_code", "gu"),
        ("legal_code", "legal_dong"),
        ("major", "major_ko"),
    ]

    summary = registry.get("gold_license_dong_summary")
    assert summary is not None
    by_name = {field["name"]: field for field in summary["fields"]}
    assert by_name["admin_dong"]["id_field"] == "admin_dong_code"
    assert by_name["gu"]["id_field"] == "gu_code"
    assert by_name["admin_dong_code"]["label_field"] == "admin_dong"
    # pydantic 응답 계약이 동반 메타를 잘라먹지 않는다
    detail = SourceDetail.model_validate(summary)
    assert next(f for f in detail.fields if f.name == "admin_dong").id_field == "admin_dong_code"

    flow = registry.get("gold_license_flow_monthly")
    assert flow is not None
    flow_by = {field["name"]: field for field in flow["fields"]}
    assert flow_by["legal_dong"]["role"] == "geo_legal_dong"
    assert flow_by["legal_dong"]["id_field"] == "legal_code"
    matrix = registry.get("gold_license_dong_category_matrix")
    assert matrix is not None
    matrix_by = {field["name"]: field for field in matrix["fields"]}
    assert matrix_by["category_ko"]["id_field"] == "category"

    meta = registry.meta()
    labels = meta["value_labels"]["admin_dong_code"]
    assert labels["1168051000"] == "신사동·강남구"   # 강남구 신사동
    assert labels["1162068500"] == "신사동·관악구"   # 관악구 신사동 — 코드로 분리
    assert labels["UNK"] == "미상"                   # 정적 큐레이션이 실측 사전 위에 얹힘
    assert meta["value_labels"]["gu_code"]["11680"] == "강남구"
    assert meta["value_labels"]["category"]          # 업종 en→ko 실측 사전
    # 행정동 지도: 자산 mois_code 병기로 코드 role 매칭 허용
    assert "geo_dong_code" in CHART_TYPES["map_seoul_dong"]["slots"][0]["accepts"]


def test_binned_measure_axis_contract() -> None:
    """구간화(B) — 숫자 측정값은 bins[슬롯] 폭이 있을 때만 binnable 축이 된다."""
    lifespan = registry.get("gold_license_lifespan")
    assert lifespan is not None
    ok = ChartConfig(
        id="bin-ok", type="bar", source=lifespan["name"],
        bindings={"axis": "avg_days", "value": "n_closed"},
        bins={"axis": 30}, agg="sum",
    )
    _validate_charts([ok])  # 축의 avg_days(비가산)는 그룹 키 — value(n_closed) 집계만 제약

    with pytest.raises(querybuilder.SpecError, match="구간 폭"):
        _validate_charts([ChartConfig(
            id="bin-missing", type="bar", source=lifespan["name"],
            bindings={"axis": "avg_days", "value": "n_closed"}, agg="sum",
        )])
    flow = registry.get("gold_license_flow_monthly")
    assert flow is not None
    with pytest.raises(querybuilder.SpecError, match="구간\\(bin\\)을 지원하지"):
        _validate_charts([ChartConfig(
            id="bin-line", type="line", source=flow["name"],
            bindings={"axis": "ym", "value": "cnt"},
            bins={"axis": 30}, agg="sum",
        )])
    with pytest.raises(querybuilder.SpecError, match="숫자 측정값"):
        _validate_charts([ChartConfig(
            id="bin-category", type="bar", source=lifespan["name"],
            bindings={"axis": "category", "value": "n_closed"},
            bins={"axis": 30}, agg="sum",
        )])
    with pytest.raises(querybuilder.SpecError, match="유한한 양수"):
        _validate_charts([ChartConfig(
            id="bin-zero", type="bar", source=lifespan["name"],
            bindings={"axis": "avg_days", "value": "n_closed"},
            bins={"axis": 0}, agg="sum",
        )])

    sql = querybuilder.build(lifespan, {
        "dims": [{"field": "avg_days", "bin_width": 30}],
        "measures": [{"field": None, "agg": "count", "alias": "count"}],
    })
    assert 'floor(try_cast("avg_days" as double) / 30.0) * 30.0' in sql
    assert 'as "avg_days"' in sql  # 별칭=필드명 — 소비자는 일반 dim 과 동일
    with pytest.raises(querybuilder.SpecError, match="서로 달라야"):
        querybuilder.build(lifespan, {
            "dims": ["avg_days", {"field": "avg_days", "bin_width": 30}],
            "measures": [{"field": None, "agg": "count"}],
        })


def test_low_cardinality_measures_open_as_group_axes() -> None:
    """자율성 개방(A) — 실측 distinct ≤ 임계인 measure 는 category 축 슬롯에 선다."""
    groupables = [
        (source["name"], field["name"])
        for source in registry.sources()
        for field in source["fields"]
        if field.get("groupable")
    ]
    assert groupables, "스냅샷에 groupable 필드가 없습니다 — extract 컬럼 통계 실측 확인"
    source_name, field_name = groupables[0]
    source = registry.get(source_name)
    _validate_charts([ChartConfig(
        id="groupable-axis", type="table", source=source_name,
        bindings={"axis": field_name}, agg="count",
    )])
    by_name = {field["name"]: field for field in source["fields"]}
    assert by_name[field_name]["role"] == "measure"
    assert by_name[field_name]["distinct_count"] <= 50


def test_hidden_chart_types_keep_a_render_contract() -> None:
    meta = registry.meta({"hidden_chart_types": ["bar"]})
    assert "bar" not in meta["chart_types"]
    assert "bar" in meta["chart_contracts"]


def test_single_row_sources_only_offer_single_value_views() -> None:
    for name in ("gold_traffic_incident_summary", "gold_weather_forecast_summary"):
        source = registry.get(name)
        assert source is not None
        assert source["row_count"] == 1
        assert set(source["supports"]) <= {"stat", "table"}


def test_source_availability_counts_only_registry_fields(monkeypatch) -> None:
    source = registry.get("gold_transit_dong_hourly")
    assert source is not None
    aliases = [f"field_{index}" for index, _ in enumerate(source["fields"])]
    captured: dict[str, object] = {}

    def fake_execute(sql: str, max_rows: int = 0, force: bool = False) -> dict:
        captured.update(sql=sql, max_rows=max_rows)
        return {
            "columns": aliases,
            "rows": [[0, *([7] * (len(aliases) - 1))]],
            "mode": "live",
            "elapsed_ms": 3,
        }

    monkeypatch.setattr(charts_router.trino, "execute", fake_execute)
    result = source_availability(source["name"], _user=object())

    first_field = source["fields"][0]["name"]
    assert result["fields"][first_field] == 0
    assert set(result["fields"]) == {field["name"] for field in source["fields"]}
    assert result["mode"] == "live"
    assert captured["max_rows"] == 1
    assert source["relation"].split(".")[-1] in str(captured["sql"])
    assert str(captured["sql"]).count("count(") == len(source["fields"])


def test_all_seed_pages_pass_the_same_server_validation_as_user_layouts() -> None:
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    assert any(page["id"] == "seed-domains" for page in seed["pages"])
    for page in seed["pages"]:
        _validate_charts([ChartConfig.model_validate(chart) for chart in page["charts"]])


def test_all_curated_default_charts_are_real_server_valid_contracts() -> None:
    for source in registry.sources():
        default = source.get("default_chart")
        if not default:
            continue
        _validate_charts(
            [
                ChartConfig(
                    id=f"default-{source['name']}",
                    source=source["name"],
                    **default,
                )
            ]
        )


def test_querybuilder_rejects_duplicate_dimensions_and_invalid_sum() -> None:
    source = registry.get("gold_weather_daily_by_admin_dong")
    assert source is not None
    with pytest.raises(querybuilder.SpecError, match="차원 필드는 서로 달라야"):
        querybuilder.build(
            source,
            {
                "dims": ["forecast_date", "forecast_date"],
                "measures": [{"field": "temp_avg_c", "agg": "avg"}],
            },
        )
    with pytest.raises(querybuilder.SpecError, match="sum 집계"):
        querybuilder.build(
            source,
            {
                "dims": ["forecast_date"],
                "measures": [{"field": "temp_avg_c", "agg": "sum"}],
            },
        )
    with pytest.raises(querybuilder.SpecError, match="비워둘 수 없습니다"):
        querybuilder.build(
            source,
            {
                "dims": ["forecast_date"],
                "measures": [{"field": "temp_avg_c", "agg": "avg"}],
                "filters": [{"field": "forecast_date", "op": "eq", "value": ""}],
            },
        )
    with pytest.raises(querybuilder.SpecError, match="like 연산자"):
        querybuilder.build(
            source,
            {
                "dims": ["forecast_date"],
                "measures": [{"field": "temp_avg_c", "agg": "avg"}],
                "filters": [{"field": "temp_avg_c", "op": "like", "value": "2%"}],
            },
        )
    numeric_sql = querybuilder.build(
        source,
        {
            "dims": ["forecast_date"],
            "measures": [{"field": "temp_avg_c", "agg": "avg"}],
            "filters": [{"field": "temp_avg_c", "op": "gte", "value": "10"}],
        },
    )
    assert 'try_cast("temp_avg_c" as double) >= 10.0' in numeric_sql
    with pytest.raises(querybuilder.SpecError, match="유한한 숫자"):
        querybuilder.build(
            source,
            {
                "dims": ["forecast_date"],
                "measures": [{"field": "temp_avg_c", "agg": "avg"}],
                "filters": [{"field": "temp_avg_c", "op": "gte", "value": "warm"}],
            },
        )
    with pytest.raises(querybuilder.SpecError, match="like 연산자"):
        _validate_charts(
            [
                ChartConfig(
                    id="invalid-filter-contract",
                    type="line",
                    source=source["name"],
                    bindings={"axis": "forecast_date", "value": "temp_avg_c"},
                    agg="avg",
                    filters=[{"field": "temp_avg_c", "op": "like", "value": "2%"}],
                )
            ]
        )


def test_layout_numeric_options_must_be_query_safe_integers() -> None:
    source = registry.get("gold_culture_activity_by_dong")
    assert source is not None
    with pytest.raises(querybuilder.SpecError, match="정수"):
        _validate_charts(
            [
                ChartConfig(
                    id="fractional-limit",
                    type="table",
                    source=source["name"],
                    bindings={"axis": "admin_dong", "value": "activities_count"},
                    agg="sum",
                    options={"top_n": 3.5},
                )
            ]
        )
