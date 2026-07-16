# 인증·권한 RDB 스키마

## 지원 원칙

모델은 SQLAlchemy 공통 타입과 FK/unique/index 계약으로 작성되어 특정 DB SQL에 종속되지 않는다.
자동 테스트에서 SQLite, PostgreSQL, MySQL dialect의 모든 table/index DDL 컴파일을 검증한다.
기본 드라이버는 SQLite 내장, PostgreSQL `psycopg`, MySQL/MariaDB `PyMySQL`이다.

```dotenv
# SQLite
DATABASE_URL=sqlite:///./data/ask_seoul.db
# PostgreSQL
DATABASE_URL=postgresql+psycopg://user:password@db:5432/ask_seoul
# MySQL/MariaDB
DATABASE_URL=mysql+pymysql://user:password@db:3306/ask_seoul?charset=utf8mb4
```

Oracle, SQL Server 등도 SQLAlchemy dialect/driver를 추가해 같은 공통 모델을 사용할 수 있지만,
운영 채택 전 해당 DB 버전에서 DDL·FK cascade·JSON·시간대·동시성 통합 테스트를 별도로 통과시켜야 한다.
SQLAlchemy의 dialect 구조는 [공식 문서](https://docs.sqlalchemy.org/en/20/dialects/index.html)를 참고한다.

## 관계

```text
auth_users
 ├─< auth_sessions
 ├─< auth_account_tokens
 ├─1 auth_user_preferences
 ├─< auth_user_page_permissions >─ auth_page_resources
 ├─< auth_dashboard_layouts
 ├─< auth_payment_requests >─ auth_payment_plans
 └─< auth_access_policies(scope_user_id)

auth_page_resources ─< auth_role_page_permissions
auth_payment_requests ─< auth_notification_deliveries
auth_users ─< auth_audit_logs(actor_user_id, target_user_id)
auth_users ─< auth_ip_blocks(created_by_id)
```

## 테이블 책임

| 테이블 | 책임 |
|---|---|
| `auth_users` | 로그인 식별자, 해시, 역할, 상태, 승인, 이용권 |
| `auth_sessions` | 해시된 세션/CSRF, 만료·폐기, IP/UA 해시 |
| `auth_account_tokens` | 이메일 인증·비밀번호 재설정 1회용 토큰 해시 |
| `auth_page_resources` | 보호할 화면/API 논리 리소스 |
| `auth_role_page_permissions` | 역할별 초기 접근 |
| `auth_user_page_permissions` | 사용자별 allow/deny override |
| `auth_user_preferences` | 개인 온톨로지·UI 설정 |
| `auth_access_policies` | system/role/user 공통·개인 정책 |
| `auth_ip_blocks` | CIDR 차단과 만료 |
| `auth_payment_plans` | 일/주/월/연 요금제 |
| `auth_payment_requests` | 모의 결제 요청·검토·승인 |
| `auth_dashboard_layouts` | 사용자별 Charts Studio 페이지/차트 |
| `auth_notification_deliveries` | 채널별 알림 결과 |
| `auth_audit_logs` | 중요 보안·관리 이벤트 append-only 이력 |

## 키·인덱스·시퀀스

- 내부 조인은 정수 PK/FK를 사용하고, 외부 API에는 추측이 어려운 UUID `public_id`만 노출한다.
- 정수 `autoincrement` PK는 DB dialect에 따라 PostgreSQL sequence/SERIAL 계열,
  MySQL `AUTO_INCREMENT`, SQLite `INTEGER PRIMARY KEY`로 렌더링된다.
  자세한 매핑은 [SQLAlchemy MetaData 문서](https://docs.sqlalchemy.org/en/21/core/metadata.html)를 참고한다.
- 사용자 이메일/닉네임/public UUID, 세션·계정 토큰 해시는 unique다.
- 상태+역할, 이용권 만료, 활성 세션, 정책 적용 순서, 결제 상태+요청일, 사용자+레이아웃 순서,
  감사 이벤트+시간에 복합 인덱스를 둔다.
- 역할/사용자 페이지 권한은 `(role,page_id)`, `(user_id,page_id)` unique로 중복을 막는다.

## 초기화·운영

```bash
python3 scripts/init_auth_db.py
python3 scripts/create_admin.py --email admin@example.com
python3 -m pytest -q tests/test_database_portability.py
```

현재 `auth_schema_version`은 예상하지 못한 스키마 버전으로 자동 기동하는 것을 막는다.
운영 스키마 변경은 Alembic 같은 명시적 migration 도구를 붙여 expand → deploy → contract 순서로 수행한다.
`create_all()`은 신규/개발 DB 초기화 용도이며 기존 운영 컬럼 변경을 대신하지 않는다.

백업 시 `auth_users`, 권한/정책, 결제, 감사 로그를 함께 일관된 snapshot으로 보관하고,
DB dump와 `AUTH_SESSION_PEPPER`는 서로 다른 접근 통제 영역에 둔다. pepper가 유출되면 모든 세션을
폐기하고 재로그인을 요구한다.
