# dev 서버 준비·검증 runbook — 서버 AI용

대상: `exisnet.iptime.org` 서버 안에서 shell을 사용할 수 있는 AI 에이전트.

목표는 `dev` 자동 배포의 사전 조건을 안전하게 확인하고, 사람이 명시적으로 허가한 범위만
준비하며, 결과를 secret 없이 보고하는 것이다. 이 문서는 AI에게 무제한 관리자 권한이나
production 변경 권한을 부여하지 않는다.

## 0. 먼저 읽을 정본

repository checkout이 있다면 다음 순서로 읽는다.

1. `dashboard/AGENTS.md`
2. `dashboard/docs/HERITAGE.md`
3. 이 문서
4. `dashboard/deploy/compose.yaml`
5. `dashboard/deploy/compose.data-network.yaml`
6. `dashboard/deploy/trino/README.md`
7. `dashboard/deploy/trino/compose.yaml`
8. `dashboard/deploy/bootstrap-dev.sh`
9. `dashboard/deploy/deploy.sh`
10. `dashboard/.github/workflows/deploy.yaml`

현재 R2/Trino 연결을 확인할 때만 상위 `trino/catalog/iceberg_dev.properties`를 읽는다.
배포 secret 정본은 GitHub `development` Environment이며
`docs/deployment/r2-secret-management.md`를 따른다. 상위 `sample/.env`는 최초 human-owned
migration 입력일 뿐 서버 AI가 읽거나 전송할 대상이 아니다.

## 1. 실행 범위

허용된 기본 작업:

- read-only 서버 inventory
- Docker/Compose/network/container 상태 조회
- 포트·디스크·메모리·파일 존재 여부와 mode 확인
- 배포 후 Compose 상태, health, 제한된 tail log 확인
- 발견한 blocker와 필요한 사람 작업 보고

사전 확인 후 사람의 명시적 승인이 있어야 하는 작업:

- OS package 설치 또는 사용자 group 변경
- 배포 디렉터리 생성
- `bootstrap-dev.sh`로 runtime env 최초 생성
- 수동 Compose 기동·중지·재배포
- DB backup 또는 restore

AI가 수행하면 안 되는 작업:

- `main` 서버 배포를 활성화하거나 workflow 조건을 제거
- branch merge, push, GitHub secret 생성·변경
- SSH `authorized_keys`, sshd, 공유기 port forwarding, 방화벽 변경
- 8765를 `0.0.0.0` 또는 공인망에 노출
- reverse proxy, 공개 도메인, TLS 구성을 임의로 추가
- `sample/.env`, Trino catalog, R2 endpoint/token/access key/secret key 변경
- runtime env, DSN 비밀번호, session pepper, MFA key의 값 출력
- `docker compose down -v`, `docker volume rm`, `docker volume prune`
- 관리자 계정 생성, MFA 등록·재등록, 비밀번호·TOTP·복구 코드 입력
- 제품/API가 확정되지 않은 SQLite/D2 전환

## 2. 1차 inventory — 변경 금지

아래 명령은 read-only로 실행한다. 일부 명령이 실패해도 바로 설치하거나 권한을 넓히지 않는다.

```bash
id
uname -m
cat /etc/os-release
docker version --format 'server={{.Server.Version}}' 2>&1
docker compose version 2>&1
command -v flock
command -v openssl
docker info --format 'architecture={{.Architecture}} root={{.DockerRootDir}}' 2>&1
docker network inspect elt_net --format 'name={{.Name}} driver={{.Driver}} scope={{.Scope}}' 2>&1
docker ps --filter network=elt_net --format 'container={{.Names}} status={{.Status}}'
df -h /var/lib/docker
free -h
ss -ltn 'sport = :8765'
```

다음 파일은 값이 아니라 존재와 권한만 확인한다.

```bash
test -d /home/exi/apps/ask-seoul-dashboard-dev \
  && stat -c 'deploy_dir mode=%a owner=%U:%G' /home/exi/apps/ask-seoul-dashboard-dev \
  || echo 'deploy_dir absent'

test -f /home/exi/.config/ask-seoul/dashboard-dev.env \
  && stat -c 'runtime_env mode=%a owner=%U:%G' /home/exi/.config/ask-seoul/dashboard-dev.env \
  || echo 'runtime_env absent'

test ! -e /home/exi/.config/ask-seoul/trino-dev.env \
  && echo 'legacy_trino_runtime absent' \
  || echo 'legacy_trino_runtime present'

find /home/exi/apps/ask-seoul-trino-dev \
  -maxdepth 1 \
  -type f \
  -name '.runtime.*.env' \
  -printf 'leftover_ephemeral=%f\n' 2>/dev/null
```

