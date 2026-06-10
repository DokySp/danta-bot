#!/usr/bin/env sh
set -eu

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"

user_candidates=""
if [ -n "${PORTFOLIO_FILE:-}" ]; then
  user_candidates="${user_candidates}${PORTFOLIO_FILE}
"
fi
user_candidates="${user_candidates}/app/config/portfolio.txt
/workspace/containers/codex-exec/profiles/base/config/portfolio.txt
containers/codex-exec/profiles/base/config/portfolio.txt"
if [ -n "$repo_root" ]; then
  user_candidates="${user_candidates}
${repo_root}/containers/codex-exec/profiles/base/config/portfolio.txt"
fi

assistant_candidates=""
if [ -n "${ASSISTANT_PORTFOLIO_CACHE_FILE:-}" ]; then
  assistant_candidates="${assistant_candidates}${ASSISTANT_PORTFOLIO_CACHE_FILE}
"
fi
home_dir="${HOME:-}"
if [ -z "$home_dir" ]; then
  home_dir="$(cd ~ && pwd)"
fi
assistant_candidates="${assistant_candidates}${home_dir}/.cache/codex/check-portfolio/assistant-recommendations.txt"

input_files=""

while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  if [ -f "$candidate" ]; then
    input_files="${input_files}${candidate}
"
    break
  fi
done <<EOF
$user_candidates
EOF

while IFS= read -r candidate; do
  [ -n "$candidate" ] || continue
  if [ -f "$candidate" ]; then
    input_files="${input_files}${candidate}
"
    break
  fi
done <<EOF
$assistant_candidates
EOF

if [ -z "$input_files" ]; then
  printf 'portfolio files not found\n' >&2
  exit 1
fi

set --
while IFS= read -r input_file; do
  [ -n "$input_file" ] || continue
  set -- "$@" "$input_file"
done <<EOF
$input_files
EOF

awk '
{
  line = $0
  sub(/#.*/, "", line)
  gsub(/[^0-9]+/, " ", line)
  count = split(line, tokens, /[[:space:]]+/)
  for (idx = 1; idx <= count; idx++) {
    token = tokens[idx]
    if (token ~ /^[0-9][0-9][0-9][0-9][0-9][0-9]$/ && !seen[token]++) {
      if (merged != "") {
        merged = merged ", "
      }
      merged = merged token
    }
  }
}
END {
  if (merged != "") {
    print merged
  } else {
    print "portfolio symbols not found" > "/dev/stderr"
    exit 1
  }
}
' "$@"
