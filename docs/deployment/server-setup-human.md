# dev 서버 준비 안내 — 사람용

대상: 서버 소유자 또는 GitHub 저장소 관리자.

이 문서는 `dev` merge 후 ASK SEOUL Dashboard와 전용 PostgreSQL을
`exisnet.iptime.org:3707` 서버에 Docker Compose로 처음 배포하기 위한 체크리스트다.
현재 작업 브랜치가 PR을 통해 `dev`에 merge되기 전에는 서버에 dashboard/PostgreSQL
컨테이너나 volume을 만들지 않는다.

처음부터 실제 명령을 순서대로 수행하려면 실행 위치까지 고정한
[dev 배포 종단간 순차 실행서](end-to-end-human-runbook.md)를 먼저 따른다. 이 문서는 각
준비 항목의 이유와 세부 판단 기준을 설명한다.

## 결론

서버에 PostgreSQL, Python, FastAPI, Uvicorn을 직접 설치할 필요가 없다. 모두 컨테이너로
제공된다. 공개 도메인, Nginx/Caddy 같은 reverse proxy, HTTPS 인증서도 현재 dev에는
필요하지 않다. 앱은 서버의 `127.0.0.1:8765`에만 열고 SSH tunnel로 접속한다.

호스트에서 확인하거나 설치할 수 있는 항목은 다음뿐이다.

| 항목 | 필요한 이유 | 처리 |
|---|---|---|
| x86_64 Linux | 현재 GitHub Actions가 amd64 이미지를 빌드 | 이미 확인됨 |
| Docker Engine | 컨테이너 실행 | 기존 Trino가 Docker라면 재설치하지 말고 동작만 확인 |
| Docker Compose v2 plugin | `docker compose` 배포 | `docker-compose` 단독 명령이 아니라 plugin 필요 |
| `flock` | 동시 배포 방지 lock | 보통 `util-linux` 패키지에 포함 |
| `openssl` | PostgreSQL 비밀번호와 인증 secret 최초 생성 | OS 패키지로 설치 |
| `elt_net` Docker network | 기존 Trino와 dashboard 연결 | 기존 데이터 스택의 network를 재사용 |
| 배포 전용 SSH key | GitHub Actions가 서버에 접속 | private key는 GitHub secret에만 저장 |

PostgreSQL 데이터는 Compose volume `auth_postgres`에 남는다. 실제 Docker volume 이름에는
project prefix가 붙어 `ask-seoul-dashboard-dev_auth_postgres`가 된다. R2 자격증명의 배포
정본은 GitHub `development` Environment이고 실행 시 Trino에만 일시 주입한다. Dashboard와
PostgreSQL에는 복사하지 않는다.

2026-07-21 최초 inventory에서는 Docker/Compose가 없었지만, 설치 후 재확인 결과 대상은
Rocky Linux 8.10 x86_64, Docker Engine `29.6.2`, Compose `v5.3.1`이고 `exi`에 docker group이
적용되어 있다. `flock`·`openssl`도 존재한다. 아직 Trino container와 `elt_net`은 없으므로
첫 `dev` workflow가 [최소 Trino companion](../../deploy/trino/README.md)을 먼저 만들고
Dashboard를 배포한다. 이 사실은 시점에 따라 바뀔 수 있으므로 첫 명령으로 다시 확인한다.

## 1. 서버 사전 점검

서버에 기존 방식으로 로그인한 뒤 아래 명령을 실행한다.

```bash
uname -m
docker version --format '{{.Server.Version}}'
docker compose version
command -v flock
command -v openssl
docker network inspect elt_net --format '{{.Name}} {{.Driver}} {{.Scope}}'
docker ps --filter network=elt_net --format '{{.Names}}\t{{.Status}}'
df -h /var/lib/docker
free -h
```

정상 기준:

- `uname -m`은 `x86_64`다.
- `docker version`과 `docker compose version`이 오류 없이 끝난다.
- `flock`, `openssl`은 실행 파일 경로를 출력한다.
- `elt_net`이 존재하고 그 network에서 Trino 컨테이너가 실행 중이다.
- `/var/lib/docker`의 여유 공간과 메모리가 기존 Trino 외에 dashboard, worker,
  PostgreSQL을 실행하기에 충분하다. 코드가 강제하는 고정 최소 용량은 없으므로 기존 서버
  사용량과 첫 배포 후 실제 사용량을 함께 본다.

