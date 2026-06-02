---
name: daily-trading
description: "[v20260602-01] 한국 주식투자 의사결정 리포트를 법정 평결 형식으로 작성하고, KIS OAuth 접근토큰 만료를 확인해 재발급한 뒤 명시 요청 시 보유 현금·보유수량 기준의 적극적 모의거래 주문 또는 실전거래 주문을 준비·제출한다. Use when the user asks for /stock-trial, stock-trial, KIS MCP 기반 투자 판단, KIS auth_token 재발급, sub agent 기반 분석가·배심원·판사 오케스트레이션, 매수/보유/매도 평결 리포트, 적극적 주문 수량 산정, 모의거래 실행, 실전거래 예약주문 실행/검토. For ordinary Korean stock analysis requests that do not mention /stock-trial or sub agent execution, ask before spawning sub agents."
---

# 주식 법정 평결 오케스트레이터

## 호출

`/stock-trial 005930`처럼 6자리 한국 주식 종목코드를 받는다. `/stock-trial` 호출은 분석가, 배심원, 판사 역할을 sub agent로 분리해 실행하는 명시적 opt-in이다.

종목코드는 정규식 `^[0-9]{6}$`로 검증한다. 종목명만 받은 경우 `domestic_stock(api_type="find_stock_code")`로 먼저 6자리 코드를 확인한다.

## 참조 자료

필요한 파일만 읽는다.

- 데이터 수집: `references/rules/data-collection.md`
- 인증 토큰: `references/rules/auth-token.md`
- 전략 매핑: `references/rules/strategy-mapping.md`
- 출력 표준: `references/rules/verdict-format.md`
- 최종 리포트: `references/rules/report-template.md`
- 거래 실행: `references/rules/trade-execution.md`
- 분석가 페르소나: `references/personas/analyst-*.md`
- 배심원 페르소나: `references/personas/juror-*.md`
- 판사 페르소나: `references/personas/judge-*.md`

## 실행 순서

1. 휴장일 게이트를 가장 먼저 실행한다. 한국시장 기준 오늘 날짜(`Asia/Seoul`, `YYYYMMDD`)로 `domestic_stock(api_type="find_api_detail", params={"api_type":"chk_holiday"})`를 확인한 뒤 `domestic_stock(api_type="chk_holiday")`를 호출한다. 필수 파라미터는 `bass_dt=YYYYMMDD`, `tr_cont=""`, `depth=0`, `max_depth=10`이다.
2. `chk_holiday` 응답에서 오늘 날짜(`bass_dt`) 행을 찾는다. `opnd_yn!="Y"`이면 휴장일 또는 비개장일로 판단하고, 종목 검증·인증 재발급·데이터 수집·분석·sub agent 생성·주문 준비·주문 제출·리포트 생성을 모두 하지 않는다. 사용자에게 휴장일 차단 사유와 해당 응답 필드만 짧게 보고하고 종료한다.
3. 휴장일 API가 실패하거나 오늘 날짜 행이 없으면 휴장 여부 미확인 상태로 간주하되, 실패 API와 누락 필드를 기록하고 기존 실행 순서대로 계속 진행한다.
4. 입력 종목코드를 검증한다.
5. `references/rules/auth-token.md`를 읽고 프롬프트에 주입된 `CODEX_MCP_TRADING_ENV`를 우선해 요청 환경(`real` 또는 `demo`)의 KIS 접근토큰 상태를 확인한다.
6. 접근토큰이 없거나 만료되었거나 만료 임박이면 `auth(api_type="auth_token")`으로 재발급한다.
7. `references/rules/data-collection.md`를 읽고, 실행 전 각 KIS API의 `find_api_detail`로 현재 파라미터를 확인한다.
8. `kis-trade-mcp` 도구로 필수 데이터를 수집한다. 추측한 파라미터를 쓰지 말고 도구 상세 문서에서 확인된 값을 사용한다. KIS MCP 호출은 기본적으로 순차 실행하고 호출 사이에 최소 1초 간격을 둔다. 특히 계좌·잔고·주문·주문가능·체결조회 같은 원장 API는 병렬 호출하지 않는다.
9. MCP 미연결, 인증 실패, 도구 응답 누락이 있으면 누락 API와 필드를 정리하고, 인증 재발급 후 한 번만 재시도한다. 그래도 실패하면 부분 평결 진행 여부를 사용자에게 묻는다.
10. `references/rules/strategy-mapping.md`를 읽고 가능한 전략 신호를 산출한다. `kis-ai-extensions` 도구가 없거나 불명확하면 수집 차트 데이터 기반의 수동 기술 신호로 대체했다고 명시한다.
11. 아래 sub agent 오케스트레이션 절차에 따라 분석가 7명, 배심원 10명, 판사 3명을 생성하고 결과를 수집한다.
12. 판사는 배심원 다수결을 기본으로 따르되 분석가 근거가 객관적으로 우세하면 뒤집을 수 있다. 뒤집을 때는 어떤 분석가의 어떤 데이터 근거가 결정적이었는지 명시한다.
13. 사용자가 명시적으로 모의거래 실행 또는 실전거래 주문 실행/준비를 요청했으면 `references/rules/trade-execution.md`를 읽고 거래 처리 절차를 따른다.
14. `references/rules/report-template.md` 형식으로 `reports/YYYY-MM-DD_종목명.md` 파일을 만든다. `reports` 폴더는 실행 시점에만 생성한다.
15. 사용자에게 저장 경로, 인증 상태 요약, 단기·중기·장기 평결, 확신도, 핵심 리스크, 현재 계좌 및 거래 상태 요약을 짧게 보고한다.

