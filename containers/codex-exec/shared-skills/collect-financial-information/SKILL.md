---
name: collect-financial-information
description: "Collect or retrieve Korean stock and ETF financial YAML caches using direct KIS market, financial, and estimate API calls. Use for daily-trading financial collection, date-based financial cache lookup, or when another agent needs the path to a cached financial file."
---

# Collect Financial Information

## Scope

Use this skill to collect KIS financial data into a date-based YAML cache or to return the path to an existing cache.

Default cache paths:

```text
memory/collect-financial-information/financial-YYYY-MM-DD.yaml
memory/collect-financial-information/financial-source-fields-YYYY-MM-DD.yaml
```

## Modes

- `get`: Return the cache file path for the requested date. If no date is supplied, use today's Asia/Seoul date. If the file does not exist, return exactly:

  ```text
  해당 날짜 재무 캐시가 아직 생성되지 않았습니다.
  ```

- `collect`: Use direct KIS Open API REST calls to collect financial data for the supplied symbol list, write the date cache and source-field sidecar, and return the main cache file path. If no symbol code is supplied, do not call KIS or update files; return the main cache file path for the requested date.

Allowed access path:

- Use only the direct KIS Open API calls listed in this skill.
- Do not use any non-API collection method or any source outside those KIS API calls.

## Permissions

- External calls: allowed only through the direct KIS Open API calls listed in this skill.
- KIS calls: direct calls only, with bounded backoff for retryable KIS failures.
- Account, balance, order-available, fill-history, pending-order, reservation-order, and order APIs: forbidden.
- File writes: allowed only for the financial YAML cache and OAuth token cache managed by the bundled helper.
- Order submission, reservation submission, correction, cancellation, and order revision: forbidden.
- Secrets such as account numbers, tokens, app keys, app secrets, and HTS IDs: never request or return.

## Direct KIS API

Use the bundled helper:

```bash
python3 containers/codex-exec/shared-skills/collect-financial-information/scripts/financial_cache.py get --date YYYY-MM-DD
python3 containers/codex-exec/shared-skills/collect-financial-information/scripts/financial_cache.py collect --date YYYY-MM-DD --symbols 005930,000660
python3 containers/codex-exec/shared-skills/collect-financial-information/scripts/financial_cache.py collect --date YYYY-MM-DD --symbol 069500:KODEX200 --include-etf
```

Required environment variables inside `codex-exec`:

```text
KIS_APP_KEY
KIS_APP_SECRET
```

The helper uses the fixed real KIS Open API base URL `https://openapi.koreainvestment.com:9443`. It does not use paper keys.

KIS API details:

- OAuth token endpoint: `/oauth2/tokenP`
- `search_stock_info`: `/uapi/domestic-stock/v1/quotations/search-stock-info`, `tr_id=CTPF1002R`
- `estimate_perform`: `/uapi/domestic-stock/v1/quotations/estimate-perform`, `tr_id=HHKST668300C0`
- `invest_opinion`: `/uapi/domestic-stock/v1/quotations/invest-opinion`, `tr_id=FHKST663300C0`
- `inquire_price`: `/uapi/domestic-stock/v1/quotations/inquire-price`, `tr_id=FHKST01010100`
- `inquire_price_2`: `/uapi/domestic-stock/v1/quotations/inquire-price-2`, `tr_id=FHPST01010000`
- ETF/ETN `inquire_price`: `/uapi/etfetn/v1/quotations/inquire-price`, `tr_id=FHPST02400000`
- ETF/ETN `nav_comparison_trend`: `/uapi/etfetn/v1/quotations/nav-comparison-trend`, `tr_id=FHPST02440000`

`invest_opinion` uses `FID_INPUT_DATE_1=<three days before --date>` and `FID_INPUT_DATE_2=<--date>` by default. Override the start date only with `--start-date YYYYMMDD` when explicitly requested.

The saved `estimate_perform` data keeps only `output1` (`종목 및 최신 투자의견 요약`). `output2`, `output3`, and `output4` are intentionally excluded from both the main cache and the source-field sidecar.

## KIS Backoff

- Use existing validated parameter templates for known KIS API calls.
- Call `find_api_detail` only when no validated template exists, a new API type is introduced, or KIS rejects the template.
- For retryable KIS/MCP API error codes or messages, including rate-limit, temporary gateway/routing, transport, and timeout failures, retry the same API with the same parameters using exponential backoff up to 10 retries after the initial call.
- Recommended delay sequence is 1, 2, 4, 8, 16, then 30 seconds capped for remaining retries. Add small jitter when the runtime supports it.
- Preserve concise API-level errors in the YAML cache only when they materially change confidence. Do not include sensitive parameters.
- Authentication, token, credential, and permission errors are not local backoff targets. Return a concise note to the daily-trading Main agent; do not call `auth_token`.

## Workflow

1. Accept `run_id`, `started_at`, trading environment, and the requested symbol list.
2. Prefer `get` when the date cache should already exist.
3. Use `collect` when fresh KIS financial data is explicitly needed. If no symbol code is supplied, `collect` behaves as a path-only lookup and does not update the cache.
4. For stocks, collect KIS-returned basic product, estimate, opinion, and price data.
5. For ETFs/ETNs, pass `--include-etf` to also collect KIS ETF/ETN price and NAV comparison data.
6. Return the YAML cache path only. Do not return a JSON envelope, code fences, trailing comments, or raw payloads to the caller.

## Required Output

The YAML file is keyed by quoted symbol strings. Repeated `collect` calls overwrite a requested symbol only when fresh KIS data is returned for that symbol. If a requested symbol fetch fails, keep the existing cached symbol content unchanged. A successfully fetched new symbol is appended to the same date file.

Main result file:

```yaml
date: "2026-06-10"
source: kis_open_api
symbols:
  "005930":
    삼성전자:
      국내주식 종목추정실적:
        종목 및 최신 투자의견 요약:
          - 주식 영업일자: "20260610"
      주식현재가 시세:
        응답:
          - 현재가: "80000"
```

Source-field sidecar:

```yaml
date: "2026-06-10"
source: kis_open_api
symbols:
  "005930":
    삼성전자:
      주식현재가 시세:
        source_api: inquire_price
        응답:
          source_output: output
          rows:
            - source_fields:
                현재가: stck_prpr
```

Top-level fields are always ordered as `date`, `source`, and `symbols`. Symbol fields use human-readable Korean hierarchy in the main result: symbol code, then Korean symbol name, then Korean API name, then Korean output name, then row mappings of Korean field names to string values. Original KIS API names, output keys, and response field names are stored only in the source-field sidecar as `source_api`, `source_output`, and `source_fields`. API-level `오류` may be present only as concise non-sensitive collection failures.

## Boundaries

- Account, balance, order-available, fill-history, pending-order, reservation-order, correction, cancellation, and order APIs are forbidden.
- Do not return app keys, app secrets, access tokens, authorization headers, or token cache contents.
- Store OAuth token cache outside the repo memory tree.
- Prefer returning the cache path to other agents. Return full file contents only when explicitly requested.
- Missing financial data is not a trading blocker. Preserve successful API data and concise per-API errors in the YAML cache.
