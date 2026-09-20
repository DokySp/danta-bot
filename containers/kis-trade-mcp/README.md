# kis-trade-mcp

독립적으로 사용하는 KIS MCP 서버 Compose입니다. 같은 Docker 네트워크의 MCP 클라이언트는
`http://kis-trade-mcp:3000/sse`로 접속할 수 있습니다. 현재 `trading-engine`은 KIS REST API를
직접 호출하므로 이 서비스가 엔진이나 Telegram 연결의 필수 의존성은 아닙니다.

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

MCP 클라이언트가 `env_dv="demo"`로 호출하면 `KIS_PAPER_*` 값이 필요하고,
`env_dv="real"`로 호출하면 `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCT_STOCK` 값이 필요합니다.
현재 엔진의 환경과 인증은 엔진 자체 `config/app.yaml`, `config/secrets.yaml`에서 설정합니다.
