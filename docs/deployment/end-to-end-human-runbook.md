# dev 배포 종단간 순차 실행서 — 사람용

대상: ASK SEOUL Dashboard를 처음부터 `dev` 서버에 배포하는 서버 소유자·저장소 관리자.

이 문서는 지금까지 구성한 SSH, GitHub Environment, Docker, Trino/R2, Dashboard/PostgreSQL
배포를 사람이 직접 수행한다고 가정한 **단일 순차 실행서**다. 세부 정책은 연결된 문서가
정본이지만, 실제 작업은 이 문서를 위에서 아래로 진행한다.

`main`은 테스트와 GHCR 이미지 발행까지만 수행하며 **서버 배포는 하지 않는다**.

## 0. 실행 위치 표기

모든 단계에는 명령을 입력하거나 화면을 조작할 위치를 붙인다.

| 표기 | 사람이 작업하는 위치 | 이 작업에서의 의미 |
|---|---|---|
| `[접속 PC]` | 대상 서버에 SSH 접속할 수 있는 신뢰된 PC | repository, `git`, `gh`, SSH private key, 검증된 `known_hosts`를 관리 |
| `[GitHub 웹]` | 접속 PC의 브라우저로 연 GitHub 저장소 화면 | Environment, Actions, PR, branch 정책을 확인 |
| `[대상 서버]` | `exi@exisnet.iptime.org`의 shell | Docker·Compose 실행, 컨테이너 확인, 최초 관리자·백업 수행 |
| `[GitHub Actions 자동]` | GitHub-hosted runner와 대상 서버 | `dev` push 이후 자동 실행. 사람이 여기서 직접 명령을 입력하지 않음 |

`[접속 PC → 대상 서버]`는 접속 PC에서 `ssh`를 실행한 뒤, 프롬프트가 서버로 바뀐 상태에서
후속 명령을 실행한다는 뜻이다. `ssh '원격 명령'`처럼 따옴표 안에 있는 명령도 입력 위치는
접속 PC지만 실제 변경 위치는 대상 서버다.

실제 프롬프트를 확인하고 진행한다.

```text
# 접속 PC 예시
exi-mac sample %

# 대상 서버 예시
[exi@server ~]$
```

private key, R2 값, PostgreSQL 비밀번호, MFA seed, 복구 코드는 터미널 출력·채팅·문서에
붙이지 않는다.

## 1. 고정된 dev 대상

| 항목 | 값 |
|---|---|
| 저장소 | `ASAC-DE-bigkk/ASK-Seoul-Dashboard` |
| 배포 branch | `dev` |
| 배포하지 않는 branch | `main` |
| 서버 | `exisnet.iptime.org` |
| SSH port / user | `3707` / `exi` |
| 서버 아키텍처 | `x86_64` (`amd64`) |
| Dashboard 경로 | `/home/exi/apps/ask-seoul-dashboard-dev` |
| Dashboard Compose project | `ask-seoul-dashboard-dev` |
| Dashboard runtime | `/home/exi/.config/ask-seoul/dashboard-dev.env` |
| Dashboard 접속 | 서버 `127.0.0.1:8765`, 접속 PC의 SSH tunnel만 사용 |
| Trino 경로 | `/home/exi/apps/ask-seoul-trino-dev` |
| Trino Compose project | `ask-seoul-trino-dev` |
| 공유 Docker network | `elt_net` |
| 서비스 DB | Dashboard 전용 PostgreSQL 16 + 영속 Docker volume |
| 차트 데이터 | Trino `iceberg_dev` → R2/Iceberg |
| R2 배포 secret 정본 | GitHub `development` Environment |

dev에는 공개 도메인, HTTPS reverse proxy, host PostgreSQL을 추가하지 않는다. 8765 port를
공유기나 서버 방화벽에서 공인망에 열지 않는다.

## 2. 현재 작업 상태

