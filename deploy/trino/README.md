# Transitional dev Trino

이 Compose는 대상 dev 서버에 기존 Trino가 없을 때만 사용하는 최소 companion이다.
Airflow·dbt·수집기는 포함하지 않으며 Dashboard의 live Charts 질의를 현재 R2/Iceberg dev
catalog로 연결한다. SQLite/D2 query adapter가 확정되면 별도 migration 후 제거한다.

Rocky 대상에 Docker가 없으면 같은 디렉터리의 `install-docker-rocky.sh`를 사람이
`sudo`로 실행한다. 이 script는 기존 Podman을 자동 삭제하거나 `--allowerasing`하지 않는다.

설정의 정본은 상위 `sample/trino/`다. 다음 파일은 해당 설정의 배포용 사본이므로 상위 설정을
변경할 때 같은 변경에서 동기화하고 diff로 검증한다.

```bash
diff -u ../../../trino/config.properties config.properties
diff -u ../../../trino/jvm.config jvm.config
diff -u ../../../trino/resource-groups.properties resource-groups.properties
diff -u ../../../trino/resource-groups.json resource-groups.json
diff -u ../../../trino/catalog/iceberg_dev.properties catalog/iceberg_dev.properties
```

R2 값의 배포 정본은 GitHub `development` Environment secrets다. 최초 migration은 사람이
신뢰된 로컬 PC에서 다음을 한 번 실행한다.

```bash
cd <sample>
dashboard/deploy/trino/register-github-secrets.sh .env
```

workflow는 실행별 고유한 server `.runtime.<run-id>.<attempt>.env`를 mode `0600`으로 만들고
`deploy.sh`에 넘긴다. `deploy.sh`는 Compose create/recreate, health, `SELECT 1`, `elt_net`을
검증한 뒤 성공·실패와 관계없이 해당 파일을 삭제한다. 별도 `if: always()` cleanup도 같은
경로만 제거한다.

서버에 `/home/exi/.config/ask-seoul/trino-dev.env` 같은 장기 R2 파일을 두지 않는다. 전체 계약,
회전, 사고 대응은
[R2/Trino secret 관리 문서](../../docs/deployment/r2-secret-management.md)를 따른다.

서버에서는 secret env 없이 container 상태를 확인한다.

```bash
docker ps \
  --filter label=com.docker.compose.project=ask-seoul-trino-dev \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

trino_id=$(
  docker ps -q \
    --filter label=com.docker.compose.project=ask-seoul-trino-dev \
    --filter label=com.docker.compose.service=trino
)
docker inspect --format 'health={{.State.Health.Status}}' "$trino_id"
docker exec "$trino_id" trino --execute 'SELECT 1'
```

`elt_net`과 DNS alias `trino`는 이 Compose가 만든다. Dashboard Compose는 해당 network에
external로 참가한다. PostgreSQL이나 Dashboard에는 R2 변수를 주입하지 않는다.
