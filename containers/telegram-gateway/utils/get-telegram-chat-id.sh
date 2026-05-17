#!/usr/bin/env bash
set -u

TOKEN="${1:-}"
[[ -z "$TOKEN" ]] && read -r -p "Telegram bot token: " TOKEN
[[ -z "$TOKEN" ]] && { echo "token is empty" >&2; exit 1; }

echo "Send any message to your bot in Telegram."

for _ in {1..60}; do
  json="$(curl -sS "https://api.telegram.org/bot${TOKEN}/getUpdates")" || exit 1
  chat_id="$(JSON="$json" python3 - <<'PY'
import json, os
for u in json.loads(os.environ["JSON"]).get("result", []):
    m = u.get("message") or u.get("edited_message") or {}
    c = m.get("chat") or {}
    if c.get("id") is not None:
        print(c["id"])
        raise SystemExit
PY
)"

  if [[ -n "$chat_id" ]]; then
    echo "chat_id=${chat_id}"
    exit 0
  fi

  sleep 1
done

echo "chat_id not found" >&2
exit 1
