#!/usr/bin/env bash
set -Eeuo pipefail

target_user=${1:-exi}

if (( EUID != 0 )); then
  echo "run this script with sudo or as root" >&2
  exit 77
fi

source /etc/os-release
if [[ ${ID:-} != rocky || ${VERSION_ID:-} != 8.* ]]; then
  echo "expected Rocky Linux 8.x, found ${PRETTY_NAME:-unknown}" >&2
  exit 65
fi
if [[ $(uname -m) != x86_64 ]]; then
  echo "expected x86_64" >&2
  exit 65
fi
if ! getent passwd "$target_user" >/dev/null; then
  echo "target user does not exist: $target_user" >&2
  exit 67
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "Docker Engine and Compose are already installed"
else
  rpm -q dnf-plugins-core >/dev/null || dnf install dnf-plugins-core

  if ! dnf repolist --all | grep -q '^docker-ce-stable'; then
    dnf config-manager \
      --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
  fi

  cat <<'EOF'
Before accepting a new Docker repository key, verify this official fingerprint:
060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35

The following install intentionally does not use --allowerasing.
If dnf reports a Podman/runc conflict or proposes removing packages, stop and review it.
EOF

  dnf install \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin
fi

systemctl enable --now docker
usermod -aG docker "$target_user"

docker version --format 'server={{.Server.Version}}'
docker compose version
systemctl is-active docker

echo "Docker is installed. Log out and back in so $target_user receives docker-group membership."
