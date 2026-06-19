#!/usr/bin/env sh
set -eu

if [ "$#" -eq 0 ]; then
  printf 'usage: sh scripts/update_assistant_portfolio_cache.sh <symbol> [<symbol> ...]\n' >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -n "${DAILY_TRADING_MEMORY_DIR:-}" ]; then
  memory_root="$DAILY_TRADING_MEMORY_DIR"
elif [ -n "$repo_root" ]; then
  memory_root="$repo_root/memory"
else
  memory_root="$(pwd)/memory"
fi

cache_file="${ASSISTANT_PORTFOLIO_CACHE_FILE:-$memory_root/check-portfolio/assistant-recommendations.txt}"
cache_dir="$(dirname "$cache_file")"
mkdir -p "$cache_dir"

additions="$(mktemp)"
valid_additions="$(mktemp)"
output="$(mktemp "${cache_dir}/.portfolio-assistant.XXXXXX")"
cleanup() {
  rm -f "$additions" "$valid_additions" "$output"
}
trap cleanup EXIT HUP INT TERM

for symbol in "$@"; do
  printf '%s\n' "$symbol"
done > "$additions"

awk '
{
  line = $0
  sub(/#.*/, "", line)
  count = split(line, entries, /,/)
  for (idx = 1; idx <= count; idx++) {
    split(entries[idx], tokens, /[[:space:]]+/)
    token = tokens[1]
    if (token != "" && !seen[token]++) {
      print token
    }
  }
}
' "$additions" > "$valid_additions"

if [ ! -s "$valid_additions" ]; then
  printf 'assistant portfolio cache symbols not found\n' >&2
  exit 1
fi

if [ -f "$cache_file" ]; then
  set -- "$cache_file" "$valid_additions"
else
  set -- "$valid_additions"
fi

awk '
{
  line = $0
  sub(/#.*/, "", line)
  count = split(line, entries, /,/)
  for (idx = 1; idx <= count; idx++) {
    split(entries[idx], tokens, /[[:space:]]+/)
    token = tokens[1]
    if (token != "" && !seen[token]++) {
      print token
    }
  }
}
' "$@" > "$output"

mv "$output" "$cache_file"
trap - EXIT HUP INT TERM
rm -f "$additions" "$valid_additions"

cat "$cache_file"