현재 서버가 이 검사를 통과하면 추가 OS 설치는 없다.

## 2. 빠진 항목만 설치

### Docker 또는 Compose가 없는 경우

서버 배포판에 맞는 Docker 공식 절차를 사용한다.

- [Docker Engine 설치](https://docs.docker.com/engine/install/)
- [Linux용 Compose plugin 설치](https://docs.docker.com/compose/install/linux/)
- [Linux 설치 후 권한 설정](https://docs.docker.com/engine/install/linux-postinstall/)
- [Rocky Linux Docker Engine 절차](https://docs.rockylinux.org/gemstones/containers/docker/)

기존 Docker가 정상 동작하면 제거하거나 재설치하지 않는다. 편의용 `curl | sh` 설치 스크립트,
임의 PPA, 오래된 standalone `docker-compose`를 새로 추가하지 않는다.

`exi`가 `sudo` 없이 Docker를 실행할 수 없어 workflow가 실패하는 경우에는 서버 관리자가
권한 모델을 선택해야 한다. 일반 Docker group을 쓸 경우 예시는 다음과 같다.

```bash
sudo usermod -aG docker exi
```

변경 후 `exi` 세션을 완전히 종료하고 다시 로그인해 `docker ps`를 확인한다. Docker group은
사실상 root 수준 권한이므로 승인된 계정만 넣는다. `/var/run/docker.sock`을 `chmod 666`으로
열지 않는다.

### `flock` 또는 `openssl`만 없는 경우

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y util-linux openssl
```

RHEL/Fedora 계열:

```bash
sudo dnf install -y util-linux openssl
```

설치 전 서버 배포판을 `cat /etc/os-release`로 확인한다. 서버 AI가 이 설치를 대신하려면
[AI용 runbook](server-agent-runbook.md)의 승인 게이트를 적용한다.

현재 Rocky 8.10 대상은 검토 가능한 설치 script를 서버에 둔다.

현재 서버에는 Docker/Compose가 이미 설치되어 있으므로 아래 설치 명령을 다시 실행하지 않는다.
새 서버이거나 1절에서 Docker가 실제로 없을 때만 사용한다.

```bash
ssh -t -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org \
  'sudo bash /home/exi/apps/ask-seoul-trino-dev/install-docker-rocky.sh exi'
```

sudo 비밀번호는 로컬 terminal에만 입력한다. script는 `--allowerasing`을 사용하지 않는다.
DNF가 Podman/runc 충돌이나 package 제거를 제안하면 승인하지 말고 중단한 뒤 transaction
summary를 검토한다. 설치 뒤 `exi`가 새 `docker` group을 받도록 SSH 연결을 완전히 끊고 다시
연결한다.

## 3. 배포 전용 SSH key 준비

이 단계는 사람 소유의 PC에서 수행한다. 현재 Codex 환경에는 서버가 허용한 private key가
없으므로, 허가 없는 SSH 접속 시도는 배포 준비가 아니다.

1. 배포 전용 key를 새로 만든다.

   ```bash
   ssh-keygen -t ed25519 \
     -f ~/.ssh/ask-seoul-dashboard-deploy \
     -C ask-seoul-dashboard-dev-deploy
   ```

2. public key만 서버의 `exi` 계정에 등록한다.

   ```bash
   ssh-copy-id -p 3707 \
     -i ~/.ssh/ask-seoul-dashboard-deploy.pub \
     exi@exisnet.iptime.org
   ```

3. key가 단독으로 동작하는지 확인한다.

   ```bash
   ssh -i ~/.ssh/ask-seoul-dashboard-deploy \
     -p 3707 \
     -o IdentitiesOnly=yes \
     exi@exisnet.iptime.org \
     'uname -m && docker compose version'
   ```

private key 원문을 저장소, 채팅, 서버 runtime env에 넣지 않는다. 이후 private key 전체를
GitHub `development` Environment의 `SSH_PRIVATE_KEY` secret으로 등록한다.

## 4. SSH host key를 검증해 고정

로컬 PC에서 받은 host key fingerprint와 서버 콘솔에서 읽은 fingerprint를 비교한다.
`ssh-keyscan` 출력만 보고 신뢰하면 중간자 공격을 구분할 수 없다.

로컬 PC:

```bash
ssh-keyscan -p 3707 -t ed25519 exisnet.iptime.org \
  > /tmp/ask-seoul-dashboard-known-hosts
ssh-keygen -lf /tmp/ask-seoul-dashboard-known-hosts
```

서버 콘솔 또는 이미 신뢰된 접속:

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

fingerprint가 정확히 같을 때만 `/tmp/ask-seoul-dashboard-known-hosts`의 전체 한 줄을
GitHub `SSH_KNOWN_HOSTS` secret에 넣는다. 포트가 3707이므로 행의 host 표기는
`[exisnet.iptime.org]:3707`이어야 한다.

## 5. GitHub `development` Environment

GitHub 저장소의 **Settings → Environments → New environment**에서 `development`를 만든다.
[GitHub Environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[Environment secrets](https://docs.github.com/en/actions/reference/security/secrets),
[Environment variables](https://docs.github.com/en/actions/concepts/workflows-and-actions/variables)
문서를 기준으로 다음 값을 등록한다.

| 종류 | 이름 | 값 |
|---|---|---|
| Variable | `DEPLOY_HOST` | `exisnet.iptime.org` |
| Variable | `DEPLOY_PORT` | `3707` |
| Variable | `DEPLOY_USER` | `exi` |
| Variable | `DEPLOY_PATH` | `/home/exi/apps/ask-seoul-dashboard-dev` |
| Variable | `COMPOSE_PROJECT_NAME` | `ask-seoul-dashboard-dev` |
| Variable | `RUNTIME_ENV_FILE` | `/home/exi/.config/ask-seoul/dashboard-dev.env` |
| Variable | `DASHBOARD_DATA_NETWORK` | `elt_net` |
| Variable | `TRINO_DEPLOY_PATH` | `/home/exi/apps/ask-seoul-trino-dev` |
| Variable | `TRINO_COMPOSE_PROJECT_NAME` | `ask-seoul-trino-dev` |
| Secret | `SSH_PRIVATE_KEY` | 3단계에서 만든 private key 전체 |
| Secret | `SSH_KNOWN_HOSTS` | 4단계에서 fingerprint를 검증한 한 줄 |
| Secret | `R2_DEV_DATA_CATALOG_URI` | 기존 dev catalog URI |
| Secret | `R2_DEV_DATA_CATALOG_WAREHOUSE` | 기존 dev warehouse |
| Secret | `R2_DEV_DATA_CATALOG_TOKEN` | 기존 dev catalog OAuth2 token |
| Secret | `R2_DEV_ENDPOINT` | 기존 R2 S3 endpoint |
| Secret | `R2_DEV_ACCESS_KEY_ID` | 기존 R2 access key ID |
| Secret | `R2_DEV_SECRET_ACCESS_KEY` | 기존 R2 secret access key |

R2 6개는 UI에 하나씩 복사하지 않고 신뢰된 로컬 PC에서 다음 script로 최초 등록한다.

```bash
cd <sample>
dashboard/deploy/trino/register-github-secrets.sh .env
```

값의 정본·회전·노출 사고 대응은
[R2/Trino secret 관리 계약](r2-secret-management.md)을 따른다.

`production` Environment는 아직 만들 필요가 없다. workflow의 `main` job에는 SSH 배포 단계가
없으며, 이 금지는 별도 production 설계와 승인 전까지 유지한다.

## 6. `dev` merge 시 자동으로 생기는 것

사전 준비가 끝난 뒤 PR을 `dev`에 merge하면 workflow가 다음 순서로 처리한다.

1. 테스트와 Docker Compose smoke test를 실행한다.
2. commit SHA로 고정한 dashboard 이미지를 GHCR에 발행한다.
3. 서버의 Dashboard/Trino 배포 디렉터리를 만들고 Compose·배포 script를 전송한다.
4. 아키텍처, Docker/Compose, `flock`, `openssl`을 다시 검사한다.
5. GitHub R2 secrets로 실행별 mode `0600` 임시 파일을 만들고 Trino를 기동·검증한 뒤
   성공·실패와 관계없이 임시 파일을 삭제한다.
6. Dashboard runtime env가 없을 때만 PostgreSQL 비밀번호, session pepper, MFA master key를 서버에서
   생성하고 mode `0600`으로 저장한다. 기존 파일은 덮어쓰지 않는다.
7. `postgres:16-alpine`을 기동하고 DB migration을 적용한다.
8. dashboard와 notification worker를 기동한 뒤 `/health`를 검사한다.
9. 새 dashboard가 unhealthy면 직전 앱 Compose/image를 복구한다.

따라서 호스트에 PostgreSQL이나 Python을 별도로 설치하지 않는다. 첫 배포 때 생기는 영속 대상은
runtime env와 PostgreSQL/auth/cache Docker volume이다. DB migration은 additive이므로 앱
rollback이 DB schema를 되돌리지는 않는다.

최소 Trino는 workflow가 관리한다. 서버에는 config만 장기 보존하며
`/home/exi/.config/ask-seoul/trino-dev.env` 또는
`/home/exi/apps/ask-seoul-trino-dev/.runtime.*.env`가 배포 후 남아 있으면 안 된다.

## 7. 배포 후 확인

GitHub Actions의 `deploy` job이 성공한 뒤 서버에서 확인한다.

```bash
cd /home/exi/apps/ask-seoul-dashboard-dev

docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  ps

stat -c '%a %n' /home/exi/.config/ask-seoul/dashboard-dev.env
```

`postgres`와 `dashboard`가 healthy이고 runtime env mode가 `600`이어야 한다.
문제가 있으면 secret 값을 출력하지 말고 다음 로그의 마지막 부분만 확인한다.

```bash
docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  logs --tail 100 dashboard postgres notification-worker
```

로컬 PC에서 tunnel을 유지한 채 접속한다.

```bash
ssh -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -L 8765:127.0.0.1:8765 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org
```

브라우저 주소는 `http://127.0.0.1:8765`다. 공유기 port forwarding이나 서버 방화벽에 8765를
추가하지 않는다.

## 8. 사람이 직접 해야 하는 최초 작업

최초 관리자 생성과 MFA 등록은 이메일, 비밀번호, TOTP seed, 복구 코드를 다루므로 서버의
보호된 TTY에서 사람이 수행한다. AI, GitHub Actions, 채팅에 입력을 위임하지 않는다.
정확한 Compose 명령과 MFA seed 폐기 절차는
[운영 매뉴얼 5-5](../operations.md#5-5-배포-후-최초-관리자)를 따른다.

운영 전 DB 백업도 [같은 절](../operations.md#5-5-배포-후-최초-관리자)의 `pg_dump` 명령을
사용한다. dump 파일은 mode `0600`으로 보호하고 서버 밖의 승인된 백업 위치로 옮긴다.
`docker compose down -v`, `docker volume rm`, `docker volume prune`은 인증 DB를 삭제할 수
있으므로 사용하지 않는다.

## 9. 지금 설치하지 않을 것

- 공개 도메인, reverse proxy, HTTPS 인증서: dev는 SSH tunnel이므로 불필요
- host PostgreSQL과 DB CA: dev PostgreSQL은 같은 Compose private network 전용
- Secret Manager agent: dev는 서버의 mode `0600` runtime env로 시작
- R2 SDK/자격증명: Trino가 기존 설정으로 R2/Iceberg를 조회
- SQLite/D2 관련 도구: 접속 제품/API와 query adapter 계약이 확정되지 않았으므로 보류
- `main`용 production 구성: 현재 배포 금지

SQLite/D2 전환 시에는 단순 패키지 설치가 아니라 `app/charts/trino.py`, snapshot relation,
캐시·동시성·백필 계약을 함께 바꾸는 별도 변경으로 검토한다.

## 10. 서버 AI에 전달할 첫 지시문

서버 안의 AI에게는 이 사람용 문서 대신 AI용 runbook과 아래 지시를 전달한다.

```text
dashboard/docs/deployment/server-agent-runbook.md를 처음부터 끝까지 읽어라.
우선 2절의 read-only inventory만 실행하고 어떤 파일·패키지·컨테이너도 변경하지 마라.
runtime env와 sample/.env의 값은 읽거나 출력하지 마라.
9절 형식으로 결과와 blocker를 보고한 뒤, 쓰기 작업은 내 명시적 승인을 기다려라.
main 배포는 금지되어 있다.
```

이렇게 1차 점검 결과를 받은 뒤 누락 패키지 설치나 디렉터리 생성만 항목별로 승인한다.
