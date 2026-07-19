from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.auth.config import load_settings
from app.auth.emailer import EmailSender
from app.auth.middleware import InMemoryRateLimiter, rate_limit_category
from app.auth.security import safe_next_path
from app.charts import querybuilder, trino
from app.charts.models import ChartConfig
from app.charts.ontology import CHART_TYPES, registry
from app.charts.router import _validate_charts
from app.notifications import service as notification_module
from app.notifications.service import NotificationMessage, NotificationService


PROJECT_ROOT = Path(__file__).parents[1]


def _production_env(monkeypatch):
    monkeypatch.delenv("AUTH_BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("AUTH_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("AUTH_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "sqlite:////private/tmp/ask-seoul-production-test.db")
    monkeypatch.setenv("AUTH_PUBLIC_BASE_URL", "https://dashboard.example.com")
    monkeypatch.setenv("AUTH_ALLOWED_HOSTS", "dashboard.example.com")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("AUTH_SESSION_PEPPER", "p" * 48)
    monkeypatch.setenv("AUTH_MFA_MASTER_KEY", "m" * 48)
    monkeypatch.setenv("AUTH_REQUIRE_MFA_FOR_PRIVILEGED", "true")


def test_production_security_settings_fail_closed(monkeypatch):
    _production_env(monkeypatch)
    settings = load_settings()
    assert settings.cookie_secure is True
    assert settings.cookie_name.startswith("__Host-")

    monkeypatch.setenv("AUTH_PUBLIC_BASE_URL", "http://dashboard.example.com")
    with pytest.raises(RuntimeError, match="https"):
        load_settings()

    monkeypatch.setenv("AUTH_ENV", "prod")
    with pytest.raises(RuntimeError, match="AUTH_ENV"):
        load_settings()

    _production_env(monkeypatch)
    monkeypatch.setenv("AUTH_REQUIRE_MFA_FOR_PRIVILEGED", "treu")
    with pytest.raises(RuntimeError, match="true 또는 false"):
        load_settings()

    _production_env(monkeypatch)
    monkeypatch.setenv("AUTH_REQUIRE_MFA_FOR_PRIVILEGED", "false")
    with pytest.raises(RuntimeError, match="MFA"):
        load_settings()


def test_bootstrap_credentials_fail_closed(monkeypatch):
    monkeypatch.setenv("AUTH_ENV", "test")
    monkeypatch.setenv("AUTH_BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.delenv("AUTH_BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="함께"):
        load_settings()

    _production_env(monkeypatch)
    monkeypatch.setenv("AUTH_BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv(
        "AUTH_BOOTSTRAP_ADMIN_PASSWORD",
        "Production-Bootstrap-Password-2026!",
    )
    with pytest.raises(RuntimeError, match="production.*bootstrap"):
        load_settings()


def test_production_database_configuration_fails_closed(monkeypatch):
    _production_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        load_settings()

    _production_env(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:password@db.example/ask_seoul",
    )
    with pytest.raises(RuntimeError, match="sslmode=verify-full"):
        load_settings()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:password@db.example/ask_seoul"
        "?sslmode=verify-full&sslrootcert=/run/secrets/db-ca.pem",
    )
    assert load_settings().database_url.startswith("postgresql+psycopg://")

    _production_env(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL",
        "mysql+pymysql://user:password@db.example/ask_seoul?charset=utf8mb4",
    )
    with pytest.raises(RuntimeError, match="ssl_ca"):
        load_settings()
    monkeypatch.setenv(
        "DATABASE_URL",
        "mysql+pymysql://user:password@db.example/ask_seoul"
        "?charset=utf8mb4&ssl_ca=/run/secrets/db-ca.pem&ssl_check_hostname=true",
    )
    assert load_settings().database_url.startswith("mysql+pymysql://")


def test_smtp_security_settings_fail_closed(monkeypatch):
    monkeypatch.setenv("SMTP_USE_TLS", "treu")
    with pytest.raises(RuntimeError, match="true 또는 false"):
        EmailSender()

    monkeypatch.setenv("SMTP_USE_TLS", "false")
    monkeypatch.setenv("SMTP_USE_SSL", "false")
    monkeypatch.setenv("SMTP_ALLOW_PLAINTEXT", "false")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM_EMAIL", "noreply@example.com")
    monkeypatch.setenv("AUTH_ENV", "production")
    with pytest.raises(RuntimeError, match="TLS/SSL"):
        EmailSender()


def test_notification_webhooks_reject_ssrf_targets_and_mask_chat_ids(
    monkeypatch,
):
    monkeypatch.setenv(
        "NOTIFY_DISCORD_WEBHOOK_URL",
        "https://127.0.0.1/api/webhooks/internal",
    )
    monkeypatch.setenv("NOTIFY_SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv(
        "NOTIFY_TELEGRAM_BOT_TOKEN",
        "123456:" + "A" * 24,
    )
    monkeypatch.setenv("NOTIFY_TELEGRAM_CHAT_IDS", "-1001234567890")

    called = []
    monkeypatch.setattr(
        notification_module,
        "_post_json",
        lambda url, payload: called.append((url, payload)),
    )
    service = NotificationService()
    configuration = service.configuration()
    result = service.send(NotificationMessage(title="test", body="body"))

    configured_discord = next(
        item for item in configuration.deliveries if item.channel == "discord"
    )
    discord = next(item for item in result.deliveries if item.channel == "discord")
    telegram = next(item for item in result.deliveries if item.channel == "telegram")
    assert configured_discord.configured is False
    assert discord.configured is False
    assert discord.delivered is False
    assert "허용되지 않은" in discord.error
    assert telegram.target == "chat:***7890"
    assert all("127.0.0.1" not in url for url, _payload in called)


def test_rate_limiter_rejection_does_not_consume_other_windows(monkeypatch):
    limiter = InMemoryRateLimiter()
    monkeypatch.setattr("app.auth.middleware.time.time", lambda: 120.0)
    limits = {"second": 1, "minute": 2}

    assert limiter.check("subject", limits)[0] is True
    assert limiter.check("subject", limits)[0] is False

    monkeypatch.setattr("app.auth.middleware.time.time", lambda: 121.0)
    # 직전 second 제한 거부가 minute bucket까지 소모하지 않아 한 번 더 허용된다.
    assert limiter.check("subject", limits)[0] is True


def test_chart_availability_uses_the_expensive_query_rate_limit():
    assert rate_limit_category(
        "/api/v1/charts/sources/gold_weather_daily/availability",
        authenticated=True,
    ) == "charts_query"
    assert rate_limit_category(
        "/api/v1/charts/sources/gold_weather_daily",
        authenticated=True,
    ) == "authenticated"


def test_identical_cold_trino_queries_are_singleflight(monkeypatch, tmp_path):
    monkeypatch.setattr(trino, "CACHE_DIR", tmp_path)
    calls = []

    def fake_run(sql: str, max_rows: int):
        calls.append(sql)
        time.sleep(0.05)
        return ["value"], [[1]]

    monkeypatch.setattr(trino, "_run", fake_run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: trino.execute("select 1"), range(2)))

    assert len(calls) == 1
    assert {result["mode"] for result in results} == {"live", "cache"}


