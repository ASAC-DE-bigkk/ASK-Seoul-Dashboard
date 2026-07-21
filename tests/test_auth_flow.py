from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest


DB_PATH = tempfile.mktemp(prefix="ask-seoul-auth-", suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["AUTH_SESSION_PEPPER"] = "test-only-session-pepper-with-enough-entropy"
os.environ["AUTH_MFA_MASTER_KEY"] = "test-only-mfa-master-key-with-enough-entropy"
os.environ["AUTH_PUBLIC_BASE_URL"] = "http://testserver"
os.environ["AUTH_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ["AUTH_MODE"] = "required"
os.environ["AUTH_BOOTSTRAP_ADMIN_EMAIL"] = "root@example.com"
os.environ["AUTH_BOOTSTRAP_ADMIN_PASSWORD"] = "Ginkgo-River-Access-2026!"
os.environ["AUTH_AUTO_APPROVE_VERIFIED"] = "false"
for name in (
    "SMTP_HOST",
    "SMTP_FROM_EMAIL",
    "NOTIFY_DISCORD_WEBHOOK_URL",
    "NOTIFY_SLACK_WEBHOOK_URL",
    "NOTIFY_TELEGRAM_BOT_TOKEN",
    "NOTIFY_TELEGRAM_CHAT_IDS",
):
    os.environ.pop(name, None)

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.auth.models import (
    AccountToken,
    AuditLog,
    AuthSession,
    MfaChallenge,
    MfaRecoveryCode,
    NotificationDelivery,
    PaymentRequest,
    User,
    utcnow,
)
from app.auth.database import Database
from app.auth.security import (
    derive_totp_secret,
    hash_password,
    random_token,
    totp_code,
)
from app.auth.service import (
    AuthService,
    DomainError,
    PaymentService,
    deliver_payment_notification,
)
from app.notifications.service import DeliveryResult, NotificationResult
from app.main import app, health
from scripts.setup_mfa import _confirm_setup


ADMIN_PASSWORD = "Ginkgo-River-Access-2026!"
USER_PASSWORD = "Canopy-Harbor-Secure-2026!"
OPERATOR_PASSWORD = "Granite-Morning-Access-2026!"


def csrf(client: TestClient) -> dict[str, str]:
    return {"x-csrf-token": client.cookies.get("askseoul_csrf")}


def login(client: TestClient, email: str, password: str):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "remember": False, "next": "/catalog"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def register(client: TestClient, email: str, password: str):
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "terms_accepted": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"


def test_complete_auth_rbac_policy_and_payment_flow():
    with TestClient(app) as anonymous, TestClient(app) as admin, TestClient(
        app
    ) as member, TestClient(app) as operator:
        landing = anonymous.get("/")
        assert landing.status_code == 200
        assert anonymous.get("/api/v1/public/summary").status_code == 200
        protected = anonymous.get(
            "/catalog", headers={"accept": "text/html"}, follow_redirects=False
        )
        assert protected.status_code == 303
        assert protected.headers["location"].startswith("/auth/login")
        assert anonymous.get("/api/v1/catalog/tables").status_code == 401
        assert anonymous.get("/docs/oauth2-redirect").status_code == 401
        assert anonymous.get("/static/auth/profile.html").status_code == 401
        assert anonymous.get("/static/auth/admin.html").status_code == 401

        register(member, "member@example.com", USER_PASSWORD)
        pending_login = member.post(
            "/api/v1/auth/login",
            json={"email": "member@example.com", "password": USER_PASSWORD},
        )
        assert pending_login.status_code == 403

        admin_login = login(admin, "root@example.com", ADMIN_PASSWORD)
        assert admin_login["user"]["role"] == "admin"
        docs = admin.get("/docs")
        assert docs.status_code == 200
        assert "swagger-ui-dist@5.32.8" in docs.text
        assert "supportedSubmitMethods: []" in docs.text
        assert 'integrity="sha384-' in docs.text
        inline = re.findall(
            r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
            docs.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assert len(inline) == 1
        digest = base64.b64encode(
            hashlib.sha256(inline[0].encode("utf-8")).digest()
        ).decode("ascii")
        assert f"'sha256-{digest}'" in docs.headers["content-security-policy"]
        all_users = admin.get("/api/v1/admin/users").json()["items"]
        admin_id = next(item["id"] for item in all_users if item["role"] == "admin")
        member_row = next(item for item in all_users if item["email"] == "member@example.com")
        member_id = member_row["id"]
        approve = admin.patch(
            f"/api/v1/admin/users/{member_id}",
            headers=csrf(admin),
            json={"role": "guest", "status": "active"},
        )
        assert approve.status_code == 200, approve.text
        reserved = admin.put(
            f"/api/v1/admin/access/users/{member_id}",
            headers=csrf(admin),
            json={"permissions": [{"page_key": "admin_users", "allowed": True}]},
        )
        assert reserved.status_code == 403
        immutable_admin = admin.put(
            "/api/v1/admin/access/roles/admin",
            headers=csrf(admin),
            json={"permissions": [{"page_key": "catalog", "allowed": False}]},
        )
        assert immutable_admin.status_code == 403

        user_login = login(member, "member@example.com", USER_PASSWORD)
        assert user_login["user"]["role_label"] == "게스트"
        assert user_login["user"]["can_edit_charts"] is False
        assert user_login["user"]["masked_id"].startswith(member_id[:4])
        assert member.get("/catalog").status_code == 200
        assert member.get("/catalog").headers["cache-control"] == "no-store"
        assert member.get("/api/v1/catalog/tables").status_code == 200
        catalog_snapshot = member.get("/api/v1/catalog/snapshot")
        assert catalog_snapshot.status_code == 200
        assert catalog_snapshot.json()["table_count"] == len(
            catalog_snapshot.json()["tables"]
        )
        assert member.get("/api/v1/charts/meta").status_code == 403

        missing_csrf = member.patch(
            "/api/v1/me/nickname", json={"nickname": "서울데이터회원"}
        )
        assert missing_csrf.status_code == 403
        nickname = member.patch(
            "/api/v1/me/nickname",
            headers=csrf(member),
            json={"nickname": "서울데이터회원"},
        )
        assert nickname.status_code == 200

        preference = member.put(
            "/api/v1/me/preferences",
            headers=csrf(member),
            json={
                "ontology": {"hidden_chart_types": ["bar"], "default_domain": "culture"},
                "ui": {"dense": True},
            },
        )
        assert preference.status_code == 200

        promote = admin.patch(
            f"/api/v1/admin/users/{member_id}",
            headers=csrf(admin),
            json={"role": "member", "status": "active"},
        )
        assert promote.status_code == 200
        assert member.get("/api/v1/charts/meta").status_code == 401
        member_login = login(member, "member@example.com", USER_PASSWORD)
        assert member_login["user"]["can_edit_charts"] is True
        meta = member.get("/api/v1/charts/meta")
        assert meta.status_code == 200
        assert "bar" not in meta.json()["chart_types"]
        assert meta.json()["default_domain"] == "culture"
        layouts = member.get("/api/v1/charts/layouts")
        assert layouts.status_code == 200
        assert layouts.json()
        personal_layout = member.post(
            "/api/v1/charts/layouts",
            headers=csrf(member),
            json={"name": "회원 전용 레이아웃"},
        )
        assert personal_layout.status_code == 200
        personal_layout_id = personal_layout.json()["id"]
        invalid_layout = member.patch(
            f"/api/v1/charts/layouts/{personal_layout_id}",
            headers=csrf(member),
            json={
                "charts": [
                    {
                        "id": "invalid-chart",
                        "title": "잘못된 소스",
                        "type": "bar",
                        "source": "does_not_exist",
                        "bindings": {"axis": "x", "value": "y"},
                        "agg": "sum",
                        "filters": [],
                        "options": {},
                        "grid": {"x": 0, "y": 0, "w": 4, "h": 4},
                    }
                ]
            },
        )
        assert invalid_layout.status_code == 400

        payment = member.post(
            "/api/v1/billing/requests",
            headers=csrf(member),
            json={"plan_code": "daily"},
        )
        assert payment.status_code == 200, payment.text
        payment_data = payment.json()
        assert payment_data["request"]["status"] == "pending"
        assert payment_data["notification"]["configured"] is False
        assert "운영자의 알림 설정이 이뤄지지 않았습니다." in payment_data["detail"]
        duplicate_payment = member.post(
            "/api/v1/billing/requests",
            headers=csrf(member),
            json={"plan_code": "weekly"},
        )
        assert duplicate_payment.status_code == 409

        review = admin.post(
            f"/api/v1/admin/payments/{payment_data['request']['id']}/review",
            headers=csrf(admin),
            json={"approve": True, "note": "테스트 승인"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["status"] == "approved"
        membership_end = member.get("/api/v1/me").json()["membership_ends_at"]
        assert membership_end is not None
        repeated_review = admin.post(
            f"/api/v1/admin/payments/{payment_data['request']['id']}/review",
            headers=csrf(admin),
            json={"approve": True, "note": "중복 승인"},
        )
        assert repeated_review.status_code == 409
        assert member.get("/api/v1/me").json()["membership_ends_at"] == membership_end

        register(operator, "operator@example.com", OPERATOR_PASSWORD)
        operator_row = next(
            item
            for item in admin.get("/api/v1/admin/users").json()["items"]
            if item["email"] == "operator@example.com"
        )
        operator_approve = admin.patch(
            f"/api/v1/admin/users/{operator_row['id']}",
            headers=csrf(admin),
            json={"role": "operator", "status": "active"},
        )
        assert operator_approve.status_code == 200
        login(operator, "operator@example.com", OPERATOR_PASSWORD)
        admin_user_policy = admin.post(
            "/api/v1/admin/policies",
            headers=csrf(admin),
            json={
                "name": "관리자 개인 온톨로지",
                "policy_type": "ontology",
                "scope_type": "user",
                "scope_user_public_id": admin_id,
                "config": {"hidden_chart_types": []},
            },
        )
        assert admin_user_policy.status_code == 200
        operator_policies = operator.get("/api/v1/admin/policies")
        assert operator_policies.status_code == 200
        assert admin_user_policy.json()["id"] not in {
            item["id"] for item in operator_policies.json()
        }
        cannot_patch_admin_policy = operator.patch(
            f"/api/v1/admin/policies/{admin_user_policy.json()['id']}",
            headers=csrf(operator),
            json={"active": False},
        )
        assert cannot_patch_admin_policy.status_code == 403
        operator_layout_ids = {
            item["id"] for item in operator.get("/api/v1/charts/layouts").json()
        }
        assert personal_layout_id not in operator_layout_ids
        visible_to_operator = operator.get("/api/v1/admin/users").json()["items"]
        assert all(item["role"] in {"guest", "member"} for item in visible_to_operator)
        cannot_manage_admin = operator.patch(
            f"/api/v1/admin/users/{admin_id}",
            headers=csrf(operator),
            json={"status": "suspended"},
        )
        assert cannot_manage_admin.status_code == 403
        operator_override = admin.put(
            f"/api/v1/admin/access/users/{operator_row['id']}",
            headers=csrf(admin),
            json={"permissions": [{"page_key": "admin_access", "allowed": True}]},
        )
        assert operator_override.status_code == 200
        downgraded = admin.patch(
            f"/api/v1/admin/users/{operator_row['id']}",
            headers=csrf(admin),
            json={"role": "member", "status": "active"},
        )
        assert downgraded.status_code == 200
        assert "admin_access" not in downgraded.json()["allowed_pages"]
        assert operator.get("/api/v1/admin/access").status_code == 401

        invalid_policy = admin.post(
            "/api/v1/admin/policies",
            headers=csrf(admin),
            json={
                "name": "잘못된 한도",
                "policy_type": "request_limit",
                "scope_type": "role",
                "scope_role": "member",
                "config": {"authenticated": {"minute": "many"}},
            },
        )
        assert invalid_policy.status_code == 400

        override = admin.put(
            f"/api/v1/admin/access/users/{member_id}",
            headers=csrf(admin),
            json={"permissions": [{"page_key": "charts", "allowed": False}]},
        )
        assert override.status_code == 200
        assert "charts" not in override.json()["allowed_pages"]
        assert member.get("/api/v1/charts/meta").status_code == 403
        assert member.get("/api/v1/catalog/tables").status_code == 200

        audit_events = admin.get("/api/v1/admin/audit?limit=20")
        assert audit_events.status_code == 200
        assert audit_events.json()["items"]
        assert all(
            len(item["ip_fingerprint"]) <= 10
            for item in audit_events.json()["items"]
        )

        headers = member.get("/profile").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in headers
        assert "script-src 'self' 'unsafe-inline'" not in headers["content-security-policy"]
        assert "script-src-attr 'none'" in headers["content-security-policy"]
        assert "'sha256-" not in headers["content-security-policy"]
        assert "'sha256-" in landing.headers["content-security-policy"]
        assert headers["cross-origin-opener-policy"] == "same-origin"
        logout = member.post("/api/v1/auth/logout", headers=csrf(member))
        assert logout.status_code == 200
        assert "clear-site-data" in logout.headers
        assert member.get("/api/v1/auth/session").json()["authenticated"] is False


def test_health_reports_database_readiness():
    with TestClient(app, client=("198.51.100.99", 50000)) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["database"] == "ok"


def test_health_fails_closed_when_database_is_unavailable():
    class UnavailableDatabase:
        class SessionContext:
            def __enter__(self):
                raise SQLAlchemyError("test database unavailable")

            def __exit__(self, *_args):
                return False

        def session(self):
            return self.SessionContext()

    with TestClient(
        app,
        client=("198.51.100.98", 50000),
        raise_server_exceptions=False,
    ) as client:
        original = app.state.database
        app.state.database = UnavailableDatabase()
        try:
            response = client.get("/health")
        finally:
            app.state.database = original
        assert response.status_code == 503
        assert response.json()["title"] == "database unavailable"


def test_health_route_rejects_missing_schema(tmp_path):
    database = Database(
        f"sqlite:///{tmp_path / 'missing-schema.db'}",
        enable_sqlite_wal=False,
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(database=database),
        )
    )
    response = health(request)
    assert response.status_code == 503
    assert json.loads(response.body)["title"] == "database unavailable"


def test_failed_logins_are_committed_and_account_is_locked():
    with TestClient(app):
        with app.state.database.session() as db:
            user = User(
                email="locked@example.com",
                password_hash=hash_password("Correct-Harbor-Password-2026!"),
                nickname="잠금테스트-사용자",
                role="member",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
            )
            db.add(user)
            db.flush()
            user_id = user.id

        with app.state.database.Session() as db:
            service = AuthService(db, app.state.auth_settings)
            for _ in range(5):
                try:
                    service.login(
                        "locked@example.com",
                        "wrong-password",
                        remember=False,
                        ip_hash="test-ip",
                        user_agent_hash="test-agent",
                    )
                except DomainError as exc:
                    assert exc.status == 401
                else:
                    raise AssertionError("잘못된 비밀번호 로그인이 성공했습니다.")

        with app.state.database.session() as db:
            user = db.get(User, user_id)
            assert user.locked_until is not None
            assert user.locked_until > utcnow()
            assert db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.event_type == "login_failed",
                    AuditLog.target_user_id == user_id,
                )
            ) == 5


def test_correct_password_clears_failures_for_pending_account():
    email = "pending-reset@example.com"
    password = "Pending-Harbor-Password-2026!"
    with TestClient(app, client=("198.51.100.41", 50000)) as client:
        with app.state.database.session() as db:
            user = User(
                email=email,
                password_hash=hash_password(password),
                nickname="승인대기-사용자",
                role="guest",
                status="pending",
                failed_login_count=4,
            )
            db.add(user)
            db.flush()
            user_id = user.id

        response = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert response.status_code == 403
        with app.state.database.Session() as db:
            pending = db.get(User, user_id)
            assert pending.failed_login_count == 0
            assert pending.locked_until is None
            assert db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.target_user_id == user_id,
                    AuditLog.event_type == "login_blocked_account_status",
                )
            ) == 1


