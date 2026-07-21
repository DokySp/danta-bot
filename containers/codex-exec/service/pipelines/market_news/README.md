# market_news

`market_news` is a deterministic service pipeline. It polls configured GDELT
DOC 2.0 ArticleList queries, stores normalized headlines in SQLite, and records
source-level collection status. It never invokes Codex or another LLM and never
submits orders.

The default database is `memory/market-news/market-news.sqlite3`. Routine
scheduled success is log-only; source failures are surfaced by the scheduler.
The base schedule runs every 15 minutes. Each source keeps an independent
cursor with a 30-minute overlap, so a failed source does not advance its cursor
or discard a successful source's articles. URL/title deduplication retains all
domestic/global provenance. Provider-capped responses are split into smaller
time windows; if the configured request budget or 15-minute minimum window is
still saturated, that source is recorded as partial and its cursor is not
advanced.

The base configuration has separate `domestic` and `global` queries. The
global query excludes South Korean outlets and explicitly includes
macroeconomic, central-bank, currency,
tariff, election, diplomacy, sanctions, conflict, energy, oil, and shipping
terms. `news_context` selects a balanced recent set from both source classes.

```text
python3 containers/codex-exec/service/pipelines/market_news/cli.py collect \
  --workspace-dir . \
  --config containers/codex-exec/profiles/base/config/market-news.yaml
python3 containers/codex-exec/service/pipelines/market_news/cli.py status --workspace-dir .
python3 containers/codex-exec/service/pipelines/market_news/cli.py self-test
```