이 표는 2026-07-21 작업 결과의 인수인계 snapshot이다. 서버와 GitHub 상태는 바뀔 수 있으므로
실행할 때 각 단계의 검증 명령으로 다시 확인한다.

| 상태 | 완료된 작업 또는 남은 작업 |
|---|---|
| 완료 | 접속 PC에 전용 Ed25519 key 생성: `~/.ssh/ask-seoul-dashboard-deploy` |
| 완료 | public key 서버 등록 및 전용 key SSH 접속 성공 확인 |
| 완료 | 서버 host key를 고정하고 `SSH_PRIVATE_KEY`, `SSH_KNOWN_HOSTS` 등록 확인 |
| 완료 | 서버 inventory: Rocky Linux 8.10 x86_64, `flock`·`openssl` 존재 |
| 완료 | Docker Engine `29.6.2`, Compose `v5.3.1` 설치, `exi`의 docker group 적용 |
| 완료 | `development` Environment, `dev` branch 제한, Environment variable 9개 등록·재확인 |
| 완료 | `gh`의 Exisign 인증과 `repo`·`workflow` scope 재확인 |
| 완료 | 서버에 Trino 배포 asset과 검토용 Docker 설치 script 전송 |
| 완료 | GitHub Actions, Dashboard/PostgreSQL Compose, 최소 Trino companion, rollback 구성 작성 |
| 보류 | R2 Environment secret 6개 등록 |
| 보류 | 현재 변경 검토·commit·push·PR merge. 현재 로컬 branch는 `feat/3-secure-auth-rbac` |
| 보류 | 최초 `dev` workflow 성공과 Dashboard/PostgreSQL/Trino 실제 기동 |
| 보류 | 최초 관리자 생성, MFA 등록, DB backup, SSH tunnel 브라우저 검증 |
| 조건부 보류 | 첫 GitHub-managed Trino 성공 후 이전 `/home/exi/.config/ask-seoul/trino-dev.env` 삭제 |

2026-07-21 재확인 시 GitHub에는 SSH secret 2개만 있고 R2 secret 6개는 없었다. 서버에는
장기 migration 파일 `/home/exi/.config/ask-seoul/trino-dev.env`가 mode `0600`으로 남아 있고,
Trino container·`elt_net`·실행별 `.runtime.*.env`는 없었다.

현재 구현은 아직 `dev`에 merge되지 않았으므로 GitHub Actions 배포도 활성 상태가 아니다.
**지금 바로 해야 할 일**은 다음 순서다.

1. `[접속 PC]` 8단계의 `gh auth status`와 R2 secret 6개 등록
2. `[접속 PC → 대상 서버]` 9-3단계의 기존 Docker 상태만 재검증
3. `[접속 PC]` 11단계의 변경 검증·PR review 후 `dev` merge
4. `[GitHub 웹]` 12단계의 첫 workflow 관찰

## 3. 접속 PC 준비

**작업 위치: `[접속 PC]`**

필수 도구를 확인한다.

```bash
git --version
gh --version
ssh -V
gh auth status
```

`gh auth status`가 실패하면 이 접속 PC에서만 재인증하고 다시 확인한다.

```bash
gh auth login -h github.com
gh auth status
```

repository 최상위와 Dashboard branch를 확인한다.

```bash
cd /Users/exi/IdeaProjects/asac_final_pjt/sample
git -C dashboard branch --show-current
git -C dashboard status --short
```

현재 이어서 작업하는 경우 branch는 `feat/3-secure-auth-rbac`여야 한다. 다른 checkout에서
처음 수행한다면 기존 issue/PR branch를 먼저 받아서 전환한다. `.env`와 실제 secret 파일은
Dashboard commit 대상이 아니다.

## 4. 배포 전용 SSH key 생성

**작업 위치: `[접속 PC]`**

현재 key가 이미 있으므로 새로 만들지 말고 존재와 fingerprint만 확인한다.

```bash
ls -l \
  ~/.ssh/ask-seoul-dashboard-deploy \
  ~/.ssh/ask-seoul-dashboard-deploy.pub
ssh-keygen -lf ~/.ssh/ask-seoul-dashboard-deploy.pub
```