def test_safe_next_path_rejects_cross_origin_and_backslash_forms():
    allowed = ("/catalog", "/charts", "/profile", "/admin", "/docs")
    assert safe_next_path("/charts/page?id=1", allowed) == "/charts/page?id=1"
    assert safe_next_path("//evil.example", allowed) == "/catalog"
    assert safe_next_path("/\\evil.example", allowed) == "/catalog"
    assert safe_next_path("https://evil.example", allowed) == "/catalog"


def test_trino_cancel_rejects_cross_origin_next_uri(monkeypatch):
    called = []
    monkeypatch.setattr(
        trino.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: called.append(True),
    )
    trino._cancel("http://169.254.169.254/latest/meta-data/")
    assert called == []


def test_query_builder_quotes_snapshot_identifiers_and_rejects_relation_injection():
    source = {
        "relation": "iceberg.gold.safe_table",
        "fields": [
            {"name": 'odd"dimension', "type": "varchar"},
        ],
    }
    sql = querybuilder.build(
        source,
        {
            "dims": ['odd"dimension'],
            "measures": [{"field": None, "agg": "count"}],
            "limit": 10,
        },
    )
    assert '"iceberg"."gold"."safe_table"' in sql
    assert '"odd""dimension"' in sql

    source["relation"] = "iceberg.gold.safe_table;drop table users"
    with pytest.raises(querybuilder.SpecError, match="relation"):
        querybuilder.build(
            source,
            {
                "dims": [],
                "measures": [{"field": None, "agg": "count"}],
            },
        )


def test_saved_chart_options_follow_server_contract():
    source = next(
        item
        for item in registry.sources()
        if "bar" in item["supports"]
        and any(field["role"] == "measure" for field in item["fields"])
    )
    chart_type = CHART_TYPES["bar"]
    bindings = {}
    for slot in chart_type["slots"]:
        if not slot["required"]:
            continue
        bindings[slot["name"]] = next(
            field["name"]
            for field in source["fields"]
            if field["role"] in slot["accepts"]
        )
    chart = ChartConfig(
        id="contract-test",
        type="bar",
        source=source["name"],
        bindings=bindings,
        options={"top_n": '\"><img src=x onerror=alert(1)>'},
    )
    with pytest.raises(querybuilder.SpecError, match="유한한 숫자"):
        _validate_charts([chart])


def test_external_static_assets_are_pinned_with_sri():
    static_root = Path(__file__).parents[1] / "app" / "static"
    tags = []
    for path in static_root.rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        tags.extend(
            re.findall(
                r"<(?:script|link)\b[^>]+https://cdn\.jsdelivr\.net[^>]*>",
                html,
                flags=re.IGNORECASE,
            )
        )
    assert tags
    assert all("integrity=\"sha384-" in tag for tag in tags)
    assert all("crossorigin=\"anonymous\"" in tag for tag in tags)


def test_static_html_security_and_local_asset_invariants():
    static_root = PROJECT_ROOT / "app" / "static"
    for path in static_root.rglob("*.html"):
        html = path.read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE)
        for tag in re.findall(
            r"<a\b[^>]*\btarget=\"_blank\"[^>]*>",
            html,
            flags=re.IGNORECASE,
        ):
            assert re.search(
                r"\brel=\"[^\"]*\bnoopener\b[^\"]*\bnoreferrer\b[^\"]*\"",
                tag,
                flags=re.IGNORECASE,
            )
        for asset in re.findall(
            r"(?:src|href)=\"(/static/[^\"?#]+)",
            html,
            flags=re.IGNORECASE,
        ):
            assert (static_root / asset.removeprefix("/static/")).is_file(), (
                f"{path.name}: 없는 정적 자산 {asset}"
            )


@pytest.mark.parametrize(
    "script_name",
    (
        "init_auth_db.py",
        "create_admin.py",
        "setup_mfa.py",
        "process_notifications.py",
        "cleanup_auth.py",
    ),
)
def test_operations_scripts_can_run_directly(script_name):
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / script_name), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


@pytest.mark.parametrize("script_name", ("create_admin.py", "setup_mfa.py"))
def test_secret_operator_scripts_require_interactive_tty(script_name):
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / script_name),
            "--email",
            "operator@example.com",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "TTY" in result.stderr + result.stdout
