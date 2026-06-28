---
name: execute-trade
description: "Run the configured default daily-trading execution. In codex-exec Telegram service, an exact `$execute-trade` message is handled directly by the Python daily-trading runner without starting Main Codex."
---

# Execute Trade

## Service Path

In codex-exec Telegram service, an exact message of:

```text
$execute-trade
```

is handled before session resume. The service reads `execute-trade.yaml`, validates its `daily_trading` block, runs `scripts/run_daily_trading_pipeline.py run`, and sends `telegram-summary.txt`.

## Config Location

The direct service path uses the first existing path in this order:

1. `EXECUTE_TRADE_CONFIG_FILE` environment variable when set.
2. `/app/config/execute-trade.yaml`.
3. `/workspace/containers/codex-exec/profiles/base/config/execute-trade.yaml`.
4. `containers/codex-exec/profiles/base/config/execute-trade.yaml` when running from a local repository checkout.

Expected shape:

```yaml
daily_trading:
  env: acct
  request_type: real-submit
  submit_orders: true
  order_path: auto
```

## Main Fallback

If this skill is invoked inside a Main Codex session instead of the codex-exec direct service path, resolve the configured `execute-trade.yaml` and run the `daily-trading` pipeline with the same structured fields. Use `telegram-summary.txt` as the user-facing response.
