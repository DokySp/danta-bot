#!/usr/bin/env sh
set -eu

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"

candidates=""
if [ -n "${PORTFOLIO_FILE:-}" ]; then
  candidates="${candidates}${PORTFOLIO_FILE}
"
fi
candidates="${candidates}/app/config/portfolio.txt
/workspace/containers/codex-exec-v1/config/portfolio.txt
containers/codex-exec-v1/config/portfolio.txt"
if [ -n "$repo_root" ]; then
  candidates="${candidates}
${repo_root}/containers/codex-exec-v1/config/portfolio.txt"
fi

while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  if [ -f "$candidate" ]; then
    printf 'path: %s\n' "$candidate"
    printf 'contents:\n'
    cat "$candidate"
    printf '\n'
    exit 0
  fi
done <<EOF
$candidates
EOF

printf 'portfolio file not found\n' >&2
exit 1
