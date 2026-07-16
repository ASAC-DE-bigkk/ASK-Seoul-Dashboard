# 추가 운영 문서

인증·회원·권한 시스템을 운영 환경에 올릴 때 필요한 보충 문서다.

| 카테고리 | 문서 |
|---|---|
| 인증·보안 | [auth/security-architecture.md](auth/security-architecture.md) |
| RDB | [database/schema.md](database/schema.md) |
| 운영 알림 | [notifications/operations.md](notifications/operations.md) |
| 개인정보·보존 | [legal/privacy-and-retention.md](legal/privacy-and-retention.md) |
| AWS | [cloud/aws-waf.md](cloud/aws-waf.md) |
| GCP | [cloud/gcp-cloud-armor.md](cloud/gcp-cloud-armor.md) |
| Cloudflare | [cloud/cloudflare-waf.md](cloud/cloudflare-waf.md) |

앱 내부 rate limit은 단일 프로세스의 2차 방어다. 다중 인스턴스·대규모 공격의 1차 방어는
반드시 각 클라우드의 WAF/Edge에서 구성한다.
