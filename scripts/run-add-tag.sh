#!/usr/bin/env bash
set -euo pipefail

prompt="$(cat <<'EOF'
태그 부착 작업
이전 버전태그부터 오늘 버전태그까지 수정내역을 확인 후, 아래와 같이 태그르 생성하세요.
###에는 001부터 시작하는 세 자리 숫자가 들어가며, 동일 날짜에 태그가 있는 경우, 순차적으로 메긴다.

v{yyyyMMdd-###}

- 수정사항 1
- 수정사항 2
- ...
EOF
)"

codex exec --yolo \
  -m gpt-5.5 \
  -c 'model_reasoning_effort="low"' \
  "${prompt}"
