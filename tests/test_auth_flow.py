from __future__ import annotations

import os
import tempfile


DB_PATH = tempfile.mktemp(prefix="ask-seoul-auth-", suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["AUTH_SESSION_PEPPER"] = "test-only-session-pepper-with-enough-entropy"
os.environ["AUTH_PUBLIC_BASE_URL"] = "http://testserver"
os.environ["AUTH_ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
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

from app.main import app


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
        "/api/v1/auth/register", json={"email": email, "password": password}
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
        all_users = admin.get("/api/v1/admin/users").json()
        admin_id = next(item["id"] for item in all_users if item["role"] == "admin")
        member_row = next(item for item in all_users if item["email"] == "member@example.com")
        member_id = member_row["id"]
        approve = admin.patch(
            f"/api/v1/admin/users/{member_id}",
            headers=csrf(admin),
            json={"role": "guest", "status": "active"},
        )
        assert approve.status_code == 200, approve.text

        user_login = login(member, "member@example.com", USER_PASSWORD)
        assert user_login["user"]["role_label"] == "게스트"
        assert user_login["user"]["masked_id"].startswith(member_id[:4])
        assert member.get("/catalog").status_code == 200
        assert member.get("/api/v1/catalog/tables").status_code == 200
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
        login(member, "member@example.com", USER_PASSWORD)
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

        review = admin.post(
            f"/api/v1/admin/payments/{payment_data['request']['id']}/review",
            headers=csrf(admin),
            json={"approve": True, "note": "테스트 승인"},
        )
        assert review.status_code == 200, review.text
        assert review.json()["status"] == "approved"
        assert member.get("/api/v1/me").json()["membership_ends_at"] is not None

        register(operator, "operator@example.com", OPERATOR_PASSWORD)
        operator_row = next(
            item
            for item in admin.get("/api/v1/admin/users").json()
            if item["email"] == "operator@example.com"
        )
        operator_approve = admin.patch(
            f"/api/v1/admin/users/{operator_row['id']}",
            headers=csrf(admin),
            json={"role": "operator", "status": "active"},
        )
        assert operator_approve.status_code == 200
        login(operator, "operator@example.com", OPERATOR_PASSWORD)
        assert operator.get("/api/v1/admin/policies").status_code == 200
        operator_layout_ids = {
            item["id"] for item in operator.get("/api/v1/charts/layouts").json()
        }
        assert personal_layout_id not in operator_layout_ids
        visible_to_operator = operator.get("/api/v1/admin/users").json()
        assert all(item["role"] in {"guest", "member"} for item in visible_to_operator)
        cannot_manage_admin = operator.patch(
            f"/api/v1/admin/users/{admin_id}",
            headers=csrf(operator),
            json={"status": "suspended"},
        )
        assert cannot_manage_admin.status_code == 403

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

        headers = member.get("/profile").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in headers
        logout = member.post("/api/v1/auth/logout", headers=csrf(member))
        assert logout.status_code == 200
        assert "clear-site-data" in logout.headers
        assert member.get("/api/v1/auth/session").json()["authenticated"] is False
