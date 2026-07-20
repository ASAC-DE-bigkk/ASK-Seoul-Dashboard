#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: bootstrap-dev.sh RUNTIME_ENV_FILE" >&2
  exit 64
fi

runtime_env_file=$1
if [[ $runtime_env_file != /* ]]; then
  echo "runtime environment file must use an absolute path" >&2
  exit 64
fi

runtime_env_dir=${runtime_env_file%/*}
if [[ ! -d $runtime_env_dir ]]; then
  install -d -m 700 "$runtime_env_dir"
fi

file_mode() {
  local path=$1
  if stat -c '%a' "$path" >/dev/null 2>&1; then
    stat -c '%a' "$path"
  else
    stat -f '%Lp' "$path"
  fi
}

if [[ -f $runtime_env_file ]]; then
  mode=$(file_mode "$runtime_env_file")
  if [[ $mode != 600 ]]; then
    echo "existing runtime environment file must have mode 0600" >&2
    exit 77
  fi
  if ! grep -q '^DATABASE_URL=postgresql+psycopg://ask_seoul:.*@postgres:5432/ask_seoul$' \
    "$runtime_env_file"; then
    echo "existing runtime environment file is not the expected PostgreSQL dev configuration" >&2
    exit 78
  fi
  echo "existing dev runtime environment preserved"
  exit 0
fi

command -v openssl >/dev/null

postgres_password=$(openssl rand -hex 32)
session_pepper=$(openssl rand -hex 32)
mfa_master_key=$(openssl rand -hex 32)
temporary_file=$(mktemp "$runtime_env_dir/.dashboard-dev.env.XXXXXX")
trap 'rm -f "$temporary_file"' EXIT

umask 077
{
  echo "DASHBOARD_BIND_HOST=127.0.0.1"
  echo "DASHBOARD_PORT=8765"
  echo "DASHBOARD_MEMORY_LIMIT=1g"
  echo "DASHBOARD_CPUS=1.0"
  echo "DASHBOARD_PIDS_LIMIT=256"
  echo "UVICORN_WORKERS=1"
  echo "HEALTHCHECK_HOST=127.0.0.1"
  echo
  echo "POSTGRES_USER=ask_seoul"
  echo "POSTGRES_PASSWORD=$postgres_password"
  echo "POSTGRES_DB=ask_seoul"
  echo "DATABASE_URL=postgresql+psycopg://ask_seoul:$postgres_password@postgres:5432/ask_seoul"
  echo
  echo "AUTH_ENV=development"
  echo "AUTH_PUBLIC_BASE_URL=http://127.0.0.1:8765"
  echo "AUTH_ALLOWED_HOSTS=127.0.0.1,localhost"
  echo "AUTH_COOKIE_SECURE=false"
  echo "AUTH_TRUST_PROXY_HEADERS=false"
  echo "AUTH_BIND_SESSION_USER_AGENT=true"
  echo "AUTH_BIND_SESSION_IP=false"
  echo "AUTH_REQUIRE_MFA_FOR_PRIVILEGED=false"
  echo "AUTH_SESSION_PEPPER=$session_pepper"
  echo "AUTH_MFA_MASTER_KEY=$mfa_master_key"
  echo "AUTH_AUTO_APPROVE_VERIFIED=true"
  echo "AUTH_SESSION_HOURS=12"
  echo "AUTH_SESSION_IDLE_MINUTES=120"
  echo "AUTH_MAX_SESSIONS_PER_USER=10"
  echo "AUTH_REMEMBER_DAYS=30"
  echo "AUTH_REMEMBER_IDLE_DAYS=7"
  echo "AUTH_RESET_TOKEN_MINUTES=30"
  echo "AUTH_VERIFY_TOKEN_HOURS=24"
  echo "AUTH_MAX_REQUEST_BYTES=262144"
  echo
  echo "CHARTS_TRINO_URL=http://trino:8080"
  echo "CHARTS_TRINO_USER=charts-studio"
  echo "CHARTS_CACHE_TTL=600"
  echo "CHARTS_MAX_CONCURRENT_QUERIES=4"
  echo
  echo "NOTIFICATION_INTERVAL_SECONDS=60"
  echo "NOTIFICATION_BATCH_SIZE=100"
  echo "NOTIFICATION_STALE_MINUTES=5"
  echo "NOTIFY_DISCORD_WEBHOOK_URL="
  echo "NOTIFY_SLACK_WEBHOOK_URL="
  echo "NOTIFY_TELEGRAM_BOT_TOKEN="
  echo "NOTIFY_TELEGRAM_CHAT_IDS="
  echo
  echo "SMTP_HOST="
  echo "SMTP_PORT=587"
  echo "SMTP_USERNAME="
  echo "SMTP_PASSWORD="
  echo "SMTP_FROM_EMAIL="
  echo "SMTP_USE_TLS=true"
  echo "SMTP_USE_SSL=false"
  echo "SMTP_ALLOW_PLAINTEXT=false"
} > "$temporary_file"

chmod 600 "$temporary_file"
mv "$temporary_file" "$runtime_env_file"
trap - EXIT
echo "created PostgreSQL-backed dev runtime environment"
