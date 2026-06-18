# 인증 토큰 관리 규칙

## 목적

KIS Open API는 appkey/appsecret과 별도로 OAuth 접근토큰을 사용한다. 접근토큰은 장시간 유지되지 않으므로 분석 실행 전과 계좌/주문 API 호출 전 만료 여부를 확인하고 필요하면 재발급한다.

## 기본 원칙

- appkey와 appsecret은 스킬, 리포트, 로그, sub agent 프롬프트에 절대 기록하지 않는다.
- 접근토큰 원문도 리포트나 사용자 응답에 출력하지 않는다.
- `real`과 `demo` 환경은 토큰을 분리해서 취급한다.
- 프롬프트에 `CODEX_MCP_TRADING_ENV`가 있으면 사용자 표현보다 우선한다. `paper`는 `env_dv="demo"`, `acct`는 `env_dv="real"`을 사용한다.
- 프롬프트에 `CODEX_MCP_TRADING_ENV`가 없을 때만 사용자 표현으로 환경을 결정한다. 이 경우 모의거래는 `env_dv="demo"`, 실전거래 또는 실전 계좌 조회는 `env_dv="real"`을 사용한다.
- sub agent에는 토큰이나 인증 응답 원문을 전달하지 않는다. 인증 프리플라이트와 재발급은 메인 Codex만 수행한다.
- 수집 sub agent와 Main agent의 read-only 계좌 조회 KIS 호출은 허용한다. 호출량 제한, 일시 게이트웨이 오류, timeout 같은 비인증 오류는 호출 지점에서 bounded backoff로 처리한다. `first-verdict`·`second-verdict` sub agent의 KIS 호출은 금지한다.

## Direct KIS Helper 인증 경계

`scripts/collect_main_evidence.py`는 KIS REST를 직접 호출하므로 MCP 내부 토큰 캐시를 사용하지 않는다. 이 helper는 runtime environment의 KIS app key/secret/account 설정을 읽고, 환경별 토큰을 분리된 로컬 캐시에 저장한다. 캐시 경로는 기본적으로 `~/.cache/codex/daily-trading/kis-token-<env>.json`이며, 필요하면 `DAILY_TRADING_TOKEN_CACHE`로 재지정할 수 있다.

Direct helper는 토큰 원문, app key, app secret, 계좌번호, 계좌상품코드, HTS ID를 artifact, prompt, report, user response에 쓰지 않는다. helper 출력은 artifact path/count/token-status 수준으로 제한한다.

인증 오류는 KIS 호출 지점에서 실패 evidence로 기록하고, 주문 또는 계좌 gate는 실패/누락 상태에서 진행하지 않는다.

## MCP 인증 경계

일반 KIS 계좌/시세/주문 API는 MCP 내부의 `kis_auth.py`가 `ka.auth()`와 로컬 토큰 캐시를 사용해 인증한다. standalone `auth_token` 호출 결과는 downstream API가 실제로 읽는 캐시와 불일치할 수 있으므로, `daily-trading`은 이를 실행 시작 preflight로 호출하지 않는다.

MCP `auth_token`이 `cache_authoritative=true`를 명시적으로 반환하는 버전에서는 인증 오류 복구용으로 한 번만 사용할 수 있다. 이 표시가 없으면 `auth_token` 성공을 계좌/시장 API 인증 성공의 근거로 삼지 않는다.

웹소켓 접속키가 필요한 실시간 API를 사용할 때만 별도로 발급한다.

```text
auth(api_type="auth_ws_token", params={"grant_type":"client_credentials", "env_dv":"real 또는 demo"})
```

MCP 래퍼가 appkey/appsecret을 자동 주입하지 않고 필수 오류를 반환하면, 사용자에게 KIS MCP 설정의 appkey/appsecret 구성을 확인하라고 안내한다. 사용자가 대화창에 appkey/appsecret을 직접 붙여넣게 하지 않는다.

## 만료 판단

