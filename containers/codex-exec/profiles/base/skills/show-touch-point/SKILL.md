---
name: show-touch-point
description: "Render price-trigger alert touch points on the configured indicator candlestick chart as Telegram-ready PNG images. Use when the user asks for /show_touch_point {trigger_id}, /show-touch-point {trigger_id}, or $show-touch-point with a price trigger id such as kospi-case-1."
---

# Show Touch Point

## Purpose

Render the alert points produced by `touch-points.yaml` on top of the matching indicator candlestick chart. Resolve the requested id through the price-trigger config first; use that id's `case_title`, `name`, `symbol`, and `source` to select the touch log rows and the indicator candles.

## Data Sources

Use the first available price-trigger config path:

1. `--config` when passed to `scripts/render_touch_point.py`.
2. `PRICE_TRIGGER_FILE` when set.
3. `/app/config/touch-points.yaml`.

Use the first available codex-exec price-trigger touch log:

1. `--touch-log` when passed.
2. `SHOW_TOUCH_POINT_TOUCH_LOG` when set.
3. `touch_log_file` from `touch-points.yaml`.
4. `<cache_file stem>-touch-events.jsonl` under the configured price-trigger state directory.

For the indicator chart, fetch the configured index from KIS Open API first using domestic index intraday chart data. `/show_touch_point {id}` uses fixed 30-minute candles (`1800` seconds). Query KIS once with past data included, then treat the returned candle range, up to 99 candles, as the complete chart range. Use `quote_history_file` from `touch-points.yaml` only as a fallback when KIS cannot provide any chart or for non-KIS sources. If fallback quote history rows contain OHLC fields, render those candles directly; otherwise render the stored sampled indicator values as degenerate candles so the chart still represents the configured indicator series, not the touch events. Never render a touch-only chart as a successful result. The renderer only needs read access to the codex-exec touch log and KIS quotation APIs; it does not read account, balance, order, Telegram conversation, or trading APIs.

## Workflow

Run the bundled renderer with a trigger id. Do not pass date or day-count options.

```bash
python3 scripts/render_touch_point.py kospi-case-1
```

Return the JSON summary printed by the script. The summary includes `image_path`, `touch_log_paths`, `trigger_id`, `data_start`, `data_end`, `interval_seconds`, `touch_count`, `series_count`, and any `warnings`. If the request came through Telegram, `codex-exec` may handle `/show_touch_point {id}` directly and send the rendered image without invoking Codex.

Do not infer the indicator from the touch log alone. Always resolve the requested id through `touch-points.yaml`.
