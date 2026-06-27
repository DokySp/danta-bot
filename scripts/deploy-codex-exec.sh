#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/deploy-codex-exec.sh <dockerhub-namespace> [version]

Builds codex-exec with the base profile and pushes it to the given Docker Hub namespace.
If version is omitted, the Docker image tag is latest and APP_VERSION is resolved from git.
EOF
}

if [ "$#" -gt 2 ] || [ -z "${1:-}" ]; then
  usage
  exit 64
fi

dockerhub_namespace="$1"
image_name="codex-exec"
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
  -f "${repo_root}/containers/codex-exec/Dockerfile" \
  --build-arg "APP_VERSION=${app_version}" \
  --build-arg "CODEX_EXEC_PROFILE=base" \
  --build-arg "IMAGE_TITLE=${image_name}" \
  -t "${local_image}" \
  -t "${remote_image}" \
  "${repo_root}/containers"

docker push "${remote_image}"