접근토큰의 일반 수명은 약 24시간으로 취급한다. 응답에 `expires_in`, `expires_at`, `access_token_token_expired`, `issued_at` 같은 만료 정보가 있으면 그 값을 우선한다.

만료 정보가 명확하지 않으면 다음 보수 기준을 적용한다.

- 발급 시각을 알고 있고 23시간 이상 지났으면 첫 read-only KIS 호출에서 MCP 내부 인증이 갱신되도록 한다.
- 발급 시각을 모르거나 현재 토큰 상태를 확인할 수 없으면 standalone `auth_token` 호출 없이 첫 read-only KIS 호출로 인증 상태를 확인한다.
- 계좌 조회 또는 주문 준비 전에는 30분 이내 만료가 예상되면 MCP 내부 인증 또는 `cache_authoritative=true`를 지원하는 `auth_token`만 사용한다.
- API가 인증 오류, 토큰 만료, 권한 오류를 반환하면 `cache_authoritative=true` 지원 여부에 따라 재시도 규칙을 따른다.

## 실행 프리플라이트

분석 시작 시 다음 순서로 처리한다.

1. 요청 환경을 결정한다.
   - 프롬프트에 `CODEX_MCP_TRADING_ENV=paper`가 있으면 `demo`를 사용한다.
   - 프롬프트에 `CODEX_MCP_TRADING_ENV=acct`가 있으면 `real`을 사용한다.
   - 분석 전용: 기본 `real` 시세 조회를 사용하되, 사용자가 모의를 명시하면 `demo`를 사용한다.
   - 모의거래: `demo`
   - 실전 계좌 조회 또는 실전 주문 티켓: `real`
2. standalone `auth_token`을 호출하지 않는다.
3. 첫 read-only KIS 계좌/시장 API 호출이 MCP 내부 인증을 수행하게 한다.
4. 인증 성공 여부와 확인 가능한 만료 예정 시각만 내부 상태에 기록한다.
5. 토큰 원문은 출력하지 않는다.

## 재시도 규칙

API 호출이 인증 문제로 실패하면 다음 순서로 처리한다. 인증 문제는 호출 지점 backoff 대상이 아니다.

1. 실패한 API명과 환경(`real`/`demo`)을 기록한다.
2. MCP `auth_token` 응답이 `cache_authoritative=true`를 지원하는지 확인된 경우에만 `auth_token`을 한 번 호출하고, 그렇지 않으면 토큰 캐시 불일치 가능성을 기록한 뒤 KIS 의존 작업을 차단한다.
3. `auth_token`을 호출했다면 실패한 API를 같은 파라미터로 한 번만 재시도한다.
4. 실패한 호출이 수집 sub agent의 호출이면, sub agent는 인증 실패를 메인 Codex에 반환하고 메인 Codex가 2~3번을 수행하게 한다. 계좌 조회는 Main agent가 직접 인증 실패를 처리한다.
5. 재시도하지 않았거나 재시도 후에도 실패하면 해당 데이터는 `누락`으로 표시하고, 오류 요약에 인증 실패를 남긴다.
6. 주문 또는 계좌 API라면 재시도 실패 시 주문을 진행하지 않는다.

## 인증 상태 요약

사용자 응답과 리포트에는 토큰 원문 없이 아래 수준만 남긴다.

```markdown
## 인증 상태
- 환경: real / demo
- 접근토큰 상태: 신규 발급 / 기존 토큰 사용 / 재발급 후 사용 / 실패
- 만료 예정: 확인된 시각 또는 확인 불가
- 재시도 여부:
- 실패한 인증 관련 API:
```

## 금지 사항

- appkey/appsecret을 스킬 파일, 리포트, sub agent 입력에 포함
- 접근토큰 원문 출력
- 토큰 재발급 실패 상태에서 계좌/주문 API 진행
- 인증 오류 후 무한 재시도
- `real`과 `demo` 토큰 혼용
- 수집 sub agent가 인증 실패 후 자체 `auth_token` 발급
- `first-verdict`·`second-verdict` sub agent의 인증 또는 KIS API 호출
