#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/deploy-codex-exec-experimental.sh <dockerhub-namespace> [version]

Builds codex-exec-experimental with the experimental profile and pushes it to the given Docker Hub namespace.
If version is omitted, the Docker image tag is latest and APP_VERSION is resolved from git.
EOF
}

if [ "$#" -gt 2 ] || [ -z "${1:-}" ]; then
  usage
  exit 64
fi

resolve_latest_codex_version() {
  local release_url release_tag

  release_url="$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/openai/codex/releases/latest)"
  release_tag="${release_url##*/}"
  case "${release_tag}" in
    rust-v?*) printf '%s\n' "${release_tag#rust-v}" ;;
    *)
      echo "Unexpected latest Codex release URL: ${release_url}" >&2
      return 1
      ;;
  esac
}

dockerhub_namespace="$1"
image_name="codex-exec-experimental"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
codex_version="$(resolve_latest_codex_version)"
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
  --build-arg "CODEX_VERSION=${codex_version}" \
  --build-arg "CODEX_EXEC_PROFILE=experimental" \
  --build-arg "IMAGE_TITLE=${image_name}" \
  -t "${local_image}" \
  -t "${remote_image}" \
  "${repo_root}/containers"

docker push "${remote_image}"
