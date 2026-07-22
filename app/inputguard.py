"""입력 표준 필터 정본 — 모든 API 입력부는 여기 선언된 필터를 거친다.

사상: 입력 검증을 핸들러마다 재발명하지 않는다. 필터 어휘(아래)를 표준으로 정하고,
**모든 라우트×파라미터를 ROUTE_INPUT_FILTERS 에 명시**한다. tests/test_input_guard.py 가
실제 FastAPI 라우트를 인트로스펙션해 이 매핑과 대조하므로, **새 입력부를 만들면 반드시
여기 등록해야 테스트가 통과한다**(누락 = CI 실패). 규약 본문: SHARE.md §9.

필터 어휘(집행 지점):
  NONE            입력 없음(정적 페이지·세션 파생 GET).
  PYDANTIC:<S>    요청 본문이 pydantic 스키마 <S> 검증을 통과(타입·길이·패턴·field_validator).
  REGISTRY        화이트리스트 조회 — 존재하지 않으면 404. (카탈로그 스냅샷 테이블명,
                  온톨로지 소스/필드명, 사용자 소유 레이아웃 id). SQL 로 가는 식별자는
                  추가로 querybuilder 의 화이트리스트+quote 를 통과한다.
  IDENT           SQL 식별자 패턴 ^[A-Za-z_][A-Za-z0-9_]*$ — 레지스트리 조회 전 1차 방어.
  SAFE_SEGMENT    id/슬러그 ^[A-Za-z0-9_-]+$ (경로 조작·DOM 주입 문자 원천 차단).
  SAFE_TEXT       사람이 읽는 텍스트(제목·이름) — 제어문자(개행 포함) 금지, 길이 제한.
                  표시 시 프론트 esc() 이스케이프와 이중 방어.
  ENUM            고정 화이트리스트(op/agg/domain/status 등) — 목록 밖 즉시 거부.
  INT             정수(+범위) — FastAPI 타입 강제(비정수 422).
  BOOL            불리언 — FastAPI 타입 강제.
  NUMBER          유한 숫자(범위) — bin 폭 등. querybuilder._bin_width 가 이중 검증.
  LITERAL         SQL 리터럴 값 — 제어문자 거부 + '' 이스케이프(querybuilder._lit)
                  또는 ORM 바인드 파라미터. 문자열이 식별자로 승격되는 일 없음.
  OPAQUE          토큰/비밀 류 — 내용 검증은 해시 비교(auth service), 로그 금지.

SQL 경계 불변식(charts): 식별자=레지스트리 화이트리스트+quote, 값=LITERAL/바인딩,
연산자·집계=ENUM — 사용자 문자열이 SQL 구조로 승격되는 경로가 존재하지 않는다.
필터 트리(2026-07-23 확장)도 동일 경계 안이다: 재귀 그룹은 leaf 마다 같은 화이트리스트
(_condition), 그룹 결합은 괄호 + ENUM logic(GROUP_LOGICS), HAVING 집계는 SELECT 와
동일한 _measure_expr 화이트리스트를 재사용하며, 트리 구조 자체는 2단 강제 +
MAX_FILTER_LEAVES(50)·MAX_GROUP_CONDITIONS(20) 상한(NUMBER/INT 류 — _bin_width 와
같은 이중검증 방식)으로 봉인된다. contains 계열의 LIKE 이스케이프 문자는 서버 상수다.
auth 경계 불변식: 전 쿼리 SQLAlchemy 바인드 파라미터(정적 감사 no_sql_text_injection).
"""
from __future__ import annotations

import re

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def is_ident(value: str, max_length: int = 120) -> bool:
    return isinstance(value, str) and len(value) <= max_length and bool(IDENT_RE.fullmatch(value))


def is_safe_segment(value: str, max_length: int = 80) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= max_length
            and bool(SAFE_SEGMENT_RE.fullmatch(value)))


def assert_safe_text(value: str, *, field: str = "text", max_length: int = 120) -> str:
    """표시용 텍스트 표준 필터 — 제어문자·과길이 거부(내용은 보존, 표시는 esc 책임)."""
    if not isinstance(value, str):
        raise ValueError(f"{field}: 문자열이어야 합니다")
    if len(value) > max_length:
        raise ValueError(f"{field}: {max_length}자 이하여야 합니다")
    if _CONTROL_RE.search(value):
        raise ValueError(f"{field}: 제어문자는 쓸 수 없습니다")
    return value


