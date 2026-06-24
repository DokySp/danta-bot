# Authentication Token Management Rules

## Purpose

KIS Open API uses an OAuth access token in addition to the app key and app secret. Access tokens are not long-lived, so check expiry before analysis runs and before account/order API calls, then renew only when needed.

## Basic Principles

- Never write app keys or app secrets into skill files, reports, logs, or sub-agent prompts.
- Never print raw access tokens in reports or user responses.
- Keep tokens for the `real` and `demo` environments separate.
- If the prompt contains `CODEX_MCP_TRADING_ENV`, it overrides user wording. Use `env_dv="demo"` for `paper` and `env_dv="real"` for `acct`.
- Only infer the environment from user wording when `CODEX_MCP_TRADING_ENV` is absent. In that case, use `env_dv="demo"` for paper trading and `env_dv="real"` for real trading or real-account lookups.
- Do not pass tokens or raw auth responses to sub-agents. Only Main Codex performs auth preflight and token renewal.
- Collection sub-agents and Main agent may make read-only KIS account lookup calls. Handle non-auth failures such as rate limits, temporary gateway errors, and timeouts with bounded backoff at the call site. `first-verdict` and `second-verdict` sub-agents must not call KIS.

## Direct KIS Helper Authentication Boundary

`scripts/collect_main_evidence.py` calls KIS REST directly, so it does not use the MCP-internal token cache. This helper reads KIS app key, app secret, and account settings from the runtime environment, then stores environment-specific tokens in a shared cache through `shared-skills/kis-token/scripts/kis_token.py`. When `CODEX_HOME` is set, the default cache paths are `$CODEX_HOME/.cache/kis-token/kis-token-real.json` and `$CODEX_HOME/.cache/kis-token/kis-token-demo.json`; otherwise they are under `~/.cache/codex/kis-token/`.

When adding a new direct KIS feature, do not create a separate token file or feature-local token cache. Import and use the shared `kis-token` helper. If deployment needs a custom cache location, use only `KIS_TOKEN_CACHE_DIR`, not feature-specific environment variables.

The direct helper must not write raw tokens, app keys, app secrets, account numbers, account product codes, or HTS IDs into artifacts, prompts, reports, or user responses. Limit helper output to artifact paths, counts, and token-status level information.

Record authentication errors as failure evidence at the KIS call site. Do not continue order or account gates when auth evidence is failed or missing.

## MCP Authentication Boundary

General KIS account, quote, and order APIs authenticate through the MCP-internal `kis_auth.py`, which uses `ka.auth()` and a local token cache. A standalone `auth_token` result can differ from the cache that downstream APIs actually read, so `daily-trading` does not call it as a run-start preflight.

If the MCP `auth_token` implementation explicitly returns `cache_authoritative=true`, it may be used once to recover from an authentication error. Without that marker, do not treat `auth_token` success as proof that account or market APIs are authenticated.

Issue a websocket approval key separately only when using realtime APIs that require it.

```text
auth(api_type="auth_ws_token", params={"grant_type":"client_credentials", "env_dv":"real or demo"})
```

If the MCP wrapper does not inject app key/app secret automatically and returns a required-field error, tell the user to check the app key/app secret configuration in KIS MCP settings. Do not ask the user to paste app key/app secret values into the chat.

## Expiry Decisions

Treat the normal access-token lifetime as about 24 hours. If the response contains expiry information such as `expires_in`, `expires_at`, `access_token_token_expired`, or `issued_at`, prefer that value.

When expiry information is unclear, apply these conservative rules.

- If the issue time is known and at least 23 hours have passed, let the first read-only KIS call refresh MCP-internal auth.
- If the issue time is unknown or the current token state cannot be checked, verify auth state through the first read-only KIS call without a standalone `auth_token` call.
- Before account lookup or order preparation, if expiry is expected within 30 minutes, use only MCP-internal auth or an `auth_token` implementation that supports `cache_authoritative=true`.
- If an API returns an auth error, token-expiry error, or permission error, follow the retry rules based on whether `cache_authoritative=true` is supported.

## Run Preflight

At analysis start, use this sequence.

1. Resolve the requested environment.
   - If the prompt contains `CODEX_MCP_TRADING_ENV=paper`, use `demo`.
   - If the prompt contains `CODEX_MCP_TRADING_ENV=acct`, use `real`.
   - For analysis-only runs, use real quote lookup by default; use `demo` only when the user explicitly requests paper/demo mode.
   - Paper trading: `demo`
   - Real-account lookup or real order ticket: `real`
2. Do not call standalone `auth_token`.
3. Let the first read-only KIS account/market API call perform MCP-internal auth.
4. Record only whether auth succeeded and any verifiable expiry time in internal state.
5. Do not print raw tokens.

## Retry Rules

When an API call fails due to an authentication problem, use this sequence. Authentication problems are not call-site backoff targets.

1. Record the failed API name and environment (`real`/`demo`).
2. Call `auth_token` once only when the MCP `auth_token` response is known to support `cache_authoritative=true`; otherwise record a possible token-cache mismatch and block KIS-dependent work.
3. If `auth_token` was called, retry the failed API exactly once with the same parameters.
4. If the failed call belongs to a collection sub-agent, the sub-agent returns the auth failure to Main Codex and Main Codex performs steps 2-3. Main agent handles account-lookup auth failures directly.
5. If no retry was performed or the retry still fails, mark that data as `missing` and leave the auth failure in the error summary.
6. For order or account APIs, do not continue order work after retry failure.

## Authentication Status Summary

User responses and reports may include only this level of auth status, without raw tokens.

```markdown
## 인증 상태
- 환경: real / demo
- 접근토큰 상태: 신규 발급 / 기존 토큰 사용 / 재발급 후 사용 / 실패
- 만료 예정: 확인된 시각 또는 확인 불가
- 재시도 여부:
- 실패한 인증 관련 API:
```

## Forbidden

- Including app key/app secret in skill files, reports, or sub-agent inputs
- Printing raw access tokens
- Continuing account/order APIs after token renewal failure
- Infinite retries after auth errors
- Mixing `real` and `demo` tokens
- Collection sub-agents issuing their own `auth_token` after auth failure
- Auth or KIS API calls from `first-verdict` or `second-verdict` sub-agents
