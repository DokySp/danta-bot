#!/usr/bin/env bash
set -euo pipefail

# Compatibility image name only; both names contain the same engine.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export TRADING_ENGINE_IMAGE_NAME=trading-engine-experimental
exec "${script_dir}/deploy-trading-engine.sh" "$@"
