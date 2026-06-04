#!/usr/bin/env sh
set -eu

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"

candidates=""
if [ -n "${DEFAULT_TRADE_PROMPT_FILE:-}" ]; then
  candidates="${candidates}${DEFAULT_TRADE_PROMPT_FILE}
"
fi
candidates="${candidates}/app/config/default-trade-prompt
/workspace/containers/codex-exec/profiles/base/config/default-trade-prompt
containers/codex-exec/profiles/base/config/default-trade-prompt"
if [ -n "$repo_root" ]; then
  candidates="${candidates}
${repo_root}/containers/codex-exec/profiles/base/config/default-trade-prompt"
fi

while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  if [ -f "$candidate" ]; then
    if [ ! -s "$candidate" ]; then
      printf 'default trade prompt is empty: %s\n' "$candidate" >&2
      exit 1
    fi
    cat "$candidate"
    printf '\n'
    exit 0
  fi
done <<EOF
$candidates
EOF

printf 'default trade prompt file not found\n' >&2
exit 1