def test_email_verification_rejects_query_token_and_post_is_single_use():
    with TestClient(app) as client:
        with app.state.database.session() as db:
            user = User(
                email="verify@example.com",
                password_hash=hash_password("Verify-Ginkgo-Password-2026!"),
                nickname="인증테스트-사용자",
                role="guest",
                status="pending",
            )
            db.add(user)
            db.flush()
            user_id = user.id
            token = AuthService(db, app.state.auth_settings)._issue_token(
                user, "verify_email", 30
            )

        query_attempt = client.get(
            f"/api/v1/auth/verify-email?token={token}", follow_redirects=False
        )
        assert query_attempt.status_code == 405
        with app.state.database.session() as db:
            row = db.scalar(
                select(AccountToken).where(
                    AccountToken.user_id == user_id,
                    AccountToken.purpose == "verify_email",
                )
            )
            assert row.consumed_at is None

        verified = client.post(
            "/api/v1/auth/verify-email", json={"token": token}
        )
        assert verified.status_code == 200
        reused = client.post(
            "/api/v1/auth/verify-email", json={"token": token}
        )
        assert reused.status_code == 400
        with app.state.database.session() as db:
            user = db.get(User, user_id)
            assert user.email_verified_at is not None


def test_streamed_request_body_limit_cannot_bypass_content_length():
    def oversized_body():
        yield b"{" + b"x" * (app.state.auth_settings.max_request_bytes + 1) + b"}"

    with TestClient(app, client=("198.51.100.34", 50000)) as client:
        response = client.post(
            "/api/v1/auth/login",
            content=oversized_body(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413
        assert response.headers["x-content-type-options"] == "nosniff"


def test_totp_matches_rfc_6238_sha1_vector():
    # RFC 6238 Appendix B: SHA-1 secret "12345678901234567890", T=59, 8 digits.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(secret, 59 // 30, digits=8) == "94287082"


def test_mfa_break_glass_verifies_new_factor_before_reset():
    password = "Break-Glass-Harbor-Password-2026!"
    with TestClient(app):
        with app.state.database.session() as db:
            user = User(
                email="break-glass@example.com",
                password_hash=hash_password(password),
                nickname="긴급복구-사용자",
                role="admin",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
                mfa_enabled_at=utcnow(),
                mfa_seed_salt="existing-seed-salt",
            )
            db.add(user)
            db.flush()
            user_id = user.id

        new_salt = random_token(24)
        with app.state.database.Session() as db:
            public_id = db.get(User, user_id).public_id
        secret = derive_totp_secret(
            app.state.auth_settings.mfa_master_key,
            public_id,
            new_salt,
        )
        correct = totp_code(secret, int(time.time() // 30))
        wrong = correct[:-1] + str((int(correct[-1]) + 1) % 10)
        with app.state.database.Session() as db:
            user = db.get(User, user_id)
            service = AuthService(db, app.state.auth_settings)
            expected_state_digest = service.mfa_security_state_digest(user)
            with pytest.raises(DomainError):
                service.force_reenroll_mfa(
                    user,
                    actor=user,
                    setup_salt=new_salt,
                    code=wrong,
                    reason="운영자 본인 확인 후 긴급 MFA 재등록 테스트",
                    ip_hash="cli",
                    expected_state_digest=expected_state_digest,
                )
            db.rollback()

        with app.state.database.session() as db:
            user = db.get(User, user_id)
            assert user.mfa_seed_salt == "existing-seed-salt"
            recovery_codes = AuthService(
                db, app.state.auth_settings
            ).force_reenroll_mfa(
                user,
                actor=user,
                setup_salt=new_salt,
                code=correct,
                reason="운영자 본인 확인 후 긴급 MFA 재등록 테스트",
                ip_hash="cli",
                expected_state_digest=expected_state_digest,
            )
            assert len(recovery_codes) == 10
            assert user.mfa_seed_salt == new_salt


def test_mfa_break_glass_rejects_stale_security_state():
    password = "Break-Glass-State-Password-2026!"
    with TestClient(app):
        with app.state.database.session() as db:
            user = User(
                email="break-glass-stale@example.com",
                password_hash=hash_password(password),
                nickname="긴급복구-상태검증",
                role="admin",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
                mfa_enabled_at=utcnow(),
                mfa_seed_salt="existing-stale-seed",
            )
            db.add(user)
            db.flush()
            user_id = user.id
            service = AuthService(db, app.state.auth_settings)
            expected_state_digest = service.mfa_security_state_digest(user)
            public_id = user.public_id

        new_salt = random_token(24)
        secret = derive_totp_secret(
            app.state.auth_settings.mfa_master_key,
            public_id,
            new_salt,
        )
        code = totp_code(secret, int(time.time() // 30))

        with app.state.database.session() as db:
            user = db.get(User, user_id)
            user.password_hash = hash_password("Changed-State-Password-2026!")
            user.password_changed_at = utcnow()

        with app.state.database.session() as db:
            user = db.get(User, user_id)
            with pytest.raises(DomainError, match="보안 상태가 변경"):
                AuthService(db, app.state.auth_settings).force_reenroll_mfa(
                    user,
                    actor=user,
                    setup_salt=new_salt,
                    code=code,
                    reason="입력 대기 중 보안 상태 변경 감지 테스트",
                    ip_hash="cli",
                    expected_state_digest=expected_state_digest,
                )

        with app.state.database.session() as db:
            user = db.get(User, user_id)
            assert user.mfa_seed_salt == "existing-stale-seed"


def test_admin_can_reset_lower_role_mfa_with_audited_reason():
    with TestClient(app, client=("198.51.100.42", 50000)) as admin:
        login(admin, "root@example.com", ADMIN_PASSWORD)
        with app.state.database.session() as db:
            target = User(
                email="mfa-reset-member@example.com",
                password_hash=hash_password("Member-Mfa-Reset-Password-2026!"),
                nickname="MFA초기화-회원",
                role="member",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
                mfa_enabled_at=utcnow(),
                mfa_seed_salt="member-seed-salt",
            )
            db.add(target)
            db.flush()
            target_id = target.id
            target_public_id = target.public_id
            db.add(
                MfaRecoveryCode(
                    user_id=target.id,
                    code_hash="f" * 64,
                )
            )

        short_reason = admin.post(
            f"/api/v1/admin/users/{target_public_id}/mfa-reset",
            headers=csrf(admin),
            json={"reason": "짧음"},
        )
        assert short_reason.status_code == 422
        reset = admin.post(
            f"/api/v1/admin/users/{target_public_id}/mfa-reset",
            headers=csrf(admin),
            json={"reason": "고객센터 본인 확인과 등록 이메일 확인 완료"},
        )
        assert reset.status_code == 200

        with app.state.database.session() as db:
            target = db.get(User, target_id)
            assert target.mfa_enabled_at is None
            assert target.mfa_seed_salt is None
            assert db.scalar(
                select(func.count(MfaRecoveryCode.id)).where(
                    MfaRecoveryCode.user_id == target_id
                )
            ) == 0
            assert db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.target_user_id == target_id,
                    AuditLog.event_type == "mfa_force_reset",
                )
            ) == 1


def test_registration_requires_consent_and_hides_duplicate_account():
    email = "consent@example.com"
    password = "Willow-Harbor-Secure-2026!"
    with TestClient(app, client=("198.51.100.30", 50000)) as client:
        missing = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "terms_accepted": False},
        )
        assert missing.status_code == 400

        first = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "terms_accepted": True},
        )
        duplicate = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "terms_accepted": True},
        )
        assert first.status_code == duplicate.status_code == 200
        assert first.json() == duplicate.json()
        with app.state.database.session() as db:
            user = db.scalar(select(User).where(User.email == email))
            assert user.terms_accepted_at is not None
            assert user.terms_version == "2026-07-17"


