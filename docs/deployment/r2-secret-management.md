# R2/Trino secret 관리 계약

대상: dev 배포를 운영하는 사람과 AI.

## 결론

dev R2/Trino 값의 배포 정본은 GitHub `development` Environment secrets다. 값을 저장소,
Dashboard runtime, 서버 장기 `.env`, 채팅, Actions log에 복사하지 않는다.

```text
GitHub development Environment secrets
                  │
                  │ dev deploy job에서만 참조
                  ▼
GitHub runner mode 0600 임시 파일
                  │ SSH
                  ▼
<TRINO_DEPLOY_PATH>/.runtime.<run-id>.<attempt>.env
                  │
        Trino Compose create/recreate
                  │
                  └─ 성공·실패와 관계없이 즉시 삭제
```

GitHub Environment는 `dev` branch deployment만 허용한다. `main` job은 이 Environment를
참조하지 않으며 서버 배포도 하지 않는다. Environment secret은 해당 Environment를 참조하는
job이 시작될 때만 사용할 수 있다.

## 1. 정본 secret

다음 6개를 모두 `development` Environment secret으로 저장한다.

| 이름 | 소비자 |
|---|---|
| `R2_DEV_DATA_CATALOG_URI` | Trino Iceberg REST catalog |
| `R2_DEV_DATA_CATALOG_WAREHOUSE` | Trino Iceberg warehouse |
| `R2_DEV_DATA_CATALOG_TOKEN` | Trino REST catalog OAuth2 |
| `R2_DEV_ENDPOINT` | Trino native S3 client |
| `R2_DEV_ACCESS_KEY_ID` | Trino native S3 client |
| `R2_DEV_SECRET_ACCESS_KEY` | Trino native S3 client |

Dashboard와 PostgreSQL에는 어떤 R2 값도 주입하지 않는다. Dashboard는 `elt_net`에서
`http://trino:8080`만 사용한다.

## 2. 최초 등록

최초 migration만 기존 상위 `sample/.env`를 입력으로 사용한다. 사람이 신뢰된 로컬 PC에서
다음 script를 실행한다.

```bash
cd <sample>
dashboard/deploy/trino/register-github-secrets.sh .env
```

script는:

- 정확히 위 6개 이름만 읽는다.
- 평문 중간 파일을 만들지 않는다.
- secret 값을 command argument나 stdout에 넣지 않는다.
- `gh secret set ...`의 stdin으로 한 항목씩 전달한다.
- 실제 값 대신 등록된 secret 이름만 출력한다.

AI가 사용자를 대신해 secret 값을 읽거나 외부로 전송하지 않는다. 등록 후 이름만 검증한다.

```bash
gh secret list \
  --env development \
  --repo ASAC-DE-bigkk/ASK-Seoul-Dashboard
```

GitHub는 secret 값을 다시 조회할 수 없으므로 등록 이후에는 GitHub가 배포 정본이다.
상위 로컬 `.env`는 로컬 데이터 스택에 여전히 필요할 수 있지만 서버 배포 입력으로 사용하지 않는다.

## 3. 배포 시 처리

`.github/workflows/deploy.yaml`의 dev deploy job만 다음 순서를 수행한다.

1. secret 6개의 존재와 CR/LF 부재를 검사한다.
2. Trino config 파일들의 SHA-256 revision을 계산한다.
3. GitHub runner의 임시 디렉터리에 mode `0600` env를 만든다.
4. 검증된 SSH host key를 사용해 서버의 실행별 고유 임시 경로로 전송한다.
5. `deploy/trino/deploy.sh`가 mode와 필수 key 이름만 검사한다.
6. `docker compose config --quiet`의 출력을 버리고 Trino를 create/recreate한다.
7. Trino health와 `SELECT 1`, `elt_net`을 검증한다.
8. local/remote 임시 파일을 성공·실패와 관계없이 삭제한다.

Compose config revision이나 secret 값이 바뀌면 Trino container가 재생성된다. 값이 같으면
불필요한 재생성을 하지 않는다.

Trino가 환경변수로 자격증명을 소비하므로 Docker daemon의 container metadata에는 실행 중인
값이 존재한다. Docker group은 사실상 root 권한이므로 `exi` 외 계정을 임의로 추가하지 않고,
`docker inspect` 결과를 채팅이나 log에 붙이지 않는다.

## 4. 서버에 남아야 하는 것

장기 보존:

```text
/home/exi/apps/ask-seoul-trino-dev/compose.yaml
/home/exi/apps/ask-seoul-trino-dev/catalog/
/home/exi/apps/ask-seoul-trino-dev/*.properties
/home/exi/apps/ask-seoul-trino-dev/resource-groups.json
```

남으면 안 되는 것:

```text
/home/exi/.config/ask-seoul/trino-dev.env
/home/exi/apps/ask-seoul-trino-dev/.runtime.*.env
```

이전 장기 파일은 GitHub secret 6개 등록과 첫 GitHub-managed Trino health 성공 후 삭제한다.
첫 성공 전에 삭제할 경우 복구 입력은 GitHub에서 읽을 수 없으므로 Cloudflare에서 다시 발급해야
할 수 있다.

## 5. 회전

R2 S3 key 또는 Data Catalog token 회전 순서:

1. Cloudflare에서 기존보다 좁거나 같은 권한으로 새 credential을 발급한다.
2. 해당 GitHub Environment secret을 새 값으로 덮어쓴다.
3. `dev` workflow를 실행해 Trino가 healthy이고 허용된 relation을 읽는지 확인한다.
4. 기존 credential을 Cloudflare에서 폐기한다.
5. Actions deployment 기록에 성공 SHA와 회전 시각을 남긴다.

먼저 기존 값을 폐기하면 rollback과 검증이 동시에 막힐 수 있으므로 새 값 검증 후 폐기한다.
노출이 의심되는 사고 대응에서는 예외로 기존 credential을 즉시 폐기한 뒤 새 값을 등록한다.

## 6. 금지 사항

- 실제 R2 값이 들어 있는 `.env`를 `scp`, 메일, 메신저, 채팅으로 전달
- secret 값을 workflow job/step output, debug trace, `docker compose config`에 출력
- R2 secret을 Dashboard runtime 또는 PostgreSQL environment에 복제
- `main` workflow나 pull request job에서 `development` Environment 참조
- 장기 server `.env`를 “백업”이라는 이름으로 별도 복사
- AI가 Cloudflare credential을 임의 발급·폐기

## 7. production 전환

현재 단일 dev 서버에는 GitHub Environment가 운영 복잡도 대비 적절하다. `main` production을
활성화할 때는 required reviewer와 branch protection을 먼저 적용하고, 중앙 감사·자동 회전이
필요하면 외부 Secret Manager를 별도 설계한다. SOPS/age, Vault, Infisical 같은 새 구성요소는
bootstrap key와 복구 절차가 추가되므로 별도 승인 없이 도입하지 않는다.

참고:

- [GitHub deployment environments](https://docs.github.com/en/actions/concepts/workflows-and-actions/deployment-environments)
- [GitHub Actions secrets](https://docs.github.com/en/actions/reference/security/secrets)
- [Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [Cloudflare R2 authentication](https://developers.cloudflare.com/r2/api/tokens/)