처음부터 구축해 파일이 없을 때만 다음 명령으로 생성한다.

```bash
ssh-keygen -t ed25519 \
  -f ~/.ssh/ask-seoul-dashboard-deploy \
  -C ask-seoul-dashboard-dev-deploy
chmod 600 ~/.ssh/ask-seoul-dashboard-deploy
chmod 644 ~/.ssh/ask-seoul-dashboard-deploy.pub
```

private key는 접속 PC와 GitHub `SSH_PRIVATE_KEY`에만 둔다. 서버에는 `.pub` 내용만 등록한다.

## 5. 서버에 public key 등록

### 5-1. 기존 SSH 접속 수단이 있는 경우

**명령 입력 위치: `[접속 PC]`**

**변경 위치: `[대상 서버]`의 `/home/exi/.ssh/authorized_keys`**

```bash
ssh-copy-id \
  -p 3707 \
  -i ~/.ssh/ask-seoul-dashboard-deploy.pub \
  exi@exisnet.iptime.org
```

### 5-2. 기존 SSH 접속 수단이 없는 경우

**작업 위치: `[대상 서버]` 콘솔 또는 이미 신뢰된 서버 관리자 session**

접속 PC에서 public key 한 줄만 전달받아 `exi`의 `authorized_keys`에 등록한다. private key는
서버로 옮기지 않는다. 권한은 `.ssh` `0700`, `authorized_keys` `0600`이어야 한다.

### 5-3. 전용 key 단독 검증

**명령 입력 위치: `[접속 PC]`**

**조회 위치: `[대상 서버]`**

```bash
ssh \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org \
  'id && uname -m'
```

`exi`와 `x86_64`가 출력되어야 한다.

## 6. SSH host key 검증과 고정

### 6-1. 접속 후보 key 수집

**작업 위치: `[접속 PC]`**

```bash
ssh-keyscan \
  -p 3707 \
  -t ed25519 \
  exisnet.iptime.org \
  > /tmp/ask-seoul-dashboard-known-hosts
ssh-keygen -lf /tmp/ask-seoul-dashboard-known-hosts
```

### 6-2. 서버 자체 fingerprint 확인

**작업 위치: `[대상 서버]` 콘솔 또는 이미 신뢰된 SSH session**

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

두 fingerprint가 정확히 같을 때만 다음 단계로 진행한다. 접속 PC 파일의 host 표기는
`[exisnet.iptime.org]:3707`이어야 한다.

## 7. GitHub `development` Environment 구성

### 7-1. Environment와 branch 제한

**작업 위치: `[GitHub 웹]`**

1. `ASAC-DE-bigkk/ASK-Seoul-Dashboard` → **Settings → Environments**로 이동한다.
2. `development`를 생성하거나 기존 Environment를 연다.
3. Deployment branches/tags는 `dev`만 허용한다.
4. `production` Environment는 만들지 않는다.

### 7-2. variable 9개 등록

**작업 위치: `[GitHub 웹]`**

| 이름 | 값 |
|---|---|
| `DEPLOY_HOST` | `exisnet.iptime.org` |
| `DEPLOY_PORT` | `3707` |
| `DEPLOY_USER` | `exi` |
| `DEPLOY_PATH` | `/home/exi/apps/ask-seoul-dashboard-dev` |
| `COMPOSE_PROJECT_NAME` | `ask-seoul-dashboard-dev` |
| `RUNTIME_ENV_FILE` | `/home/exi/.config/ask-seoul/dashboard-dev.env` |
| `DASHBOARD_DATA_NETWORK` | `elt_net` |
| `TRINO_DEPLOY_PATH` | `/home/exi/apps/ask-seoul-trino-dev` |
| `TRINO_COMPOSE_PROJECT_NAME` | `ask-seoul-trino-dev` |

### 7-3. SSH secret 2개 등록

