#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 3 || $# -gt 4 ]]; then
  echo "usage: deploy.sh IMAGE_REF COMPOSE_PROJECT_NAME RUNTIME_ENV_FILE [DATA_NETWORK_NAME]" >&2
  exit 64
fi

image_ref=$1
project_name=$2
runtime_env_file=$3
data_network_name=${4:-}
deploy_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ ! $image_ref =~ ^ghcr\.io/[a-z0-9._/-]+:[0-9a-f]{40}$ ]]; then
  echo "invalid immutable GHCR image reference" >&2
  exit 64
fi
if [[ ! $project_name =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]]; then
  echo "invalid Compose project name" >&2
  exit 64
fi
if [[ $runtime_env_file != /* || ! -f $runtime_env_file ]]; then
  echo "runtime environment file must exist at an absolute path" >&2
  exit 66
fi
if [[ -n $data_network_name && ! $data_network_name =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]]; then
  echo "invalid Docker data network name" >&2
  exit 64
fi

command -v docker >/dev/null
docker compose version >/dev/null
command -v flock >/dev/null

runtime_mode=$(stat -c '%a' "$runtime_env_file")
if (( 8#$runtime_mode & 077 )); then
  echo "runtime environment file must not be readable by group or others" >&2
  exit 77
fi

cd "$deploy_dir"
install -d -m 755 certificates

exec 9>.deploy.lock
if ! flock -n 9; then
  echo "another dashboard deployment is already running" >&2
  exit 75
fi

candidate_compose=compose.yaml.candidate
candidate_network_compose=compose.data-network.yaml.candidate
candidate_env=.deploy.env.candidate
current_compose=compose.yaml
current_network_compose=compose.data-network.yaml
current_env=.deploy.env

if [[ ! -f $candidate_compose || ! -f $candidate_network_compose ]]; then
  echo "candidate Compose files are missing" >&2
  exit 66
fi

write_deploy_env() {
  local path=$1
  local ref=$2
  local runtime_file=$3
  local network_name=$4
  (
    umask 077
    {
      printf 'IMAGE_REF=%s\n' "$ref"
      printf 'RUNTIME_ENV_FILE=%s\n' "$runtime_file"
      printf 'DASHBOARD_DATA_NETWORK=%s\n' "$network_name"
    } > "$path"
  )
}

compose_run() {
  local compose_file=$1
  local deploy_env=$2
  local network_compose=$3
  shift 3

  local args=(
    docker compose
    --env-file "$runtime_env_file"
    --env-file "$deploy_env"
    --project-name "$project_name"
    --file "$compose_file"
  )
  if [[ -n $network_compose ]]; then
    args+=(--file "$network_compose")
  fi
  "${args[@]}" "$@"
}

network_file_for() {
  local deploy_env=$1
  local network_compose=$2
  if grep -Eq '^DASHBOARD_DATA_NETWORK=.+$' "$deploy_env"; then
    printf '%s' "$network_compose"
  fi
}

wait_for_healthy() {
  local compose_file=$1
  local deploy_env=$2
  local network_compose=$3
  local container_id
  local status

  for _ in {1..60}; do
    container_id=$(compose_run "$compose_file" "$deploy_env" "$network_compose" ps -q dashboard)
    if [[ -n $container_id ]]; then
      status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")
      if [[ $status == healthy ]]; then
        return 0
      fi
      if [[ $status == exited || $status == dead ]]; then
        return 1
      fi
    fi
    sleep 2
  done
  return 1
}

write_deploy_env "$candidate_env" "$image_ref" "$runtime_env_file" "$data_network_name"
candidate_network=
if [[ -n $data_network_name ]]; then
  candidate_network=$candidate_network_compose
fi

compose_run "$candidate_compose" "$candidate_env" "$candidate_network" config --quiet
compose_run "$candidate_compose" "$candidate_env" "$candidate_network" --profile tools pull \
  postgres dashboard notification-worker migrate

had_previous=false
previous_network=
if [[ -f $current_compose && -f $current_env ]]; then
  had_previous=true
  cp -p "$current_compose" compose.yaml.previous
  cp -p "$current_env" .deploy.env.previous
  if [[ -f $current_network_compose ]]; then
    cp -p "$current_network_compose" compose.data-network.yaml.previous
  fi
  previous_network=$(network_file_for .deploy.env.previous compose.data-network.yaml.previous)
  compose_run "$current_compose" "$current_env" \
    "$(network_file_for "$current_env" "$current_network_compose")" \
    stop dashboard notification-worker || true
fi

if ! compose_run "$candidate_compose" "$candidate_env" "$candidate_network" \
  --profile tools run --rm migrate; then
  echo "database migration failed; restarting the previous application" >&2
  if [[ $had_previous == true ]]; then
    compose_run "$current_compose" "$current_env" \
      "$(network_file_for "$current_env" "$current_network_compose")" \
      up -d dashboard notification-worker
  fi
  exit 1
fi

mv "$candidate_compose" "$current_compose"
mv "$candidate_network_compose" "$current_network_compose"
mv "$candidate_env" "$current_env"

current_network=
if [[ -n $data_network_name ]]; then
  current_network=$current_network_compose
fi

deployment_started=false
if compose_run "$current_compose" "$current_env" "$current_network" up -d --remove-orphans; then
  deployment_started=true
fi

if [[ $deployment_started == true ]] \
  && wait_for_healthy "$current_compose" "$current_env" "$current_network"; then
  if [[ -f deploy.sh.candidate ]]; then
    chmod 700 deploy.sh.candidate
    mv deploy.sh.candidate deploy.sh
  fi
  echo "dashboard deployment is healthy: $image_ref"
  exit 0
fi

echo "new dashboard did not become healthy" >&2
compose_run "$current_compose" "$current_env" "$current_network" ps >&2 || true

if [[ $had_previous != true ]]; then
  echo "no previous deployment is available for rollback" >&2
  exit 1
fi

cp -p compose.yaml.previous "$current_compose"
cp -p .deploy.env.previous "$current_env"
if [[ -f compose.data-network.yaml.previous ]]; then
  cp -p compose.data-network.yaml.previous "$current_network_compose"
fi

compose_run "$current_compose" "$current_env" "$previous_network" up -d --remove-orphans
if wait_for_healthy "$current_compose" "$current_env" "$previous_network"; then
  echo "rollback completed; deployment remains failed" >&2
else
  echo "rollback also failed; operator intervention is required" >&2
fi
exit 1
