---
name: check-holiday
description: "KIS Trade MCP domestic_stock.chk_holiday based Korean market open-day gate with local once-per-day caching. Use when Codex must decide whether the queried Asia/Seoul trading date is a Korean domestic stock market holiday, closed day, or open day before stock analysis, daily trading, report generation, or order preparation."
---

# Check Holiday

## Purpose

Decide whether the Korean domestic stock market is open for the queried date by using KIS Trade MCP `domestic_stock.chk_holiday`, while avoiding repeated API calls on the same `Asia/Seoul` date.

## Workflow

1. Resolve the target date in `Asia/Seoul` as `YYYYMMDD`. If the user did not provide a date, use today's Korean date.
2. Check the local cache first:

   ```bash
   python3 scripts/holiday_cache.py get --date "$TARGET_DATE"
   ```

3. If the cache returns `cache_hit: true`, use that result and do not call KIS MCP.
4. If the cache misses, inspect the live KIS API parameters before calling:

   ```json
   domestic_stock({
     "api_type": "find_api_detail",
     "params": { "api_type": "chk_holiday" }
   })
   ```

5. Call KIS MCP once for the target date:

   ```json
   domestic_stock({
     "api_type": "chk_holiday",
     "params": {
       "bass_dt": "YYYYMMDD",
       "tr_cont": "",
       "depth": 0,
       "max_depth": 10
     }
   })
   ```

6. Store and normalize the raw MCP response:

   ```bash
   python3 scripts/holiday_cache.py put --date "$TARGET_DATE" < response.json
   ```

7. Use the normalized status in the script output.

## Decision Rules

- Find the row whose `bass_dt` equals the target date.
- If that row has `opnd_yn == "Y"`, report `status: open`.
- If that row has `opnd_yn` and the value is not `"Y"`, report `status: closed`; treat it as a holiday or non-trading day.
- If the MCP call fails, the target-date row is missing, or `opnd_yn` is missing, report `status: unknown`; do not guess.

## Cache Rules

- Use `CHECK_HOLIDAY_CACHE_DIR` when set.
- Otherwise use `DAILY_TRADING_MEMORY_DIR/check-holiday` when `DAILY_TRADING_MEMORY_DIR` is set.
- Otherwise use `<repo>/memory/check-holiday` from a local repository checkout, or `./memory/check-holiday` from the current working directory.
- Cache files are keyed by date: `holiday-YYYYMMDD.json`.
- A valid same-date cache result is authoritative for this skill; do not refresh it unless the user explicitly asks to ignore or clear the cache.

## Reporting

Report only the useful fields:

- `status`
- `source` (`cache` or `kis_mcp`)
- `bass_dt`
- `opnd_yn`
- any holiday/name fields present in the matched row
- missing-field or API-failure details when `status: unknown`