**작업 위치: `[접속 PC]`**

GitHub 인증이 정상인 접속 PC에서 파일을 stdin으로 전달한다. 값은 화면이나 shell argument에
붙이지 않는다.

```bash
gh secret set SSH_PRIVATE_KEY \
  --env development \
  --repo ASAC-DE-bigkk/ASK-Seoul-Dashboard \
  < ~/.ssh/ask-seoul-dashboard-deploy

gh secret set SSH_KNOWN_HOSTS \
  --env development \
  --repo ASAC-DE-bigkk/ASK-Seoul-Dashboard \
  < /tmp/ask-seoul-dashboard-known-hosts
```

## 8. GitHub 인증과 R2 secret 등록

### 8-1. `gh` 인증 확인

**작업 위치: `[접속 PC]`**

```bash
gh auth status
```

실패할 때만 같은 PC에서 재인증한다.

```bash
gh auth login -h github.com
gh auth status
```

active account가 저장소에 접근할 수 있고 `repo`, `workflow` scope를 가져야 한다.
3단계에서 이미 확인했더라도 R2 값을 외부로 전송하기 직전에 다시 실행한다.

### 8-2. R2 secret 6개 일괄 등록

**작업 위치: `[접속 PC]`**

**입력 파일: 접속 PC의 `/Users/exi/IdeaProjects/asac_final_pjt/sample/.env`**

**저장 위치: GitHub `development` Environment**

실제 운영 credential을 외부 GitHub Environment로 전송하는 단계이므로 AI/Codex에 값 읽기나
전송을 위임하지 않고, 저장소 접근 권한이 있는 사람이 신뢰된 접속 PC에서 직접 실행한다.

```bash
cd /Users/exi/IdeaProjects/asac_final_pjt/sample
dashboard/deploy/trino/register-github-secrets.sh .env
```

이 script는 평문 중간 파일을 만들지 않고 다음 이름만 GitHub로 전송한다.

```text
R2_DEV_DATA_CATALOG_URI
R2_DEV_DATA_CATALOG_WAREHOUSE
R2_DEV_DATA_CATALOG_TOKEN
R2_DEV_ENDPOINT
R2_DEV_ACCESS_KEY_ID
R2_DEV_SECRET_ACCESS_KEY
```

값이 아니라 등록된 이름만 확인한다.

```bash
gh secret list \
  --env development \
  --repo ASAC-DE-bigkk/ASK-Seoul-Dashboard
```

정확히 위 6개와 SSH secret 2개가 보여야 한다. 전체 정책은
[R2/Trino secret 관리 계약](r2-secret-management.md)을 따른다.

## 9. 대상 서버에 Docker Engine·Compose 설치

현재 서버에는 검토용 script가
`/home/exi/apps/ask-seoul-trino-dev/install-docker-rocky.sh`로 전송되어 있다.
2026-07-21 재확인 결과 Docker Engine `29.6.2`, Compose `v5.3.1`이 이미 동작하고 `exi`가
docker group에 포함되어 있다. **현재 서버에서는 9-1·9-2를 다시 실행하지 말고 9-3 검증만
수행한다.** 9-1·9-2는 새 서버를 처음 준비하거나 Docker가 실제로 없을 때만 사용한다.

### 9-1. 처음부터 준비할 때 script 전송

이미 파일이 있으면 이 절은 건너뛴다.

**명령 입력 위치: `[접속 PC]`**

**변경 위치: `[대상 서버]`**

```bash
cd /Users/exi/IdeaProjects/asac_final_pjt/sample

ssh \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org \
  'install -d -m 700 /home/exi/apps/ask-seoul-trino-dev'

scp \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -P 3707 \
  -o IdentitiesOnly=yes \
  dashboard/deploy/install-docker-rocky.sh \
  exi@exisnet.iptime.org:/home/exi/apps/ask-seoul-trino-dev/

ssh \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org \
  'chmod 700 /home/exi/apps/ask-seoul-trino-dev/install-docker-rocky.sh'
```