runtime env에 `cat`, `source`, `set -x`, 전체 `grep`을 실행하거나 내용을 응답에 붙이지 않는다.
기존 파일의 계약 검사는 승인 후 `bootstrap-dev.sh` 자체에 맡긴다. 이 script는 secret 값을
출력하지 않고 mode와 PostgreSQL DSN 형태만 검사한다.

## 3. 판정표

| 관찰 결과 | 판정과 다음 행동 |
|---|---|
| `uname -m` 또는 Docker architecture가 `x86_64/amd64`가 아님 | 중단. 현재 이미지는 호환되지 않는다고 보고 |
| Docker daemon/Compose 없음 | 중단. OS와 정확한 오류를 보고하고 설치 승인 요청 |
| `exi`가 Docker socket에 접근 못 함 | 중단. `chmod`하지 말고 서버 관리자에게 권한 모델 결정 요청 |
| `flock` 없음 | OS가 apt 계열이면 `util-linux`, dnf 계열이면 `util-linux` 설치 승인 요청 |
| `openssl` 없음 | `openssl` package 설치 승인 요청 |
| `elt_net` 없음 | 중단. AI가 임의 network를 만들지 말고 기존 데이터 스택 담당자에게 확인 요청 |
| `elt_net`에 Trino가 없거나 unhealthy | 중단. 기존 Trino Compose를 먼저 복구하도록 보고 |
| 8765가 다른 프로세스에 사용 중 | 중단. 프로세스를 종료하지 말고 owner와 충돌을 보고 |
| runtime env가 없고 merge 전 | 정상. 아무것도 생성하지 않고 대기 |
| runtime env가 없고 첫 `dev` 배포 승인 후 | workflow가 `bootstrap-dev.sh`로 생성하도록 둠 |
| runtime env mode가 `600`이 아님 | 중단. 내용을 읽거나 자동 수정하지 말고 보고 |
| 장기 `trino-dev.env` 또는 `.runtime.*.env`가 남음 | 값을 읽지 말고 경로만 보고. active 배포가 아님을 확인한 뒤 승인된 cleanup만 수행 |
| Docker disk 또는 메모리 여유가 불명확 | 수치와 현재 컨테이너 상태를 보고하고 사람 판단 요청 |

