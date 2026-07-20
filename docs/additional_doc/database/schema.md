# 인증·권한 RDB 스키마

## 지원 원칙

모델은 SQLAlchemy 공통 타입과 FK/unique/index 계약으로 작성되어 특정 DB SQL에 종속되지 않는다.
자동 테스트에서 SQLite, PostgreSQL, MySQL dialect의 모든 table/index DDL 컴파일을 검증한다.
릴리스 전에는 각 실제 DB에서도 초기화·로그인·관리 API·레이아웃 시드 smoke test를 수행한다.
기본 드라이버는 SQLite 내장, PostgreSQL `psycopg`, MySQL/MariaDB `PyMySQL`이다.

```dotenv
# SQLite
DATABASE_URL=sqlite:///./data/ask_seoul.db
# PostgreSQL
DATABASE_URL=postgresql+psycopg://user:password@postgres-auth:5432/ask_seoul?sslmode=verify-full&sslrootcert=/run/secrets/db-ca.pem
# MySQL/MariaDB
DATABASE_URL=mysql+pymysql://user:password@mysql-auth:3306/ask_seoul?charset=utf8mb4&ssl_ca=/run/secrets/db-ca.pem&ssl_check_hostname=true
```

호스트명은 대시보드 프로세스가 속한 네트워크에서 해석되어야 한다. 상위 Compose의 `postgres`는
Airflow 메타데이터용이고 호스트 포트도 공개하지 않으므로 인증 DB로 묵시적으로 재사용하지 않는다.
production의 원격 PostgreSQL은 인증서 hostname 검증(`sslmode=verify-full`), MySQL/MariaDB는
신뢰 CA와 hostname 검증을 설정하지 않으면 기동을 거부한다.

