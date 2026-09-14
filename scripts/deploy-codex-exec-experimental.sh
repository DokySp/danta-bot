#!/usr/bin/env bash
set -euo pipefail

# Compatibility image name only; both names contain the same engine.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CODEX_EXEC_IMAGE_NAME=codex-exec-experimental
exec "${script_dir}/deploy-codex-exec.sh" "$@"