def test_session_inventory_and_revoke_others():
    email = "sessions@example.com"
    password = "Sessions-Cedar-Secure-2026!"
    with TestClient(app) as seed:
        with app.state.database.session() as db:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(password),
                    nickname="세션관리-테스트",
                    role="member",
                    status="active",
                    email_verified_at=utcnow(),
                    approved_at=utcnow(),
                )
            )
    with TestClient(
        app, client=("198.51.100.31", 50000)
    ) as first, TestClient(
        app, client=("198.51.100.32", 50000)
    ) as second:
        login(first, email, password)
        login(second, email, password)
        sessions = first.get("/api/v1/me/sessions")
        assert sessions.status_code == 200
        assert len(sessions.json()) == 2
        assert sum(1 for row in sessions.json() if row["current"]) == 1
        assert all(row["remembered"] is False for row in sessions.json())

        revoked = first.post(
            "/api/v1/me/sessions/revoke-others", headers=csrf(first)
        )
        assert revoked.status_code == 200
        assert revoked.json()["revoked"] == 1
        assert second.get("/api/v1/auth/session").json()["authenticated"] is False
        assert first.get("/api/v1/auth/session").json()["authenticated"] is True


def test_inactive_session_expires_before_absolute_deadline():
    email = "idle-session@example.com"
    password = "Idle-Session-Willow-2026!"
    with TestClient(app, client=("198.51.100.33", 50000)) as client:
        with app.state.database.session() as db:
            user = User(
                email=email,
                password_hash=hash_password(password),
                nickname="무활동세션-테스트",
                role="member",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
            )
            db.add(user)
            db.flush()
            user_id = user.id
        login(client, email, password)
        with app.state.database.session() as db:
            row = db.scalar(
                select(AuthSession).where(
                    AuthSession.user_id == user_id,
                    AuthSession.revoked_at.is_(None),
                )
            )
            row.last_seen_at = utcnow() - timedelta(
                minutes=app.state.auth_settings.session_idle_minutes + 1
            )
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False
        with app.state.database.session() as db:
            row = db.scalar(
                select(AuthSession).where(AuthSession.user_id == user_id)
            )
            assert row.revoked_at is not None


