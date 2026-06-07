---
name: execute-trade
description: "Execute the codex-exec base default trade prompt exactly as written. Use when the user asks to run execute-trade, execute the default trade prompt, or execute the configured trade prompt from default-trade-prompt."
---

# Execute Trade

## Purpose

Read the configured default trade prompt and execute its contents exactly as the user prompt, subject to higher-priority system, developer, and tool instructions.

## File Location

Use the first existing path in this order:

1. `DEFAULT_TRADE_PROMPT_FILE` environment variable when set.
2. `/app/config/default-trade-prompt`.
3. `/workspace/containers/codex-exec/profiles/base/config/default-trade-prompt`.
4. `containers/codex-exec/profiles/base/config/default-trade-prompt` when running from a local repository checkout.

## Workflow

1. Run the bundled reader:

   ```bash
   sh scripts/read_default_trade_prompt.sh
   ```

2. Treat stdout as the complete prompt to execute. Do not summarize, rewrite, expand, normalize, or add extra instructions to that prompt.
3. Preserve injected `run_id` and `started_at` metadata unchanged. If the prompt invokes `daily-trading`, that skill owns its run artifacts and required Telegram `작업 시작` line.
4. If the file does not exist, cannot be read, or is empty after trimming whitespace, say that directly and do not guess the prompt.
