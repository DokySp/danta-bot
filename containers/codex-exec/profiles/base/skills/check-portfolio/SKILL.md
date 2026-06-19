---
name: check-portfolio
description: "Read codex-exec portfolio symbols as JSON from assistant recommendations, user-specified portfolio, and direct KIS account holdings. Use when the user asks to check, show, load, or reference the portfolio universe."
---

# Check Portfolio

## Purpose

Read portfolio symbols from three sources and output a JSON object with separate source lists plus a deduplicated universe.

- The user-managed portfolio file is for symbols the user adds directly.
- The assistant-managed recommendation cache is for symbols added by Codex recommendations.
- Direct KIS Open API account balance lookup is for currently held domestic stock symbols.
- `universe` is the union of the three source lists as supplied KIS-recognized symbols.
- Remove duplicates inside each list and in `universe`. Ordering has no meaning.
- Keep `holding` as the deduplicated actual holdings list even when a holding symbol is also present in `specified`.
- Do not rank, expand, or add symbols unless the user explicitly asks for a separate follow-up action.
- `$daily-trading` must use this skill's `universe` as the portfolio universe and must not separately re-read live holdings only to expand that universe.

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

## Direct KIS API

The bundled reader calls KIS Open API directly, not through MCP, to load current holdings.

Minimum environment variables for real account holdings:

```text
KIS_APP_KEY
KIS_APP_SECRET
KIS_ACCT_STOCK
```

Additional environment variables for paper/demo holdings:

```text
KIS_PAPER_APP_KEY
KIS_PAPER_APP_SECRET
KIS_PAPER_STOCK
```

`CODEX_MCP_TRADING_ENV=paper` maps to KIS demo credentials and `CODEX_MCP_TRADING_ENV=acct` maps to real credentials. `CHECK_PORTFOLIO_TRADING_ENV` may override that only for this helper. `KIS_PROD_TYPE` is optional and defaults to `01` when the account value does not include a product code.

Allowed access path:

- OAuth token endpoint: `/oauth2/tokenP`
- Domestic stock `inquire_balance`: `/uapi/domestic-stock/v1/trading/inquire-balance`
- Real `tr_id`: `TTTC8434R`
- Demo `tr_id`: `VTTC8434R`

Do not use MCP for this skill's holdings lookup.

## Workflow

Run the bundled reader:

```bash
sh scripts/read_portfolio.sh
```

Return stdout exactly as JSON. Do not add path labels, commentary, code fences, or raw API payloads. If KIS credentials or the holdings API fail, report the failure directly and do not guess holdings.

Required stdout shape:

```json
{
  "recommanded": ["123456"],
  "specified": ["005930"],
  "holding": ["000660"],
  "universe": ["123456", "005930", "000660"]
}
```

The field name is intentionally `recommanded` to match the current downstream contract.
`holding` preserves actual held symbols. `universe` is the only field where cross-source duplicates are removed.

## Assistant Recommendation Cache

When the user asks Codex to add recommended symbols to the assistant cache, run:

```bash
sh scripts/update_assistant_portfolio_cache.sh 123456 234567
```

This creates or updates `ASSISTANT_PORTFOLIO_CACHE_FILE` when set, otherwise `memory/check-portfolio/assistant-recommendations.txt`, and stores one deduplicated symbol per line.

## Boundaries

- Account access is limited to read-only current domestic stock holdings through direct KIS `inquire_balance`.
- Do not call order, order-available, fill-history, pending-order, reservation-order, correction, or cancellation APIs.
- Do not return account numbers, account product codes, app keys, app secrets, access tokens, authorization headers, or token cache contents.
- Store OAuth token cache outside the repo memory tree.