### 9-2. sudo 설치 실행

**명령 입력 위치: `[접속 PC]`**

**sudo 비밀번호 입력·package 변경 위치: `[대상 서버]`**

```bash
ssh -t \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org \
  'sudo bash /home/exi/apps/ask-seoul-trino-dev/install-docker-rocky.sh exi'
```

새 Docker repository GPG key를 승인하기 전 다음 fingerprint를 대조한다.

```text
060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35
```

DNF가 Podman/runc 충돌 또는 package 제거를 제안하면 `n`으로 중단한다. transaction summary를
검토하기 전에는 `--allowerasing`이나 Podman 강제 삭제를 실행하지 않는다.

설치가 끝나면 SSH 연결을 완전히 종료한다. `exi`의 새 `docker` group은 새 login session부터
적용된다.

### 9-3. 재로그인 후 Docker 검증

**명령 입력 위치: `[접속 PC]`**

**조회 위치: `[대상 서버]`**

```bash
ssh \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org \
  'id &&
   docker version --format "server={{.Server.Version}}" &&
   docker compose version &&
   systemctl is-active docker &&
   command -v flock &&
   command -v openssl'
```

정상 기준은 `docker` group 포함, Docker server version 출력, Compose v2 출력,
`systemctl`의 `active`다. `/var/run/docker.sock`을 `chmod 666`으로 열지 않는다.

## 10. 서버 사전 상태 재확인

**작업 위치: `[접속 PC → 대상 서버]`**

```bash
ssh \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org
```

프롬프트가 대상 서버로 바뀐 뒤 실행한다.

```bash
id
uname -m
cat /etc/os-release
docker info --format 'architecture={{.Architecture}} root={{.DockerRootDir}}'
docker compose version
df -h /var/lib/docker
free -h
ss -ltn 'sport = :8765'
test -f /home/exi/apps/ask-seoul-trino-dev/install-docker-rocky.sh
```

정상 기준:

- architecture가 `x86_64`/`amd64`다.
- Docker와 Compose가 `exi` 권한으로 동작한다.
- 8765를 다른 process가 사용하지 않는다.
- disk·memory가 첫 pull과 PostgreSQL volume 생성을 감당할 수 있다.

현재 Trino와 `elt_net`이 없어도 정상이다. 첫 `dev` workflow가 Trino companion과 network를
먼저 만들고 그다음 Dashboard를 배포한다.

## 11. 변경 검증·commit·PR

### 11-1. 배포 코드 검증

**작업 위치: `[접속 PC]`**

```bash
cd /Users/exi/IdeaProjects/asac_final_pjt/sample/dashboard

python3 -m pytest -q

bash -n \
  deploy/deploy.sh \
  deploy/bootstrap-dev.sh \
  deploy/install-docker-rocky.sh \
  deploy/trino/deploy.sh \
  deploy/trino/register-github-secrets.sh

export IMAGE_REF=ghcr.io/example/ask-seoul-dashboard:0123456789012345678901234567890123456789
export RUNTIME_ENV_FILE="$PWD/deploy/runtime.dev.env.example"

docker compose \
  --env-file deploy/runtime.dev.env.example \
  --file deploy/compose.yaml \
  config --quiet

DASHBOARD_DATA_NETWORK=elt_net docker compose \
  --env-file deploy/runtime.dev.env.example \
  --file deploy/compose.yaml \
  --file deploy/compose.data-network.yaml \
  config --quiet

docker compose \
  --env-file deploy/trino/runtime.env.example \
  --project-name ask-seoul-trino-check \
  --file deploy/trino/compose.yaml \
  config --quiet

git diff --check
```

접속 PC에 Docker가 없으면 Compose 검사는 GitHub Actions에서 실행되지만, 가능하면 merge 전에
Docker가 있는 개발 PC에서 먼저 통과시킨다.

### 11-2. 변경 범위와 secret 확인

**작업 위치: `[접속 PC]`**