Oracle, SQL Server 등도 SQLAlchemy dialect/driver를 추가해 같은 공통 모델을 사용할 수 있지만,
운영 채택 전 해당 DB 버전에서 DDL·FK cascade·JSON·시간대·동시성 통합 테스트를 별도로 통과시켜야 한다.
SQLAlchemy의 dialect 구조는 [공식 문서](https://docs.sqlalchemy.org/en/20/dialects/index.html)를 참고한다.

## 관계

```text
auth_schema_version
auth_control

auth_users
 ├─< auth_sessions
 ├─< auth_account_tokens
 ├─< auth_mfa_challenges
 ├─< auth_mfa_recovery_codes
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
| `auth_schema_version` | 앱이 이해하는 인증 스키마 버전 |
| `auth_control` | 마지막 최고관리자 보호 등 전역 권한 전이 직렬화 |
| `auth_users` | 로그인 식별자, 해시, 역할, 상태, 승인, 이용권 |
| `auth_sessions` | 해시된 세션/CSRF, 만료·폐기, IP/UA 해시 |
| `auth_account_tokens` | 이메일 인증·비밀번호 재설정 1회용 토큰 해시 |
| `auth_mfa_challenges` | 로그인·등록용 짧은 수명의 MFA challenge |
| `auth_mfa_recovery_codes` | 사용자별 해시된 일회용 복구 코드 |
| `auth_page_resources` | 보호할 화면/API 논리 리소스 |
| `auth_role_page_permissions` | 역할별 초기 접근 |
| `auth_user_page_permissions` | 사용자별 allow/deny override |
| `auth_user_preferences` | 개인 온톨로지·UI 설정 |
| `auth_access_policies` | system/role/user 공통·개인 정책 |
| `auth_ip_blocks` | CIDR 차단과 만료 |
| `auth_payment_plans` | 일/주/월/연 요금제 |
| `auth_payment_requests` | 모의 결제 요청·검토·승인, 사용자당 단일 pending 보장 |
| `auth_dashboard_layouts` | 사용자별 Charts Studio 페이지/차트 |
| `auth_notification_deliveries` | 채널별 outbox 상태·시도 횟수·최종 오류 |
| `auth_audit_logs` | 중요 보안·관리 이벤트 append-only 이력 |

## 키·인덱스·시퀀스

- 내부 조인은 정수 PK/FK를 사용하고, 외부 API에는 추측이 어려운 UUID `public_id`만 노출한다.
- 정수 `autoincrement` PK는 DB dialect에 따라 PostgreSQL sequence/SERIAL 계열,
  MySQL `AUTO_INCREMENT`, SQLite `INTEGER PRIMARY KEY`로 렌더링된다.
  자세한 매핑은 [SQLAlchemy MetaData 문서](https://docs.sqlalchemy.org/en/21/core/metadata.html)를 참고한다.
- 사용자 이메일/닉네임/public UUID, 세션·계정 토큰 해시는 unique다.
- 상태+역할, 이용권 만료, 활성 세션, 정책 적용 순서, 결제 상태+요청일, 사용자+레이아웃 순서,
  감사 이벤트+시간, 알림 상태+시도 시각, 결제 요청+알림 상태에 복합 인덱스를 둔다.
- 역할/사용자 페이지 권한은 `(role,page_id)`, `(user_id,page_id)` unique로 중복을 막는다.
- `auth_payment_requests.pending_key`는 pending 상태일 때만 `user:<internal-id>`를 가지고 승인·거절 시
  `NULL`이 된다. nullable unique index로 DB 종류와 무관하게 사용자당 pending 요청 하나를 보장한다.
- 역할·상태 전이, 로그인 실패 수, TOTP counter, 결제 승인, 알림 outbox claim은 조건부 UPDATE 또는
  row lock으로 동시 요청의 lost update와 이중 처리를 막는다.

## 초기화·운영

```bash
.venv/bin/python scripts/init_auth_db.py
.venv/bin/python scripts/create_admin.py
.venv/bin/python scripts/setup_mfa.py
.venv/bin/python scripts/setup_mfa.py --reset-existing  # break-glass 전용
.venv/bin/python -m pytest -q tests/test_database_portability.py
```

현재 schema version은 `6`이다. `initialize_database()`는 기존 버전을 먼저 확인한 뒤 알려진
v1→v6 경로만 순서대로 적용하고,
앱보다 새로운 버전 또는 정의되지 않은 경로에서는 기동을 중단한다. 신규 테이블은 `create_all()`로 만들되
기존 컬럼·인덱스 변경은 버전별 migration 코드가 담당한다. 향후 대규모 운영 변경은 Alembic 같은 전용
도구로 expand → deploy → contract 순서를 적용한다.

버전 테이블이 없는 기존 `auth_*` 테이블이나 비어 있는 버전 이력은 최신 스키마로 추정하지 않고
기동을 중단한다. 마이그레이션과 기본 시드는 `auth_control` 단일 행 쓰기로 직렬화한다.
production 앱 worker는 스키마를 변경하지 않고 전체 table/column/index/unique/FK 계약을 검증하므로,
migration job 한 개를 먼저 완료한 뒤 앱 인스턴스를 순차 기동한다.

백업 시 `auth_users`, 권한/정책, 결제, 감사 로그를 함께 일관된 snapshot으로 보관하고,
DB dump와 `AUTH_SESSION_PEPPER`, `AUTH_MFA_MASTER_KEY`는 서로 다른 접근 통제 영역에 둔다.
기본 SQLite 경로는 런타임에 디렉터리 `0700`, DB/WAL/SHM `0600`을 적용하며 production에서
권한 제한에 실패하면 기동을 중단한다. 다른 프로세스와
공유하는 사용자 지정 경로는 배포 계정·볼륨 권한도 별도로 제한한다.
pepper가 유출되면 모든 세션을 폐기하고 재로그인을 요구한다. MFA master key가 유실되면 기존 사용자의
TOTP를 복원할 수 없으므로 복구 코드를 사용해 재등록하거나 운영자 확인 절차를 거쳐야 한다.
게스트·일반회원은 최고관리자 화면의 감사 사유 기반 초기화를 사용할 수 있고, 권한 계정은 서버
콘솔의 `--reset-existing` 절차만 허용한다.
