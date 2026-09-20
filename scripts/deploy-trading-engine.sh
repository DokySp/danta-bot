#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/deploy-trading-engine.sh <dockerhub-namespace> [version]

Builds trading-engine and pushes it to the given Docker Hub namespace.
An explicit version is pushed first, then the same image is published as latest.
If version is omitted, the Docker image tag is latest and APP_VERSION is resolved from git.
Regression tests run in Docker with Python 3.12 and pinned dependencies.
EOF
}

if [ "$#" -gt 2 ] || [ -z "${1:-}" ]; then
  usage
  exit 64
fi

dockerhub_namespace="$1"
image_name="trading-engine"
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

echo "Running repo-wide regression suite in Docker before build..." >&2
if ! "${PYTHON_BIN:-python3}" "${repo_root}/scripts/run_tests.py" --docker; then
  echo "Regression suite failed; aborting deploy." >&2
  exit 1
fi

docker build \
  -f "${repo_root}/containers/trading-engine/Dockerfile" \
  --build-arg "APP_VERSION=${app_version}" \
  -t "${local_image}" \
  -t "${remote_image}" \
  "${repo_root}/containers/trading-engine"

echo "Checking built images over the deployment network before push..." >&2
"${PYTHON_BIN:-python3}" "${repo_root}/scripts/verify-deployment.py" \
  --engine-image "${local_image}" --version "${app_version}"

docker push "${remote_image}"
if [ "${image_tag}" != "latest" ]; then
  latest_image="${dockerhub_namespace}/${image_name}:latest"
  docker tag "${remote_image}" "${latest_image}"
  docker push "${latest_image}"
fi
