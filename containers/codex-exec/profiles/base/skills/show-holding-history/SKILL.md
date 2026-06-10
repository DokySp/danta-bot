---
name: show-holding-history
description: "Render recent holding quantity changes from memory/holding-history/holding-changes.csv as Telegram-ready time/quantity chart images. Use when the user asks for $show-holding-history or $show-holding-history N."
---

# Show Holding History

## Purpose

Render submitted cash-order holding quantity changes recorded by `codex-exec` after daily-trading runs. The output is one or more step chart images with time on the x-axis, held share count on the y-axis, one color per symbol, offset `symbol name(symbol id)` labels connected to the matching line by thin leader lines, and a bottom legend. Draw holdings as horizontal and vertical segments only, without diagonal interpolation. Add light gray vertical grid lines at each midnight and light gray horizontal grid lines at every 1-share unit; y-axis labels must be integers. Sort symbols by each symbol's highest held quantity in the selected period, descending, then split them into groups of 10 per image. Each image uses its own y-axis maximum based on the symbols shown in that image.

## Data Source

Use the first configured CSV path in this order:

1. `HOLDING_HISTORY_CSV` when set.
2. `DAILY_TRADING_MEMORY_DIR/holding-history/holding-changes.csv` when `DAILY_TRADING_MEMORY_DIR` is set.
3. `<repo>/memory/holding-history/holding-changes.csv` from a local repository checkout.
4. `./memory/holding-history/holding-changes.csv` from the current working directory.

The CSV is append-only and contains submitted non-reservation order quantity changes. Reservation orders are ignored.

## Workflow

Run the bundled renderer with the requested calendar-day count. If no count is provided, use 7 calendar days.

```bash
python3 scripts/render_holding_history.py --days 7
```

Return the resulting JSON summary. The summary includes `image_paths` for all generated images and `image_path` for the first image for compatibility. If the request came through Telegram, `codex-exec` may handle `$show-holding-history` or `$show-holding-history N` directly and send the rendered image(s) and CSV document without invoking Codex.

Do not call KIS, account, order, web, or MCP APIs from this skill. This skill only reads the local CSV and renders a local image.
