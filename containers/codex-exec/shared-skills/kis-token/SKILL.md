---
name: kis-token
description: "Shared runtime helper for KIS Open API OAuth token caching. Use as a library from direct KIS helpers; do not expose access tokens to prompts, reports, artifacts, or user-visible output."
---

# KIS Token Helper

This shared skill owns direct KIS Open API OAuth token caching for codex-exec runtime helpers.

Runtime helpers that need KIS REST access must import `scripts/kis_token.py` instead of creating their own token cache files. Do not add new per-feature token files such as `kis-token.json`, `kis-token-real.json`, or feature-specific token caches.

Default shared cache paths when `CODEX_HOME` is set:

- `$CODEX_HOME/.cache/kis-token/kis-token-real.json`
- `$CODEX_HOME/.cache/kis-token/kis-token-demo.json`

Fallback shared cache paths when `CODEX_HOME` is not set:

- `~/.cache/codex/kis-token/kis-token-real.json`
- `~/.cache/codex/kis-token/kis-token-demo.json`

`KIS_TOKEN_CACHE_DIR` may override the cache directory when deployment needs a different shared writable location. Avoid feature-specific token cache environment variables for new code.

Tokens are sensitive. The helper may return the token to the local Python caller in process memory, but token strings must not be written to prompts, reports, logs, canonical artifacts, Telegram messages, or user-visible stdout.
