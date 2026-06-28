# Market Index Snapshot Pipeline

`market_index_snapshot` collects compact run-level market index context for codex-exec
pipelines. It is a deterministic service pipeline, not a shared skill.

## Purpose

Use this pipeline when a run needs broad market index direction as context. The
daily-trading pipeline uses it as optional, non-blocking evidence and stores the
result as `market-index-snapshot.json`.

## Command

Collect JSON:

```bash
python3 containers/codex-exec/service/pipelines/market_index_snapshot/cli.py collect \
  --run-id "$RUN_ID" \
  --started-at "$STARTED_AT" \
  --output reports/runs/"$RUN_ID"/market-index-snapshot.json
```

Render an existing JSON payload as Korean Markdown:

```bash
python3 containers/codex-exec/service/pipelines/market_index_snapshot/cli.py render \
  --input reports/runs/"$RUN_ID"/market-index-snapshot.json
```

Run offline self-test:

```bash
python3 containers/codex-exec/service/pipelines/market_index_snapshot/cli.py self-test
```

## Markets

By default, collect these five markets:

- S&P 500
- Nasdaq
- Dow
- KOSPI
- KOSDAQ

## Data Rules

- Use live or latest-available market data at the time of collection.
- For US indexes, use Google Finance (`https://www.google.com/finance/`) as the
  authoritative quote source:
  - S&P 500: `https://www.google.com/finance/quote/.INX:INDEXSP`
  - Nasdaq: `https://www.google.com/finance/quote/.IXIC:INDEXNASDAQ`
  - Dow: `https://www.google.com/finance/quote/.DJI:INDEXDJX`
- Do not mix US index values or percent changes from Yahoo Finance, MarketWatch,
  Investing.com, news snippets, futures, ETFs, or another source when Google
  Finance data is available.
- If Google Finance is unavailable for a US index, mark that index as
  `unavailable` instead of falling back to another source.
- For KOSPI and KOSDAQ, use the service KIS domestic index quote path.
- Use the index's percent change versus the previous close as `change_percent`.
- Classify display status from percent change:
  - `상승`: percent change is greater than `0.1%`.
  - `하강`: percent change is less than `-0.1%`.
  - `보합`: percent change is between `-0.1%` and `0.1%`, inclusive.
  - `확인불가`: percent change is unavailable or the index collection failed.
- If a market is closed, use the latest available regular-session index value.
- If a live quote cannot be verified for a market, do not guess. Keep that
  index unavailable and include the non-sensitive error in `errors`.
- Do not use futures as a substitute for the spot index.
- Do not produce trading orders or portfolio actions from this pipeline.

## JSON Contract

The collector writes a compact JSON object with these top-level fields:

- `schema_version`
- `run_id`
- `started_at`
- `generated_at`
- `status`: `success`, `partial`, or `failed`
- `indexes`: one entry per requested index
- `warnings`
- `errors`

Each `indexes` entry includes:

- `symbol`
- `name`
- `source`
- `status`
- `value`
- `change_percent`
- `observed_at`
- `market_status`
- `error`

The daily-trading pipeline compacts this payload and stores the compact form in
`decision-brief.json` as top-level `market_index_snapshot`. It is run-level context and
is not copied into each symbol.

## Markdown Output

The `render` command emits Korean Markdown in this shape:

```markdown
S&P 500:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

Nasdaq:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

Dow:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

코스피:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...

코스닥:
- 상태: 상승/하강/보합/확인불가
- 등락률: +0.00%
- 의견: ...
```
