#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/deploy-telegram-gateway.sh <dockerhub-namespace> [version]

Builds telegram-gateway and pushes it to the given Docker Hub namespace.
If version is omitted, the Docker image tag is latest and APP_VERSION is resolved from git.
EOF
}

if [ "$#" -gt 2 ] || [ -z "${1:-}" ]; then
  usage
  exit 64
fi

dockerhub_namespace="$1"
image_name="telegram-gateway"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
if [ -n "${2:-}" ]; then
  app_version="$2"
  image_tag="$2"
else
  app_version="$(git -C "${repo_root}" describe --tags --always --dirty)"
  image_tag="latest"
fi
local_image="${image_name}:${image_tag}"
remote_image="${dockerhub_namespace}/${image_name}:${image_tag}"

docker build \
  --build-arg "APP_VERSION=${app_version}" \
  -t "${local_image}" \
  -t "${remote_image}" \
  "${repo_root}/containers/telegram-gateway"

docker push "${remote_image}"
