---
name: collect-news-information
description: "Collect or retrieve Korean stock news YAML caches using direct KIS Open API calls. Use for daily-trading news collection, date-based news cache lookup, or when another agent needs the path to a cached news file."
---

# Collect News Information

## Scope

Use this skill to collect Korean stock news into a date-based YAML cache or to return the path to an existing cache.

Default cache path:

```text
memory/collect-news-information/news-YYYY-MM-DD.yaml
```

## Modes

- `get`: Return the cache file path for the requested date. If no date is supplied, use today's Asia/Seoul date. If the file does not exist, return exactly:

  ```text
  해당 날짜 뉴스 캐시가 아직 생성되지 않았습니다.
  ```

- `collect`: Use direct KIS Open API REST calls to collect news for the supplied symbol list, write the date cache, and return the cache file path.

## Direct KIS API

Use the bundled helper:

```bash
python3 containers/codex-exec/shared-skills/collect-news-information/scripts/news_cache.py get --date YYYY-MM-DD
python3 containers/codex-exec/shared-skills/collect-news-information/scripts/news_cache.py collect --date YYYY-MM-DD --symbols 005930,000660
```

Required environment variables inside `codex-exec`:

```text
KIS_APP_KEY
KIS_APP_SECRET
```

The helper uses the fixed real KIS Open API base URL `https://openapi.koreainvestment.com:9443`. It does not use paper keys.

News API details:

- OAuth token endpoint: `/oauth2/tokenP`
- News endpoint: `/uapi/domestic-stock/v1/quotations/news-title`
- News `tr_id`: `FHKST01011800`

## Output Format

The YAML file is keyed by quoted symbol strings. Repeated `collect` calls update only the requested symbols and preserve other existing symbols in the same date file.

```yaml
date: "2026-06-10"
source: kis_open_api
symbols:
  "000000":
    symbol_name: 종목이름
    articles:
      - article_date: "2026-06-10T09:30:00+09:00"
        sentiment: positive
        content: KIS API 반환 제목 또는 문구
```

Allowed sentiment values are `positive`, `neutral`, `negative`, and `mixed`.

Top-level fields are always ordered as `date`, `source`, and `symbols`. Symbol fields are ordered as `symbol_name` then `articles` when `symbol_name` is present; otherwise only `articles` is written. Article fields are always strings: `article_date`, `sentiment`, and `content`. Use an empty string for `article_date` when the API does not provide a date. Do not write `title`, `symbol_id`, `updated_at`, or `errors` fields. Write `symbol_name` only when the KIS API response includes a matching Korean symbol name for the requested symbol code.

## Boundaries

- Account, balance, order-available, fill-history, pending-order, reservation-order, correction, cancellation, and order APIs are forbidden.
- Do not return app keys, app secrets, access tokens, authorization headers, or token cache contents.
- Store OAuth token cache outside the repo memory tree.
- Prefer returning the cache path to other agents. Return full file contents only when explicitly requested.
- Store the KIS API returned news title or short text in `content` without a separate summarization step.