기존 Docker가 동작하면 버전 업그레이드나 재설치를 제안하지 않는다. 설치가 승인되면
[Docker 공식 Engine 설치](https://docs.docker.com/engine/install/)와
[Compose plugin 설치](https://docs.docker.com/compose/install/linux/)만 기준으로 삼는다.
편의 스크립트나 비공식 저장소는 사용하지 않는다.

대상 서버에 기존 Trino가 없는 것이 확인되고 사람이 최소 companion 배포를 승인했다면
`dashboard/deploy/trino/README.md`가 예외적으로 `elt_net`을 만드는 정본이다. 이 경우에만
해당 Compose가 network와 `trino` DNS alias를 만들 수 있다. R2 6개 값은 GitHub
`development` Environment에서 workflow 실행 동안만 일시 주입한다. 서버 AI는 R2 값을
읽거나 장기 파일을 만들지 않고, secret 이름·임시 파일 부재·Trino health만 검증한다.

## 4. 정상 준비 경로

권장 경로는 서버 AI의 수동 배포가 아니라 GitHub Actions다.

1. 사람에게 2차 inventory 결과를 보고한다.
2. 사람이 배포 전용 SSH public key와 검증된 host key를 준비한다.
3. 사람이 GitHub `development` Environment 변수와 secret을 등록한다.
4. PR이 `dev`에 merge된다.
5. workflow가 후보 파일 업로드, runtime bootstrap, migration, Compose up, health gate,
   실패 시 직전 앱 복구를 수행한다.

서버에 repository clone이나 Python virtualenv는 필요하지 않다. workflow가 필요한 Compose와
script를 `/home/exi/apps/ask-seoul-dashboard-dev`에 전송하고 GHCR image를 pull한다.

## 5. 승인된 수동 준비

사람이 “서버 사전 준비를 실행하라”고 명시한 경우에만 디렉터리를 만들 수 있다.

```bash
install -d -m 700 /home/exi/apps/ask-seoul-dashboard-dev
install -d -m 700 /home/exi/.config/ask-seoul
```

runtime env 생성은 `dev` merge 이후, repository의 해당 commit에 있는
`deploy/bootstrap-dev.sh`를 확인했고 사람이 최초 생성을 승인한 경우에만 실행한다.

```bash
dashboard/deploy/bootstrap-dev.sh \
  /home/exi/.config/ask-seoul/dashboard-dev.env

stat -c '%a %U:%G %n' \
  /home/exi/.config/ask-seoul/dashboard-dev.env
```

결과가 `600 exi:...`가 아니면 중단한다. script가 기존 파일을 거부하면 덮어쓰거나 지우지 않는다.
오류 문구만 보고한다.

`deploy.sh`는 GHCR의 40자리 commit SHA image ref와 workflow가 올린 candidate 파일을 요구한다.
AI가 임의 tag(`latest`, branch tag)나 로컬 build로 대신 실행하지 않는다.

## 6. 배포 후 read-only 검증

GitHub Actions가 성공했거나 사람이 배포 완료를 알린 뒤 실행한다.

```bash
cd /home/exi/apps/ask-seoul-dashboard-dev

docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  ps
```

검증 기준:

- `postgres`는 healthy다.
- `dashboard`는 healthy다.
- `notification-worker`는 running이다.
- dashboard port mapping은 `127.0.0.1:8765->8765`이며 공인 bind가 아니다.
- `dashboard`와 `notification-worker`는 `elt_net`에 연결된다.
- PostgreSQL은 host port를 publish하지 않는다.
- runtime env mode는 `600`이다.

container health를 직접 확인할 수 있다.

```bash
dashboard_id=$(
  docker compose \
    --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
    --env-file .deploy.env \
    --project-name ask-seoul-dashboard-dev \
    -f compose.yaml \
    -f compose.data-network.yaml \
    ps -q dashboard
)
docker inspect --format 'dashboard health={{.State.Health.Status}}' "$dashboard_id"
```

호스트에 `curl`이 이미 있을 때만 local bind도 확인한다.

```bash
curl -fsS http://127.0.0.1:8765/health
```

응답은 health 상태만 보고하고 body에 예상 밖의 민감정보가 있으면 그대로 복사하지 않는다.

## 7. 장애 확인과 복구 경계

health가 실패하면 먼저 상태와 마지막 100줄만 확인한다.

```bash
docker compose \
  --env-file /home/exi/.config/ask-seoul/dashboard-dev.env \
  --env-file .deploy.env \
  --project-name ask-seoul-dashboard-dev \
  -f compose.yaml \
  -f compose.data-network.yaml \
  logs --tail 100 dashboard postgres notification-worker
```

- log에 DSN, token, 이메일, 세션 값이 보이면 응답에서 마스킹한다.
- 새 앱 health 실패 시 `deploy.sh`가 직전 Compose/image를 복구한다.
- migration 실패 시 이전 앱을 다시 기동한다.
- additive migration은 자동 rollback되지 않는다.
- `.deploy.env.previous`와 `compose.yaml.previous`는 복구 근거다. 내용을 임의 편집하지 않는다.
- 자동 복구도 실패하면 컨테이너/volume을 삭제하지 말고 상태와 오류를 보고한다.

DB 변경·복원 전에는 사람에게 백업 경로와 보존 위치를 확인한다. 승인된 백업은
[운영 매뉴얼](../operations.md#5-5-배포-후-최초-관리자)의 `pg_dump` 계약을 사용한다.
AI 판단으로 restore, schema downgrade, volume 재생성을 하지 않는다.

## 8. 사람에게 넘길 작업

다음은 항상 사람에게 넘긴다.

- 배포 전용 private key와 GitHub Environment secret 등록
- SSH host key fingerprint의 독립 채널 비교
- PR review와 `dev` merge
- 최초 관리자 생성과 MFA 등록/복구 코드 보관
- SSH tunnel을 통한 브라우저 로그인·Charts 실질의 확인
- production/main 활성화 결정
- SQLite/D2 제품, API, 보존·백필 계약 결정

## 9. 보고 형식

secret 값을 포함하지 않고 아래 형식으로 보고한다.

```text
대상: exisnet.iptime.org:3707 / exi
모드: dev 준비 또는 dev 배포 후 검증
아키텍처: x86_64 | 기타
Docker/Compose: 정상 | 누락 | 권한 오류
flock/openssl: 정상 | 누락
elt_net/Trino: 정상 | 누락 | unhealthy
포트 8765: 비어 있음 | 기존 listener 있음
배포 경로: 없음 | mode/owner만
runtime env: 없음 | mode/owner만 (값 미열람)
R2 runtime 잔여물: 없음 | 경로만 보고
디스크/메모리: 조회 수치
수행한 변경: 없음 또는 승인받은 항목만
배포 상태: 미실행 | healthy | 자동복구 | 실패
blocker/사람 작업: 구체적인 다음 한 단계
```

`main`은 “배포 미실행이 정상”이라고 보고한다. `main`에서 image가 만들어졌다는 이유로 서버
작업을 이어가지 않는다.