## Sub Agent 오케스트레이션

사용자가 `/stock-trial` 또는 sub agent 기반 실행을 명시한 경우, sub agent 도구가 사용 가능한 환경에서는 반드시 `spawn_agent`로 역할별 agent를 생성한다. 일반 종목 분석 요청처럼 sub agent 실행 동의가 불명확한 경우에는 생성 전에 사용자에게 확인한다. Sub agent 도구가 없거나 정책상 사용할 수 없으면 사용자에게 알리고, 단일 Codex 내부 시뮬레이션으로 진행할지 확인한다.

### 공통 원칙

- 메인 Codex만 KIS MCP와 외부 데이터 도구를 호출한다. Sub agent는 추가 API 호출이나 파일 쓰기를 하지 않는다.
- 각 sub agent에는 완성된 데이터 패키지, 필요한 전략 신호, 자기 페르소나, 출력 형식만 전달한다.
- `fork_context`는 기본적으로 `false`로 둔다. 이전 agent의 추론이나 대화 맥락이 다른 agent에게 새지 않게 한다.
- Sub agent 프롬프트에는 해당 역할의 산출물만 요구한다. 리포트 파일 생성은 메인 Codex만 수행한다.
- Sub agent 결과에 누락 데이터가 있으면 추정하지 말고 `누락`으로 유지한다.

### 분석가 7명

분석가 페르소나 파일 7개를 각각 읽고, 7개의 sub agent를 병렬 생성한다.

- `references/personas/analyst-blackrock.md`
- `references/personas/analyst-fidelity.md`
- `references/personas/analyst-goldman.md`
- `references/personas/analyst-jpmorgan.md`
- `references/personas/analyst-morganstanley.md`
- `references/personas/analyst-statestreet.md`
- `references/personas/analyst-vanguard.md`

각 분석가 sub agent에 전달할 항목:

- 데이터 패키지 전체
- 전략 신호 전체
- 해당 분석가 페르소나 본문
- `references/rules/verdict-format.md`의 분석가 의견서 형식

각 분석가 sub agent에 금지할 항목:

- 다른 분석가 결과
- 배심원 결과
- 판사 결과
- 파일 쓰기 또는 주문 실행

### 배심원 10명

분석가 결과를 기다리지 않고, 데이터 패키지가 준비되면 배심원 페르소나 파일 10개를 각각 읽고 10개의 sub agent를 병렬 생성한다.

- `references/personas/juror-01-가치투자자.md`
- `references/personas/juror-02-성장주헌터.md`
- `references/personas/juror-03-모멘텀트레이더.md`
- `references/personas/juror-04-배당주신봉자.md`
- `references/personas/juror-05-역발상.md`
- `references/personas/juror-06-ESG중시.md`
- `references/personas/juror-07-매크로탑다운.md`
- `references/personas/juror-08-퀀트팩터.md`
- `references/personas/juror-09-차티스트.md`
- `references/personas/juror-10-보수적은퇴자.md`

각 배심원 sub agent에 전달할 항목:

- 데이터 패키지 전체
- 해당 배심원 페르소나 본문
- `references/rules/verdict-format.md`의 배심원 투표 형식

각 배심원 sub agent에 금지할 항목:

- 전략 신호 외 해석 보강 자료
- 분석가 결과
- 다른 배심원 결과
- 판사 결과
- 파일 쓰기 또는 주문 실행

### 판사 3명

분석가 7건과 배심원 10표가 모두 수집된 뒤 판사 페르소나 파일 3개를 각각 읽고 3개의 sub agent를 병렬 생성한다.