```bash
git status --short
git diff --stat
git diff -- .github/workflows/deploy.yaml deploy docs/deployment
```

다음을 확인한다.

- `.env`, SSH private key, 실제 R2 값, runtime secret이 staged file에 없다.
- `main-deployment-disabled` job과 `deploy` job의 `if: github.ref_name == 'dev'`가 유지된다.
- Dashboard/PostgreSQL에는 R2 secret이 주입되지 않는다.
- 변경 범위가 `dashboard/` 내부다.

### 11-3. commit·push·PR

**작업 위치: `[접속 PC]`**

현재 issue/branch 규칙을 유지해 검토된 파일만 stage한다.

```bash
git add \
  AGENTS.md \
  README.md \
  docs/HERITAGE.md \
  docs/README.md \
  docs/operations.md \
  docs/deployment \
  .dockerignore \
  Dockerfile \
  .github/workflows/deploy.yaml \
  deploy

git diff --cached --check
git diff --cached --name-only
git commit -m "feat: add secure dev server deployment"
git push -u origin feat/3-secure-auth-rbac
```

기존 PR이 있으면 갱신하고, 없으면 `dev` base로 PR을 만든다. PR 본문에는 해당 issue를
`Closes #<issue>`로 연결한다.

```bash
gh pr create \
  --base dev \
  --head feat/3-secure-auth-rbac \
  --title "feat: add secure dev server deployment" \
  --body "Closes #3"
```

PR에서 test, secret 경계, server 경로, `main` 배포 금지를 검토한 뒤 merge한다. 이 문서 작성
작업 자체는 commit·push·merge를 자동 수행하지 않는다.

## 12. `dev` merge 이후 자동 배포 관찰

### 12-1. workflow 순서

**실행 위치: `[GitHub Actions 자동]`**

`dev`에 merge되면 다음 순서로 자동 실행된다.

1. Docker test image build와 전체 test
2. Bash·Dashboard Compose·Trino Compose 계약 검증
3. commit SHA로 고정한 runtime image build·smoke test·GHCR push
4. 검증된 SSH key와 host key로 대상 서버 접속
5. Dashboard·Trino 후보 파일 전송
6. 서버 Docker·Compose·`flock`·`openssl` 재검증
7. Dashboard runtime이 없을 때만 PostgreSQL 비밀번호·session pepper·MFA key 생성
8. R2 secret 6개를 실행별 mode `0600` 임시 파일로 Trino에 주입
9. Trino health, `SELECT 1`, `elt_net` 검증 후 임시 파일 삭제
10. PostgreSQL 기동, 단일 migration, Dashboard·notification worker 기동
11. `/health` 검사. 새 앱 실패 시 직전 앱 Compose/image 복구
12. 성공·실패와 관계없이 GHCR login과 남은 실행별 Trino 임시 파일 정리

### 12-2. Actions 화면 확인

**작업 위치: `[GitHub 웹]`**

저장소 → **Actions → Test, build, and deploy**에서 해당 `dev` commit을 연다.

정상 기준:

- `build` 성공
- `deploy`가 `development` Environment를 사용하고 성공
- log에 R2 값, SSH private key, PostgreSQL 비밀번호가 출력되지 않음
- deployment 대상 commit SHA가 PR merge SHA와 일치

`main` push에서는 `build`와 `main-deployment-disabled`만 성공하고 `deploy`는 실행되지 않아야
한다.

## 13. 서버 배포 결과 검증

**작업 위치: `[접속 PC → 대상 서버]`**

10단계와 같은 SSH 명령으로 접속한 뒤 대상 서버에서 실행한다.

### 13-1. Trino

```bash
docker ps \
  --filter label=com.docker.compose.project=ask-seoul-trino-dev \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

trino_id=$(
  docker ps -q \
    --filter label=com.docker.compose.project=ask-seoul-trino-dev \
    --filter label=com.docker.compose.service=trino
)
test -n "$trino_id"
docker inspect --format 'health={{.State.Health.Status}}' "$trino_id"
docker exec "$trino_id" trino --execute 'SELECT 1'
docker network inspect elt_net --format 'network={{.Name}} driver={{.Driver}}'
```

