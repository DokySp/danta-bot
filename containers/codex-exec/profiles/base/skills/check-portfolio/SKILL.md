---
name: check-portfolio
description: "Read the merged codex-exec configured portfolio symbols from the user-managed portfolio file and the assistant-managed recommendation cache. Use when the user asks to check, show, load, or reference the configured portfolio, configured tickers, or portfolio file."
---

# Check Portfolio

## Purpose

Read the configured portfolio symbols from two sources and output the merged symbol list only.

- The user-managed portfolio file is for symbols the user adds directly.
- The assistant-managed recommendation cache is for symbols added by Codex recommendations.
- Merge both sources as a union of six-digit Korean stock codes.
- Remove duplicates. Ordering has no meaning.
- Do not rank, expand, or add symbols unless the user explicitly asks for a separate follow-up action.
- This skill does not read live account holdings. `$daily-trading` adds current holdings separately from its initial read-only account snapshot.

## File Locations

Use the first existing user-managed portfolio path in this order:

1. `PORTFOLIO_FILE` environment variable when set.
2. `/app/config/portfolio.txt`.
3. `/workspace/containers/codex-exec/profiles/base/config/portfolio.txt`.
4. `containers/codex-exec/profiles/base/config/portfolio.txt` when running from a local repository checkout.

Use the first existing assistant-managed recommendation cache path in this order:

1. `ASSISTANT_PORTFOLIO_CACHE_FILE` environment variable when set.
2. `DAILY_TRADING_MEMORY_DIR/check-portfolio/assistant-recommendations.txt` when `DAILY_TRADING_MEMORY_DIR` is set.
3. `<repo>/memory/check-portfolio/assistant-recommendations.txt` from a local repository checkout.
4. `./memory/check-portfolio/assistant-recommendations.txt` from the current working directory.

The assistant cache is stored under the repository-level `memory/` directory by default. It remains separate from the user-managed `/app/config/portfolio.txt` so Codex recommendations do not rewrite the user's portfolio file.

## Workflow

Run the bundled reader:

```bash
sh scripts/read_portfolio.sh
```

Return the stdout exactly as the merged symbol list. Do not add path labels or source sections. If neither file exists or no readable symbols are present, say that directly and do not guess the portfolio.

## Assistant Recommendation Cache

When the user asks Codex to add recommended symbols to the assistant cache, run:

```bash
sh scripts/update_assistant_portfolio_cache.sh 123456 234567
```

This creates or updates `ASSISTANT_PORTFOLIO_CACHE_FILE` when set, otherwise `memory/check-portfolio/assistant-recommendations.txt`, and stores one deduplicated six-digit symbol per line.
