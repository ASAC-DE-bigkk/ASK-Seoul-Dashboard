#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_env_file=${1:-"$script_dir/../../../.env"}
repository=${2:-ASAC-DE-bigkk/ASK-Seoul-Dashboard}
environment=${3:-development}

if [[ ! -f $source_env_file ]]; then
  echo "source env file does not exist: $source_env_file" >&2
  exit 66
fi
command -v gh >/dev/null
gh auth status >/dev/null

read_dotenv_value() {
  local requested_key=$1
  awk -v requested_key="$requested_key" '
    index($0, requested_key "=") == 1 {
      print substr($0, length(requested_key) + 2)
      found++
    }
    END {
      if (found != 1) exit 42
    }
  ' "$source_env_file"
}

secret_names=(
  R2_DEV_DATA_CATALOG_URI
  R2_DEV_DATA_CATALOG_WAREHOUSE
  R2_DEV_DATA_CATALOG_TOKEN
  R2_DEV_ENDPOINT
  R2_DEV_ACCESS_KEY_ID
  R2_DEV_SECRET_ACCESS_KEY
)

for secret_name in "${secret_names[@]}"; do
  secret_value=$(read_dotenv_value "$secret_name")
  if [[ -z $secret_value || $secret_value == *$'\n'* || $secret_value == *$'\r'* ]]; then
    echo "invalid value in source env: $secret_name" >&2
    exit 78
  fi
  printf '%s' "$secret_value" |
    gh secret set "$secret_name" \
      --env "$environment" \
      --repo "$repository"
  unset secret_value
  echo "registered: $secret_name"
done

echo "registered R2 development secrets without writing a plaintext intermediate file"
