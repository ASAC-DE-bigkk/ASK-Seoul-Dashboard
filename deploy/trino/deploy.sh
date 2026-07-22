#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: deploy.sh EPHEMERAL_RUNTIME_ENV COMPOSE_PROJECT_NAME NETWORK_NAME" >&2
  exit 64
fi

runtime_env_file=$1
project_name=$2
network_name=$3
deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ $runtime_env_file != "$deploy_dir"/.runtime.*.env || ! -f $runtime_env_file ]]; then
  echo "ephemeral runtime file must be inside the Trino deployment directory" >&2
  exit 66
fi
if [[ ! $project_name =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
  echo "invalid Compose project name" >&2
  exit 64
fi
if [[ ! $network_name =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]]; then
  echo "invalid Docker network name" >&2
  exit 64
fi

cleanup() {
  rm -f "$runtime_env_file"
}
trap cleanup EXIT

runtime_mode=$(stat -c '%a' "$runtime_env_file")
if (( 8#$runtime_mode & 077 )); then
  echo "ephemeral runtime file must have mode 0600" >&2
  exit 77
fi

required_keys=(
  R2_DEV_DATA_CATALOG_URI
  R2_DEV_DATA_CATALOG_WAREHOUSE
  R2_DEV_DATA_CATALOG_TOKEN
  R2_DEV_ENDPOINT
  R2_DEV_ACCESS_KEY_ID
  R2_DEV_SECRET_ACCESS_KEY
  TRINO_CONFIG_REVISION
)
for key in "${required_keys[@]}"; do
  if [[ $(grep -c "^${key}=." "$runtime_env_file") -ne 1 ]]; then
    echo "ephemeral runtime is missing a required value: $key" >&2
    exit 78
  fi
done

command -v docker >/dev/null
docker compose version >/dev/null
command -v flock >/dev/null

cd "$deploy_dir"
exec 9>.deploy.lock
if ! flock -n 9; then
  echo "another Trino deployment is already running" >&2
  exit 75
fi

compose=(
  docker compose
  --env-file "$runtime_env_file"
  --project-name "$project_name"
  --file compose.yaml
)

"${compose[@]}" config --quiet >/dev/null
"${compose[@]}" pull trino
"${compose[@]}" up -d trino

healthy=false
for _ in {1..60}; do
  container_id=$("${compose[@]}" ps -q trino)
  if [[ -n $container_id ]]; then
    status=$(docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$container_id")
    if [[ $status == healthy ]]; then
      healthy=true
      break
    fi
    if [[ $status == exited || $status == dead ]]; then
      break
    fi
  fi
  sleep 2
done

if [[ $healthy != true ]]; then
  echo "Trino did not become healthy" >&2
  "${compose[@]}" ps >&2 || true
  exit 1
fi

"${compose[@]}" exec -T trino trino --execute 'SELECT 1' >/dev/null
docker network inspect "$network_name" >/dev/null
echo "Trino companion is healthy on network: $network_name"
