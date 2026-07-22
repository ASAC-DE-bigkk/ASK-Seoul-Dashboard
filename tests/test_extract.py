from __future__ import annotations

from pathlib import Path

import pytest

import extract


def _table(name: str, domain: str) -> dict:
    return {
        "name": name,
        "domain": domain,
        "relation": f"iceberg_dev.{domain}.{name}",
        "columns": [],
    }


def test_resolve_sample_dir_supports_embedded_dashboard(tmp_path: Path) -> None:
    dashboard = tmp_path / "sample" / "dashboard"
    dashboard.mkdir(parents=True)
    (dashboard.parent / "dbt").mkdir()
    (dashboard.parent / "docker-compose.yml").touch()

    assert extract.resolve_sample_dir(dashboard) == dashboard.parent


def test_merge_basic_domains_preserves_other_domains_and_traceability() -> None:
    snapshot = {
        "generated_at": "2026-07-20T00:00:00+00:00",
        "domain": "all",
        "domains": {"culture": 1, "traffic": 1},
        "table_count": 2,
        "tables": [
            _table("gold_culture", "culture"),
            _table("gold_traffic", "traffic"),
        ],
    }
    commerce = [_table("gold_license", "commerce")]

    merged = extract.merge_basic_domains(
        snapshot,
        {"commerce": commerce},
        "2026-07-21T00:00:00+00:00",
    )

    assert [table["name"] for table in merged["tables"]] == [
        "gold_culture",
        "gold_license",
        "gold_traffic",
    ]
    assert merged["domains"] == {"culture": 1, "commerce": 1, "traffic": 1}
    assert merged["table_count"] == 3
    assert merged["domain_generated_at"] == {
        "culture": "2026-07-20T00:00:00+00:00",
        "traffic": "2026-07-20T00:00:00+00:00",
        "commerce": "2026-07-21T00:00:00+00:00",
    }
    assert merged["refresh"] == {
        "mode": "partial_basic",
        "source_system": "trino",
        "catalog": "iceberg_dev",
        "domains": ["commerce"],
        "observed_at": "2026-07-21T00:00:00+00:00",
    }
    assert "commerce" not in snapshot["domains"]


def test_merge_basic_domains_rejects_empty_replacement() -> None:
    with pytest.raises(ValueError, match="empty table set"):
        extract.merge_basic_domains(
            {"generated_at": "now", "domains": {}, "tables": []},
            {"commerce": []},
            "later",
        )


def test_extract_basic_domain_preserves_external_metadata(monkeypatch) -> None:
    def fake_trino_rows(sql: str, timeout: int = 300) -> list[dict]:
        if sql.startswith("SHOW TABLES"):
            return [{"Table": "gold_internal"}]
        if sql.startswith("SHOW COLUMNS"):
            return [{"Column": "cnt", "Type": "bigint", "Comment": ""}]
        if sql.startswith("SELECT approx_distinct"):  # 컬럼 통계 실측(축 자율성 근거)
            return [{"d_0": 7, "mn_0": 1.0, "mx_0": 9.0}]
        raise AssertionError(sql)

    monkeypatch.setattr(extract, "trino_rows", fake_trino_rows)
    monkeypatch.setattr(
        extract,
        "measure",
        lambda relation, columns: (1, None, []),
    )

    tables = extract.extract_basic_domain(
        "commerce",
        "commerce",
        {
            "gold_internal": {
                "external": False,
                "description": "내부 마트",
            }
        },
    )

    assert tables[0]["external"] is False
    # 실측 통계가 컬럼에 병합된다 — ontology groupable/구간 기본 폭의 근거
    assert tables[0]["columns"][0]["distinct_count"] == 7
    assert tables[0]["columns"][0]["min"] == 1.0
    assert tables[0]["columns"][0]["max"] == 9.0
