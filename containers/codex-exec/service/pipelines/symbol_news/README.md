# Symbol News Pipeline

`symbol_news` collects per-symbol Korean stock news into a date-based YAML
cache using direct KIS Open API calls. It is a deterministic service
pipeline, not a shared skill, and it never invokes Codex/an LLM.

## Purpose

The daily-trading pipeline calls this pipeline on demand for its universe and
stores the result as a per-symbol evidence cache. `build_run_artifacts.py`
compacts it into each symbol's `symbol_news_summary` in `decision-brief.json`.
It is unrelated to the separately scheduled `market_news` pipeline, which
covers broad domestic/global/macro/geopolitical headlines.

## Cache path

```text
memory/symbol-news-cache/symbol-news-YYYY-MM-DD.yaml
```

Override the directory with `SYMBOL_NEWS_CACHE_MEMORY_DIR`.

Historical caches written by the old `collect-news-information` shared skill
remain at `memory/collect-news-information/news-YYYY-MM-DD.yaml` and are not
migrated or rewritten.

## Commands

```bash
python3 containers/codex-exec/service/pipelines/symbol_news/cli.py get --date YYYY-MM-DD
python3 containers/codex-exec/service/pipelines/symbol_news/cli.py collect --date YYYY-MM-DD --symbols 005930,000660
python3 containers/codex-exec/service/pipelines/symbol_news/cli.py self-test
```

Required environment variables:

```text
KIS_APP_KEY
KIS_APP_SECRET
```

The helper uses the fixed real KIS Open API base URL
`https://openapi.koreainvestment.com:9443`. It does not use paper keys.

News API details:

- News endpoint: `/uapi/domestic-stock/v1/quotations/news-title`
- News `tr_id`: `FHKST01011800`

Use the shared `kis-token` helper for OAuth token caching.

## Output Format

The YAML file is keyed by quoted symbol strings. Repeated `collect` calls
update only the requested symbols and preserve other existing symbols in the
same date file.

```yaml
date: "2026-06-10"
source: kis_open_api
symbols:
  "000000":
    symbol_name: 종목이름
    articles:
      - article_date: "2026-06-10T09:30:00+09:00"
        content: KIS API 반환 제목 또는 문구
```

Top-level fields are always ordered as `date`, `source`, and `symbols`.
Symbol fields are ordered as `symbol_name` then `articles` when `symbol_name`
is present; otherwise only `articles` is written. Article fields are always
strings: `article_date` and `content`. Do not write `title`, `symbol_id`,
`updated_at`, `errors`, or `sentiment` fields.

## Boundaries

- Account, balance, order-available, fill-history, pending-order,
  reservation-order, correction, cancellation, and order APIs are forbidden.
- Do not return app keys, app secrets, access tokens, authorization headers,
  or token cache contents.
- Store the KIS API returned news title or short text in `content` without a
  separate summarization step.
- Collection is mechanical; it must never call Codex or another LLM.