`health=healthy`, `SELECT 1` 성공, `network=elt_net`이어야 한다. `docker inspect` 전체 출력은
container environment의 R2 값을 포함할 수 있으므로 사용하지 않는다.

### 13-2. Dashboard·PostgreSQL·worker

```bash
cd /home/exi/apps/ask-seoul-dashboard-dev

docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  ps

stat -c '%a %U:%G %n' \
  /home/exi/.config/ask-seoul/dashboard-dev.env

if command -v curl >/dev/null; then
  curl -fsS http://127.0.0.1:8765/health
fi
```

정상 기준:

- `postgres`와 `dashboard`가 healthy다.
- `notification-worker`가 running이다.
- Dashboard port가 `127.0.0.1:8765->8765`로만 bind된다.
- PostgreSQL은 host port를 publish하지 않는다.
- runtime env는 `600`, owner는 `exi`다.

### 13-3. 실행별 R2 임시 파일 정리 확인

```bash
find /home/exi/apps/ask-seoul-trino-dev \
  -maxdepth 1 \
  -type f \
  -name '.runtime.*.env' \
  -printf 'leftover=%f\n'
```

출력이 없어야 한다.

## 14. SSH tunnel과 브라우저 검증

### 14-1. tunnel 열기

**작업 위치: `[접속 PC]`**

```bash
ssh \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -p 3707 \
  -L 8765:127.0.0.1:8765 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org
```

이 terminal을 닫지 않는다.

### 14-2. 브라우저

**작업 위치: `[접속 PC]`**

브라우저에서 `http://127.0.0.1:8765`와 `http://127.0.0.1:8765/health`를 연다.
`localhost`가 아니라 설정과 일치하는 `127.0.0.1`을 사용한다. 로그인 후 카탈로그와
Charts Studio 화면까지 확인한다.

## 15. 최초 관리자와 MFA 등록

**작업 위치: `[접속 PC → 대상 서버]`의 보호된 대화형 TTY**

SSH tunnel session이나 별도 SSH session에서 대상 서버 프롬프트를 확인하고 실행한다.

```bash
cd /home/exi/apps/ask-seoul-dashboard-dev

docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  exec dashboard python scripts/create_admin.py

docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  exec dashboard python scripts/setup_mfa.py
```

이메일·비밀번호·TOTP 값은 사람만 입력한다. MFA seed는 authenticator 등록과 실제 TOTP 확인이
끝나면 폐기하고, 복구 코드는 승인된 password manager 또는 오프라인 보관 위치에 둔다.
AI·Actions·채팅에 입력하지 않는다.

브라우저에서 관리자 로그인, MFA, 운영 콘솔 접근을 확인한다.

## 16. PostgreSQL 최초 backup

### 16-1. 서버에서 dump 생성

**작업 위치: `[대상 서버]`**

```bash
umask 077
install -d -m 700 /home/exi/backups/ask-seoul
cd /home/exi/apps/ask-seoul-dashboard-dev

backup_file="/home/exi/backups/ask-seoul/ask_seoul_$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  exec -T postgres pg_dump \
    -U ask_seoul \
    -d ask_seoul \
    -Fc \
  > "$backup_file"

chmod 600 "$backup_file"
stat -c '%a %s %n' "$backup_file"
```

size가 0보다 크고 mode가 `600`이어야 한다.

### 16-2. 승인된 서버 밖 위치로 복사

**작업 위치: `[접속 PC]`**

대상 서버가 출력한 정확한 파일명을 사용한다.

```bash
mkdir -p ~/Backups/ask-seoul
chmod 700 ~/Backups/ask-seoul

scp \
  -i ~/.ssh/ask-seoul-dashboard-deploy \
  -P 3707 \
  -o IdentitiesOnly=yes \
  exi@exisnet.iptime.org:/home/exi/backups/ask-seoul/<정확한-dump-파일명> \
  ~/Backups/ask-seoul/
```

