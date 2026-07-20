# Dashboard 서버 배포 문서

이 디렉터리는 같은 배포를 실행 순서와 독자별 책임으로 분리해 설명한다. 사람이 처음부터
배포를 수행할 때는 종단간 순차 실행서를 먼저 사용하고, 세부 정책이 필요할 때 나머지 문서를
참조한다.

| 문서 | 읽는 사람 | 목적 |
|---|---|---|
| [end-to-end-human-runbook.md](end-to-end-human-runbook.md) | 실제 배포를 수행하는 사람 | 접속 PC·GitHub 웹·대상 서버·Actions로 위치를 나눈 처음부터 끝까지의 실행 순서 |
| [server-setup-human.md](server-setup-human.md) | 서버 소유자·운영자 | 별도 설치 항목, SSH/GitHub 설정, 최초 배포와 운영 확인 |
| [server-agent-runbook.md](server-agent-runbook.md) | 대상 서버에서 작업하는 AI | 허용 범위, 금지 작업, 사전 점검, 안전한 검증과 보고 형식 |
| [r2-secret-management.md](r2-secret-management.md) | 사람·AI 공통 정책 | GitHub Environment 정본, 일시 주입, 회전·사고 대응 |

현재 자동 배포 대상은 `dev`뿐이다. `main`은 테스트와 이미지 발행까지만 수행하며 서버에는
배포하지 않는다.
