#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/deploy-codex-exec-experimental.sh <dockerhub-namespace> [version]

Builds codex-exec-experimental with the experimental profile and pushes it to the given Docker Hub namespace.
If version is omitted, latest is used.
EOF
}

if [ "$#" -gt 2 ] || [ -z "${1:-}" ]; then
  usage
  exit 64
fi

dockerhub_namespace="$1"
version="${2:-latest}"
image_name="codex-exec-experimental"
image_tag="${version}"
local_image="${image_name}:${image_tag}"
remote_image="${dockerhub_namespace}/${image_name}:${image_tag}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

docker build \
  -f "${repo_root}/containers/codex-exec/Dockerfile" \
  --build-arg "APP_VERSION=${version}" \
  --build-arg "CODEX_EXEC_PROFILE=experimental" \
  --build-arg "IMAGE_TITLE=${image_name}" \
  -t "${local_image}" \
  -t "${remote_image}" \
  "${repo_root}/containers"

docker push "${remote_image}"
