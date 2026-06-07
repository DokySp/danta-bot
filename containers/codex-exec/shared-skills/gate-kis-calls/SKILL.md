---
name: gate-kis-calls
description: "Serialize KIS MCP calls made by concurrent collection agents and enforce a minimum interval between calls. Use whenever daily-trading market, financial, or news collection agents call any KIS MCP tool."
---

# Gate KIS Calls

Use this gate before every KIS MCP call made by a collection sub-agent. The gate serializes KIS calls across the three parallel collectors; it does not grant permission to call account or order APIs.

## Workflow

1. Acquire a lease immediately before one KIS MCP call:

   ```bash
   python3 scripts/kis_call_gate.py acquire \
     --run-id "$RUN_ID" \
     --agent "$AGENT_NAME"
   ```

2. Parse the returned JSON and retain `token`.
3. Make exactly one KIS MCP call.
4. Release the lease in a `finally` path, including when the KIS call fails:

   ```bash
   python3 scripts/kis_call_gate.py release --token "$TOKEN"
   ```

5. Acquire a new lease for the next KIS call.

## Rules

- One lease permits exactly one KIS MCP call.
- Never hold a lease while making web calls or doing local analysis.
- Never share a token with another agent.
- If acquisition times out, return a collection error; do not bypass the gate.
- The default minimum interval is 1.2 seconds. `KIS_CALL_GATE_MIN_INTERVAL` may increase it, but must not reduce it below 1 second.
- The default gate directory is `~/.cache/codex/kis-call-gate`; override it with `KIS_CALL_GATE_DIR` only when all collectors use the same directory.
