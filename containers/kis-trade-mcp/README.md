# kis-trade-mcp

공용 KIS MCP 서버 compose입니다. v1_1과 v2의 codex-exec는 같은 Docker 네트워크에서
`http://kis-trade-mcp:3000/sse`로 이 서버에 접속합니다.

## Runtime Env

실제 값은 `config/kis-trade-mcp.env`에 둡니다. 이 파일은 git에서 무시합니다.

```bash
cp config/kis-trade-mcp.env.example config/kis-trade-mcp.env
```

필요한 값:

- `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCT_STOCK`: 실전 계좌용
- `KIS_PAPER_APP_KEY`, `KIS_PAPER_APP_SECRET`, `KIS_PAPER_STOCK`: 모의 계좌용
- `KIS_HTS_ID`, `KIS_PROD_TYPE`: 공통 계좌 설정

## Trading Env

codex-exec의 `CODEX_MCP_TRADING_ENV`와 이 파일의 값은 직접 연결되지는 않습니다.
다만 `CODEX_MCP_TRADING_ENV=paper`는 MCP 호출에 `env_dv="demo"`를 강제하므로
`KIS_PAPER_*` 값이 필요하고, `CODEX_MCP_TRADING_ENV=acct`는 `env_dv="real"`을
강제하므로 `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCT_STOCK` 값이 필요합니다.