# ── 라우트 × 입력 → 표준 필터 매핑 (정본) ─────────────────────────
# 키: "METHOD /path" (FastAPI 라우트 경로 그대로). 값: {입력: 필터}.
#   본문은 "body" 하나로 선언하고 PYDANTIC:<스키마명> 을 지정한다(스키마 안의 필드 규칙이
#   상세 필터). 경로/쿼리는 "path:<이름>"/"query:<이름>".
ROUTE_INPUT_FILTERS: dict[str, dict[str, str]] = {
    # ── 정적 페이지·무입력 GET ──
    "GET /": {}, "GET /home": {}, "GET /catalog": {}, "GET /charts": {},
    "GET /docs": {}, "GET /health": {}, "GET /profile": {}, "GET /admin": {},
    "GET /legal/terms": {}, "GET /legal/privacy": {},
    "GET /auth/login": {}, "GET /auth/register": {}, "GET /auth/mfa": {},
    "GET /auth/resend-verification": {}, "GET /auth/forgot-password": {},
    "GET /auth/reset-password": {}, "GET /auth/verify-email": {},

    # ── 공개 카탈로그(스냅샷 서빙 — SQL 없음) ──
    "GET /api/v1/public/summary": {},
    "GET /api/v1/catalog/snapshot": {},
    "GET /api/v1/catalog/tables": {"query:external": "BOOL"},
    "GET /api/v1/catalog/tables/{name}": {"path:name": "REGISTRY"},
    "GET /api/v1/catalog/tables/{name}/schema": {"path:name": "REGISTRY"},
    "GET /api/v1/catalog/tables/{name}/quality": {"path:name": "REGISTRY"},
    "GET /api/v1/catalog/tables/{name}/sample": {"path:name": "REGISTRY"},

    # ── Charts Studio (SQL 경계 — querybuilder 화이트리스트/이스케이프) ──
    "GET /api/v1/charts/meta": {},
    "GET /api/v1/charts/sources": {"query:domain": "ENUM"},
    "GET /api/v1/charts/sources/{name}": {"path:name": "IDENT+REGISTRY"},
    "GET /api/v1/charts/sources/{name}/availability": {"path:name": "IDENT+REGISTRY"},
    "POST /api/v1/charts/query": {"body": "PYDANTIC:QueryRequest"},
    "GET /api/v1/charts/layouts": {},
    "POST /api/v1/charts/layouts": {"body": "PYDANTIC:PageCreate"},
    "GET /api/v1/charts/layouts/{page_id}": {"path:page_id": "SAFE_SEGMENT+REGISTRY"},
    "PATCH /api/v1/charts/layouts/{page_id}": {
        "path:page_id": "SAFE_SEGMENT+REGISTRY", "body": "PYDANTIC:PagePatch"},
    "DELETE /api/v1/charts/layouts/{page_id}": {"path:page_id": "SAFE_SEGMENT+REGISTRY"},
    "POST /api/v1/charts/layouts/{page_id}/duplicate": {"path:page_id": "SAFE_SEGMENT+REGISTRY"},
    "POST /api/v1/charts/layouts-reorder": {"body": "PYDANTIC:ReorderRequest"},

    # ── 인증·계정 (SQLAlchemy 바인드 파라미터 — no_sql_text_injection 감사) ──
    "POST /api/v1/auth/register": {"body": "PYDANTIC:RegisterRequest"},
    "POST /api/v1/auth/login": {"body": "PYDANTIC:LoginRequest"},
    "POST /api/v1/auth/logout": {},
    "GET /api/v1/auth/session": {},
    "POST /api/v1/auth/mfa/verify": {"body": "PYDANTIC:MfaLoginRequest"},
    "POST /api/v1/auth/resend-verification": {"body": "PYDANTIC:ResendVerificationRequest"},
    "POST /api/v1/auth/verify-email": {"body": "PYDANTIC:VerifyEmailRequest"},
    "POST /api/v1/auth/forgot-password": {"body": "PYDANTIC:ForgotPasswordRequest"},
    "POST /api/v1/auth/reset-password": {"body": "PYDANTIC:ResetPasswordRequest"},
    "GET /api/v1/me": {},
    "PATCH /api/v1/me/nickname": {"body": "PYDANTIC:NicknamePatch"},
    "PATCH /api/v1/me/password": {"body": "PYDANTIC:PasswordPatch"},
    "GET /api/v1/me/sessions": {},
    "DELETE /api/v1/me/sessions/{session_id}": {"path:session_id": "SAFE_SEGMENT+REGISTRY"},
    "POST /api/v1/me/sessions/revoke-others": {},
    "GET /api/v1/me/mfa": {},
    "POST /api/v1/me/mfa/setup": {"body": "PYDANTIC:MfaSetupRequest"},
    "POST /api/v1/me/mfa/confirm": {"body": "PYDANTIC:MfaConfirmRequest"},
    "POST /api/v1/me/mfa/recovery-codes": {"body": "PYDANTIC:MfaManageRequest"},
    "DELETE /api/v1/me/mfa": {"body": "PYDANTIC:MfaManageRequest"},
    "GET /api/v1/me/preferences": {},
    "PUT /api/v1/me/preferences": {"body": "PYDANTIC:PreferencePatch"},
    "GET /api/v1/billing/plans": {},
    "GET /api/v1/billing/summary": {},
    "GET /api/v1/billing/requests": {},
    "POST /api/v1/billing/requests": {"body": "PYDANTIC:PaymentCreate"},

    # ── 관리자 ──
    "GET /api/v1/admin/users": {
        "query:status": "ENUM", "query:q": "LITERAL",
        "query:before": "SAFE_SEGMENT", "query:limit": "INT"},
    "PATCH /api/v1/admin/users/{public_id}": {
        "path:public_id": "SAFE_SEGMENT+REGISTRY", "body": "PYDANTIC:UserAdminPatch"},
    "POST /api/v1/admin/users/{public_id}/mfa-reset": {
        "path:public_id": "SAFE_SEGMENT+REGISTRY", "body": "PYDANTIC:MfaAdminResetRequest"},
    "GET /api/v1/admin/access": {},
    "PUT /api/v1/admin/access/roles/{role}": {
        "path:role": "ENUM", "body": "PYDANTIC:PermissionPatch"},
    "PUT /api/v1/admin/access/users/{public_id}": {
        "path:public_id": "SAFE_SEGMENT+REGISTRY", "body": "PYDANTIC:PermissionPatch"},
    "GET /api/v1/admin/policies": {},
    "POST /api/v1/admin/policies": {"body": "PYDANTIC:PolicyCreate"},
    "PATCH /api/v1/admin/policies/{policy_id}": {
        "path:policy_id": "INT+REGISTRY", "body": "PYDANTIC:PolicyPatch"},
    "GET /api/v1/admin/ip-blocks": {},
    "POST /api/v1/admin/ip-blocks": {"body": "PYDANTIC:IpBlockCreate"},
    "DELETE /api/v1/admin/ip-blocks/{block_id}": {"path:block_id": "INT+REGISTRY"},
    "GET /api/v1/admin/auto-blocks/ips": {"query:page": "INT", "query:size": "INT"},
    "GET /api/v1/admin/auto-blocks/users": {"query:page": "INT", "query:size": "INT"},
    "POST /api/v1/admin/auto-blocks/ips/{block_id}/release": {"path:block_id": "INT+REGISTRY"},
    "POST /api/v1/admin/auto-blocks/users/{block_id}/release": {"path:block_id": "INT+REGISTRY"},
    "GET /api/v1/admin/payments": {"query:status": "ENUM"},
    "POST /api/v1/admin/payments/{request_id}/review": {
        "path:request_id": "INT+REGISTRY", "body": "PYDANTIC:PaymentReview"},
    "POST /api/v1/admin/payments/{request_id}/notify": {"path:request_id": "INT+REGISTRY"},
    "GET /api/v1/admin/audit": {
        "query:event_type": "SAFE_SEGMENT", "query:before_id": "INT", "query:limit": "INT"},
}