def test_mfa_enrollment_login_and_recovery_code_is_single_use():
    password = "Maple-Citadel-Secure-2026!"
    email = "mfa-member@example.com"
    with TestClient(app, client=("198.51.100.22", 50000)) as client:
        with app.state.database.session() as db:
            user = User(
                email=email,
                password_hash=hash_password(password),
                nickname="다중인증-테스트",
                role="member",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
            )
            db.add(user)
            db.flush()
            user_id = user.id

        login(client, email, password)
        setup = client.post(
            "/api/v1/me/mfa/setup",
            headers=csrf(client),
            json={"current_password": password, "current_code": ""},
        )
        assert setup.status_code == 200, setup.text
        setup_data = setup.json()
        code = totp_code(setup_data["secret"], int(time.time() // 30))
        confirmed = client.post(
            "/api/v1/me/mfa/confirm",
            headers=csrf(client),
            json={"setup_token": setup_data["setup_token"], "code": code},
        )
        assert confirmed.status_code == 200, confirmed.text
        recovery_codes = confirmed.json()["recovery_codes"]
        assert len(recovery_codes) == 10
        assert client.get("/api/v1/auth/session").json()["authenticated"] is False

        password_factor = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password, "remember": True, "next": "/profile"},
        )
        assert password_factor.status_code == 200, password_factor.text
        assert password_factor.json()["mfa_required"] is True
        challenge = password_factor.json()["challenge"]
        completed = client.post(
            "/api/v1/auth/mfa/verify",
            json={
                "challenge": challenge,
                "code": recovery_codes[0],
                "next": "/profile",
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["authenticated"] is True
        assert completed.json()["next"] == "/profile"
        assert "Max-Age=" in completed.headers.get("set-cookie", "")

        with app.state.database.session() as db:
            assert db.get(User, user_id).mfa_enabled_at is not None
            assert db.scalar(
                select(func.count(MfaRecoveryCode.id)).where(
                    MfaRecoveryCode.user_id == user_id,
                    MfaRecoveryCode.used_at.is_(None),
                )
            ) == 9

        assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 200
        second_password_factor = client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        reused = client.post(
            "/api/v1/auth/mfa/verify",
            json={
                "challenge": second_password_factor.json()["challenge"],
                "code": recovery_codes[0],
            },
        )
        assert reused.status_code == 401


def test_mfa_enrollment_can_be_confirmed_in_same_database_session():
    password = "Harbor-Cedar-Secure-2026!"
    with TestClient(app):
        with app.state.database.session() as db:
            user = User(
                email="mfa-cli-session@example.com",
                password_hash=hash_password(password),
                nickname="CLI-MFA-테스트",
                role="admin",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
            )
            db.add(user)
            db.flush()

            service = AuthService(db, app.state.auth_settings)
            setup = service.begin_mfa_setup(
                user,
                current_password=password,
                current_code="",
                ip_hash="cli",
                user_agent_hash="setup-mfa-cli",
            )
            code = totp_code(setup["secret"], int(time.time() // 30))

            recovery_codes = service.confirm_mfa_setup(
                user,
                setup["setup_token"],
                code,
                ip_hash="cli",
                user_agent_hash="setup-mfa-cli",
            )

            assert user.mfa_enabled_at is not None
            assert len(recovery_codes) == 10


def test_cli_mfa_enrollment_commits_before_prompt_and_retries(monkeypatch):
    password = "Maple-Quartz-Secure-2026!"
    with TestClient(app):
        with app.state.database.session() as db:
            user = User(
                email="mfa-cli-retry@example.com",
                password_hash=hash_password(password),
                nickname="CLI-MFA-재시도",
                role="admin",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
            )
            db.add(user)
            db.flush()
            user_id = user.id
            service = AuthService(db, app.state.auth_settings)
            setup = service.begin_mfa_setup(
                user,
                current_password=password,
                current_code="",
                ip_hash="cli",
                user_agent_hash="setup-mfa-cli",
            )
            expected_state_digest = service.mfa_security_state_digest(user)

        current_code = totp_code(setup["secret"], int(time.time() // 30))
        entered_codes = iter(("not-six-digits", current_code))
        monkeypatch.setattr(
            "scripts.setup_mfa.getpass.getpass",
            lambda _prompt: next(entered_codes),
        )

        recovery_codes = _confirm_setup(
            app.state.database,
            app.state.auth_settings,
            user_id=user_id,
            setup=setup,
            break_glass=False,
            expected_state_digest=expected_state_digest,
        )

        assert len(recovery_codes) == 10
        with app.state.database.session() as db:
            user = db.get(User, user_id)
            challenge = db.scalar(
                select(MfaChallenge).where(
                    MfaChallenge.user_id == user_id,
                    MfaChallenge.purpose == "setup",
                )
            )
            assert user.mfa_enabled_at is not None
            assert challenge.attempts == 1
            assert challenge.consumed_at is not None


def test_payment_notification_outbox_claim_is_single_delivery():
    class FakeNotifier:
        def __init__(self):
            self.calls = 0

        def configuration(self):
            return NotificationResult(
                (DeliveryResult("slack", True, False, "webhook"),)
            )

        def send(self, _message):
            self.calls += 1
            return NotificationResult(
                (
                    DeliveryResult("discord", False, False),
                    DeliveryResult("slack", True, True, "webhook"),
                    DeliveryResult("telegram", False, False),
                )
            )

    notifier = FakeNotifier()
    with TestClient(app):
        with app.state.database.session() as db:
            user = User(
                email="outbox@example.com",
                password_hash=hash_password("Outbox-Harbor-Secure-2026!"),
                nickname="알림아웃박스-테스트",
                role="member",
                status="active",
                email_verified_at=utcnow(),
                approved_at=utcnow(),
            )
            db.add(user)
            db.flush()
            row, result = PaymentService(db, notifier).request(
                user, "daily", "outbox-ip"
            )
            request_id = row.public_id
            assert result["queued"] is True

        deliver_payment_notification(app.state.database, request_id, notifier)
        deliver_payment_notification(app.state.database, request_id, notifier)
        assert notifier.calls == 1
        with app.state.database.session() as db:
            row = db.scalar(
                select(PaymentRequest).where(
                    PaymentRequest.public_id == request_id
                )
            )
            deliveries = db.scalars(
                select(NotificationDelivery).where(
                    NotificationDelivery.payment_request_id == row.id
                )
            ).all()
            assert row.notification_result["delivered"] is True
            assert [item.status for item in deliveries] == ["delivered"]
            assert deliveries[0].attempts == 1

            actor = db.scalar(select(User).where(User.email == "root@example.com"))
            PaymentService(db, notifier).requeue_notification(
                actor, request_id, "outbox-admin-ip"
            )

        deliver_payment_notification(app.state.database, request_id, notifier)
        assert notifier.calls == 2
        with app.state.database.session() as db:
            row = db.scalar(
                select(PaymentRequest).where(
                    PaymentRequest.public_id == request_id
                )
            )
            deliveries = db.scalars(
                select(NotificationDelivery)
                .where(NotificationDelivery.payment_request_id == row.id)
                .order_by(NotificationDelivery.id)
            ).all()
            assert [item.status for item in deliveries] == [
                "delivered",
                "delivered",
            ]
