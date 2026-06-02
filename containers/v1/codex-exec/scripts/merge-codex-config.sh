#!/usr/bin/env sh
set -eu

CODEX_HOME="${CODEX_HOME:-/codex-home}"
SNIPPET_DIR="${CODEX_CONFIG_SNIPPET_DIR:-/app/codex-config.d}"
TARGET="${CODEX_CONFIG_FILE:-$CODEX_HOME/config.toml}"
TMP_DIR="${CODEX_CONFIG_TMP_DIR:-/tmp/codex-config.d}"

merge_snippet() {
  snippet="$1"
  name="$(basename "$snippet")"
  start="# BEGIN codex-exec managed: $name"
  end="# END codex-exec managed: $name"
  tmp="$(mktemp)"

  if [ -f "$TARGET" ]; then
    awk -v start="$start" -v end="$end" '
      $0 == start { skip = 1; next }
      $0 == end { skip = 0; next }
      !skip { print }
    ' "$TARGET" > "$tmp"
  else
    : > "$tmp"
  fi

  {
    cat "$tmp"
    printf '\n%s\n' "$start"
    cat "$snippet"
    printf '\n%s\n' "$end"
  } > "$TARGET"

  rm -f "$tmp"
}

mkdir -p "$(dirname "$TARGET")"

if [ -n "${CODEX_MCP_SERVER_NAME:-}" ]; then
  mkdir -p "$TMP_DIR"
  env_snippet="$TMP_DIR/env-mcp.toml"
  {
    printf '[mcp_servers.%s]\n' "$CODEX_MCP_SERVER_NAME"
    printf 'command = "%s"\n' "${CODEX_MCP_COMMAND:-npx}"
    printf 'args = %s\n' "${CODEX_MCP_ARGS_TOML:-[]}"
    printf 'enabled = %s\n' "${CODEX_MCP_ENABLED:-true}"
  } > "$env_snippet"
  merge_snippet "$env_snippet"
fi

if [ -d "$SNIPPET_DIR" ]; then
  for snippet in "$SNIPPET_DIR"/*.toml; do
    [ -e "$snippet" ] || continue
    merge_snippet "$snippet"
  done
fi

exec "$@"
