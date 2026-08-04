# market_news

`market_news` is a deterministic service pipeline. It reads domestic and
overseas market-news titles from the KIS Open API, stores them in SQLite, and
records source-level collection status. It never invokes Codex or submits
orders.

The default database is `memory/market-news/market-news.sqlite3`. The base
schedule runs every 15 minutes. A failure from one KIS source does not discard
the other source's articles; only failure of both sources fails the scheduled
job. Existing URL/title deduplication preserves domestic/global provenance.

`news_context` prioritizes market, macroeconomic, financial, and geopolitical
titles while balancing domestic and global sources. Lower-priority titles stay
in SQLite and fill unused context slots instead of being discarded.

```text
python3 containers/codex-exec/service/pipelines/market_news/cli.py collect --workspace-dir .
python3 containers/codex-exec/service/pipelines/market_news/cli.py status --workspace-dir .
python3 containers/codex-exec/service/pipelines/market_news/cli.py self-test
```
