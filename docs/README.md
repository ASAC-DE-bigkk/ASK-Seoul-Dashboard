# docs — ASK SEOUL Dashboard 문서 인덱스

이 폴더는 대시보드(카탈로그 + Charts Studio)의 운영·사용·설계 문서를 모은다.
**처음 합류한 사람(또는 AI)은 [HERITAGE.md](HERITAGE.md) 를 먼저 읽는다** — 그 문서 하나로
프로젝트 규정과 Charts Studio 의 설계 의도를 이어받을 수 있도록 쓰였다.

| 문서 | 내용 | 대상 독자 |
|---|---|---|
| [maintanance/README.md](maintanance/README.md) | 개발 실행·최초 최고관리자 접속·필수 운영 원칙 | 개발·설치·운영 책임자 |
| [operations.md](operations.md) | 솔루션 기동(up)/중지(down)/초기화 매뉴얼 | 운영·시연하는 사람 |
| [deployment/end-to-end-human-runbook.md](deployment/end-to-end-human-runbook.md) | 접속 PC·GitHub·서버 위치를 분리한 dev 배포 전체 순서 | 실제 배포를 수행하는 사람 |
| [deployment/server-setup-human.md](deployment/server-setup-human.md) | dev 서버 별도 설치·SSH/GitHub 설정·최초 배포 | 서버 소유자·저장소 관리자 |
| [deployment/server-agent-runbook.md](deployment/server-agent-runbook.md) | 서버 사전 점검·승인 게이트·안전한 배포 검증 | 서버에서 작업하는 AI |
| [deployment/r2-secret-management.md](deployment/r2-secret-management.md) | R2 secret 정본·일시 주입·회전·사고 대응 | 배포 운영자·AI |
| [charts-user-guide.md](charts-user-guide.md) | Charts Studio 화면 사용법 (레이아웃·차트 추가·자동 갱신) | 화면을 쓰는 사람 |
| [charts-design-intents.md](charts-design-intents.md) | 개발 의도 정리 — 대분류 › 중분류 › 소분류 | 코드를 고치는 사람 |
| [HERITAGE.md](HERITAGE.md) | **계승 문서** — 규정·설계 바탕·불변식·확장법·검증 절차 | 다음 작업자(사람/AI) |
| [additional_doc/README.md](additional_doc/README.md) | 인증·RDB·알림·AWS/GCP/Cloudflare 보안 운영 | 보안·DB·인프라 운영자 |

관련 상위 문서: [../README.md](../README.md) (프로젝트 개요·API 표) ·
dbt 커머스 gold 정의: `sample/dbt/domains/commerce/` · 수집 파이프라인: `sample/dags/domains/commerce/`