보존 위치의 암호화·접근권한·복원 시험은 운영 정책에 맞춰 관리한다. backup을 확인하기 전 서버
원본을 삭제하지 않는다.

## 17. 이전 서버 R2 env 제거

이 절은 이번 전환 과정에서만 필요하다. 새로 구축한 서버에는 해당 장기 파일을 만들지 않는다.

### 17-1. 삭제 조건 확인

**작업 위치: `[접속 PC]` 및 `[GitHub 웹]`**

다음을 모두 확인한다.

1. GitHub `development` Environment에 R2 secret 6개 이름이 모두 존재한다.
2. 첫 GitHub-managed `dev` workflow가 성공했다.
3. 대상 서버 Trino가 healthy이고 `SELECT 1`이 성공했다.
4. 허용된 실제 Charts relation 조회가 성공했다.

하나라도 만족하지 않으면 파일을 유지하고 원인을 해결한다.

### 17-2. 장기 파일 삭제

**작업 위치: `[대상 서버]`**

파일 내용을 읽거나 복사하지 않고 존재와 mode만 확인한 뒤 삭제한다.

```bash
stat -c '%a %U:%G %n' /home/exi/.config/ask-seoul/trino-dev.env
rm -f /home/exi/.config/ask-seoul/trino-dev.env
test ! -e /home/exi/.config/ask-seoul/trino-dev.env
```

마지막 `test`가 성공해야 한다. 이후 R2 값은 GitHub Environment에서 실행별 임시 파일로만
주입한다.

## 18. 이후의 일상 배포 순서

1. `[접속 PC]` issue branch에서 변경·test
2. `[접속 PC]` commit·push 후 `dev` 대상 PR 생성
3. `[GitHub 웹]` review·check 성공 후 `dev` merge
4. `[GitHub Actions 자동]` test → SHA image → Trino → migration → Dashboard health
5. `[GitHub 웹]` Actions 성공과 배포 SHA 확인
6. `[접속 PC → 대상 서버]` Compose health와 임시 파일 부재 확인
7. `[접속 PC]` SSH tunnel로 브라우저 기능 확인

`main`에 push해도 서버는 바뀌지 않는다. production 활성화는 도메인·HTTPS reverse proxy,
production PostgreSQL/backup, Secret Manager, required reviewer를 별도 설계하고 승인한 뒤
새 작업으로 진행한다.

## 19. 중단 기준과 금지 작업

즉시 중단하고 검토할 조건:

- GitHub Environment secret 또는 variable이 하나라도 없음
- SSH host fingerprint가 독립 확인값과 다름
- DNF가 Podman/runc package 제거를 제안함
- Docker가 `exi` 권한으로 동작하지 않음
- 8765를 다른 process가 사용 중
- Trino health 또는 `SELECT 1` 실패
- Dashboard runtime mode가 `600`이 아님
- migration 또는 Dashboard health 실패 후 자동 복구도 실패
- Actions log에 secret으로 의심되는 값이 보임

금지:

- `docker compose down -v`
- `docker volume rm`, `docker volume prune`
- `/var/run/docker.sock`의 `chmod 666`
- R2 값, private key, runtime env, MFA 정보를 채팅·log·commit에 복사
- 8765를 `0.0.0.0`이나 공인망에 노출
- `main` 배포 조건을 임의로 활성화
- SQLite/D2 제품과 query 계약이 확정되기 전 현재 Trino를 제거

세부 설명:

- 서버 설치·배포 전제: [server-setup-human.md](server-setup-human.md)
- 서버 AI에게 전달할 제한된 절차: [server-agent-runbook.md](server-agent-runbook.md)
- R2 정본·회전·사고 대응: [r2-secret-management.md](r2-secret-management.md)
- 최초 관리자·backup·장애 운영: [../operations.md](../operations.md)