- `references/personas/judge-shortterm.md`
- `references/personas/judge-midterm.md`
- `references/personas/judge-longterm.md`

각 판사 sub agent에 전달할 항목:

- 데이터 패키지 전체
- 전략 신호 전체
- 분석가 의견서 7건
- 배심원 투표 10건
- 해당 판사 페르소나 본문
- `references/rules/verdict-format.md`의 판사 평결문 형식

각 판사 sub agent는 자기 시계열만 평결한다. 예를 들어 단기 판사는 단기 평결문만 작성한다.

### 결과 수집

1. 분석가 결과 7건과 배심원 결과 10건을 원문 그대로 보존한다.
2. 배심원 표를 집계해 매수, 보유, 매도 수를 계산한다.
3. 판사 결과 3건을 검토해 다수결 채택 여부와 뒤집기 사유가 빠졌는지 확인한다.
4. 빠진 항목이 있으면 해당 역할 sub agent에 한 번만 보완 요청한다.
5. 보완 후에도 누락된 항목은 메인 Codex가 임의로 채우지 말고 `누락`으로 표시한다.

## 데이터 패키지

```markdown
# 데이터 패키지
- 종목명:
- 종목코드:
- 수집 시각:
- 누락 데이터:

## 시세
- 현재가:
- 등락률:
- 거래량:
- 52주 최고/최저:

## 차트
- 일봉:
- 주봉:
- 월봉:

## 호가/체결
- 호가:
- 예상체결:
- 체결:

## 재무/추정
- PER/PBR/EPS/BPS:
- 매출/영업이익/순이익:
- ROE/부채비율:
- 배당:

## 수급
- 외국인:
- 기관:
- 개인:

## 순위/업종/ETF
- 등락률/거래량/시총 순위:
- 업종:
- ETF/NAV:
```

## 오류 처리

- MCP 미연결: `kis-trade-mcp 연결 실패`와 실행하지 못한 도구명을 보고한다.
- 인증 실패: `references/rules/auth-token.md` 절차에 따라 접근토큰을 재발급하고 한 번만 재시도한다. 재시도 실패 시 `auth_token 재발급 실패`와 실패 환경(`real`/`demo`)을 보고한다.
- 응답 필드 누락: 누락 필드와 영향받는 분석 역할을 함께 보고한다.
- 사용자가 부분 평결을 거부하면 리포트를 생성하지 않는다.

## 거래 처리 원칙

- 분석만 요청한 경우에는 어떤 주문도 준비하거나 실행하지 않는다.
- 프롬프트에 `CODEX_MCP_TRADING_ENV`가 있으면 사용자 표현보다 우선한다. `paper`는 `env_dv="demo"`, `acct`는 `env_dv="real"`을 사용한다.
- 사용자가 `모의거래로 실행`, `데모로 주문`, `demo 주문`처럼 모의거래를 명시하면 `env_dv="demo"`로만 주문 절차를 진행한다.
- 사용자가 `실제 거래 실행`, `실전 주문 실행`, `real 주문 제출`처럼 실전거래 실행을 명시하면 모의거래와 동일한 수준의 검증 후 실전 주문 API를 제출할 수 있다. `준비`, `검토`, `티켓 작성`처럼 제출 의사가 불명확한 요청은 예약주문 티켓, 주문 근거, 계좌·잔고·주문가능 상태, 최종 제출 체크리스트까지만 작성한다.
- 장외 시간 작업을 전제로 하므로 주문은 기본적으로 예약주문 형태로 준비한다. 예약주문 API가 요청 환경에서 지원되고 주문 조건 검증이 끝난 경우에만 예약주문을 제출한다.
- 매수는 `매수가능조회`, 매도는 `잔고조회`와 `매도가능수량조회`를 먼저 확인한다. 확인 실패 시 주문을 진행하지 않는다.
- 주문 수량은 기본 1주가 아니라 `references/rules/trade-execution.md`의 적극적 비중 산정표에 따라 주문가능금액 또는 매도가능수량 기준으로 계산한다.
- 응답 마지막에는 현재 계좌 요약, 보유/현금/주문가능 요약, 미체결·예약 주문 상태, 이번 작업에서 실제 제출된 주문 여부를 명시한다.

## 저장 규칙

- 파일명은 `reports/YYYY-MM-DD_종목명.md`로 저장한다.
- 모든 본문은 한국어로 작성한다.
- 누락 데이터는 삭제하지 않고 `누락`으로 표시한다.
- 투자 권유가 아니라 의사결정 보조 분석임을 리포트 메모에 남긴다.
