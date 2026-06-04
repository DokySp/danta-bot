---
name: check-portfolio
description: "Read the codex-exec base portfolio configuration file verbatim. Use when the user asks to check, show, load, or reference the configured portfolio, portfolio tickers, holding list, or portfolio file."
---

# Check Portfolio

## Purpose

Read the portfolio configuration file exactly as stored. Do not parse, rank, expand, normalize, or edit the portfolio unless the user explicitly asks for a separate follow-up action.

## File Location

Use the first existing path in this order:

1. `PORTFOLIO_FILE` environment variable when set.
2. `/app/config/portfolio.txt`.
3. `/workspace/containers/codex-exec/profiles/base/config/portfolio.txt`.
4. `containers/codex-exec/profiles/base/config/portfolio.txt` when running from a local repository checkout.

## Workflow

Run the bundled reader:

```bash
sh scripts/read_portfolio.sh
```

Report the file path and the raw file contents. If the file does not exist or cannot be read, say that directly and do not guess the portfolio.
