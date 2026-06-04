---
name: daily-trading
description: "[v20260605-01] 한국 주식·ETF 투자 판단을 단일 종목 법정 평결 또는 다종목 포트폴리오 스크리닝 형식으로 작성하고, KIS OAuth 접근토큰 만료를 확인해 재발급한 뒤 명시 요청 시 보유 현금·보유수량 기준의 모의거래 주문 또는 실전거래 주문을 준비·제출한다. Use when the user asks for KIS MCP 기반 단일/다종목 투자 판단, portfolio 종목 스크리닝, sub agent 기반 분석가·배심원·판사 오케스트레이션, 매수/보유/매도 평결 리포트, 주문검토대상 산출, 모의거래 실행, 실전거래 예약주문 또는 장중 주문 실행/검토."
---

# 주식 법정 평결 오케스트레이터

## 호출

하나 이상의 종목 식별자를 받는다. 식별자는 숫자 코드, 종목명, ETF/ETN 이름, 사용자가 제공한 portfolio 목록이 될 수 있다. 특정 숫자 형식으로 강제하지 않는다.

종목명만 받은 경우 `domestic_stock(api_type="find_stock_code")`로 먼저 식별자를 확인한다. ETF/ETN이면 `etfetn` 규칙을 함께 적용한다. 도구로 해석할 수 없는 식별자는 실패로 중단하지 말고 `해석 불가` 또는 `누락`으로 기록한다.

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

1. 입력 종목 식별자를 정리한다. 명시 입력이 없고 portfolio 확인 요청이 있으면 `$check-portfolio` 결과를 사용한다.
2. `references/rules/auth-token.md`를 읽고 프롬프트에 주입된 `CODEX_MCP_TRADING_ENV`를 우선해 요청 환경(`real` 또는 `demo`)의 KIS 접근토큰 상태를 확인한다.
3. 접근토큰이 없거나 만료되었거나 만료 임박이면 `auth(api_type="auth_token")`으로 재발급한다.
4. `references/rules/data-collection.md`를 읽고, 실행 전 각 KIS API의 `find_api_detail`로 현재 파라미터를 확인한다.
5. `kis-trade-mcp` 도구로 필수 데이터를 수집한다. 추측한 파라미터를 쓰지 말고 도구 상세 문서에서 확인된 값을 사용한다. KIS MCP 호출은 기본적으로 순차 실행하고 호출 사이에 최소 1초 간격을 둔다. 특히 계좌·잔고·주문·주문가능·체결조회 같은 원장 API는 병렬 호출하지 않는다.
6. MCP 미연결, 인증 실패, 도구 응답 누락이 있으면 누락 API와 필드를 정리하고, 인증 재발급 후 한 번만 재시도한다. 그래도 실패하면 부분 평결 진행 여부를 사용자에게 묻는다.
7. 수집 결과를 단일 종목 데이터 패키지 또는 다종목 `종목 카드` 목록으로 정규화한다.
8. 단일 종목이면 단일 종목 모드, 여러 종목이면 다종목 포트폴리오 모드로 진행한다.
9. `references/rules/strategy-mapping.md`를 읽고 가능한 전략 신호를 산출한다. `kis-ai-extensions` 도구가 없거나 불명확하면 수집 차트 데이터 기반의 수동 기술 신호로 대체했다고 명시한다.
10. 아래 sub agent 오케스트레이션 절차에 따라 결과를 수집한다.
11. 사용자가 명시적으로 모의거래 실행 또는 실전거래 주문 실행/준비를 요청했으면 `references/rules/trade-execution.md`를 읽고 주문검토대상에 대해서만 거래 처리 절차를 따른다.
12. `references/rules/report-template.md` 형식으로 단일 종목은 `reports/YYYY-MM-DD_종목명.md`, 다종목은 `reports/YYYY-MM-DD_포트폴리오.md` 파일을 만든다. `reports` 폴더는 실행 시점에만 생성한다.
13. 사용자에게 저장 경로, 인증 상태 요약, 평결, 주문검토대상, 현재 계좌 및 거래 상태 요약을 짧게 보고한다.

## Sub Agent 오케스트레이션

사용자가 sub agent 기반 실행을 명시한 경우, sub agent 도구가 사용 가능한 환경에서는 반드시 역할별 agent를 생성한다. 일반 종목 분석 요청처럼 sub agent 실행 동의가 불명확한 경우에는 생성 전에 사용자에게 확인한다. Sub agent 도구가 없거나 정책상 사용할 수 없으면 사용자에게 알리고, 단일 Codex 내부 시뮬레이션으로 진행할지 확인한다.

### 공통 원칙

- 메인 Codex만 KIS MCP와 외부 데이터 도구를 호출한다. Sub agent는 추가 API 호출이나 파일 쓰기를 하지 않는다.
- 각 sub agent에는 완성된 데이터 패키지, 필요한 전략 신호, 자기 페르소나, 출력 형식만 전달한다.
- 단일 종목 모드에서 `fork_context`는 기본적으로 `false`로 둔다. 이전 agent의 추론이나 대화 맥락이 다른 agent에게 새지 않게 한다.
- Sub agent 프롬프트에는 해당 역할의 산출물만 요구한다. 리포트 파일 생성은 메인 Codex만 수행한다.
- Sub agent 결과에 누락 데이터가 있으면 추정하지 말고 `누락`으로 유지한다.

### 단일 종목 모드

기존 법정 평결 흐름을 유지한다. 분석가 7명, 배심원 10명, 판사 3명을 사용하고, 판사는 배심원 다수결을 기본으로 따르되 분석가 근거가 객관적으로 우세하면 뒤집을 수 있다. 뒤집을 때는 어떤 분석가의 어떤 데이터 근거가 결정적이었는지 명시한다.

#### 분석가 7명

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

#### 배심원 10명

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

#### 판사 3명

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

#### 결과 수집

1. 분석가 결과 7건과 배심원 결과 10건을 원문 그대로 보존한다.
2. 배심원 표를 집계해 매수, 보유, 매도 수를 계산한다.
3. 판사 결과 3건을 검토해 다수결 채택 여부와 뒤집기 사유가 빠졌는지 확인한다.
4. 빠진 항목이 있으면 해당 역할 sub agent에 한 번만 보완 요청한다.
5. 보완 후에도 누락된 항목은 메인 Codex가 임의로 채우지 말고 `누락`으로 표시한다.

### 다종목 포트폴리오 모드

다종목 모드는 종목별 정밀 법정 평결이 아니라 포트폴리오 스크리닝과 주문검토대상 산출을 목적으로 한다.

1. 메인 Codex가 모든 후보의 시세, 핵심 차트, 뉴스/시장 맥락, 계좌 보유, 당일 체결, 미체결/예약 상태를 수집해 종목 카드 목록을 만든다.
2. 분석가와 배심원 sub agent를 역할별로 한 번 생성한다.
3. 살아있는 sub agent에 추가 입력을 보낼 수 있으면 같은 agent에 종목 카드를 하나씩 순서대로 전달한다.
4. 추가 입력을 보낼 수 없으면 같은 출력 형식을 유지해 종목별 독립 요청으로 대체한다.
5. 각 sub agent 응답에는 반드시 `종목식별자`와 `종목명`을 포함시켜 이전 종목 판단과 섞이지 않게 한다.
6. 메인 Codex는 종목별 응답을 집계해 `주문검토대상`과 `제외`를 나눈다.
7. 판사 3명은 전체 후보 요약과 주문검토대상 요약을 받아 단기, 중기, 장기 포트폴리오 평결을 작성한다.
8. 상대 비교와 포트폴리오 비중 판단은 허용하지만, 종목별 응답의 누락 데이터를 다른 종목 데이터로 보완하지 않는다.

`주문검토대상`은 최종 판사 평결이 `매수` 또는 `매도`이고, 확신도 6 이상이며, 핵심 누락 데이터가 없고, 계좌상 실제 주문 가능성이 있는 종목이다. 메인 Codex는 주문검토대상 전체를 순차 검증한다. 계좌, 잔고, 주문가능, 미체결, 예약주문 조회가 실패하면 해당 종목 주문은 제출하지 않고 보류 사유를 기록한다.

## 데이터 패키지

```markdown
# 데이터 패키지
- 종목명:
- 종목식별자:
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

## 종목 카드

다종목 포트폴리오 모드에서는 각 후보를 아래 형식으로 압축한다.

```markdown
## 종목 카드
- 종목식별자:
- 종목명:
- 상품유형: 주식 / ETF / ETN / 기타 / 해석 불가
- 수집 시각:
- 현재가 / 등락률 / 거래량:
- 핵심 차트:
- 핵심 수급:
- 핵심 재무 또는 ETF/NAV:
- 뉴스/시장 맥락:
- 계좌 보유수량:
- 매수가능 또는 매도가능 관련 정보:
- 미체결/예약 주문:
- 누락 데이터:
- 메인 Codex 1차 전략 신호: BUY / SELL / HOLD / ERROR
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
- 사용자가 `예약거래`, `예약 거래`, `예약주문`, `예약 주문`을 명시하면 명시적 거래 처리 요청으로 보고 `references/rules/trade-execution.md`를 읽은 뒤 예약주문 절차로 처리한다.
- 장외 시간 작업을 전제로 하므로 주문은 기본적으로 예약주문 형태로 준비한다. 예약주문 API가 요청 환경에서 지원되고 주문 조건 검증이 끝난 경우에만 예약주문을 제출한다.
- 매수는 `매수가능조회`, 매도는 `잔고조회`와 `매도가능수량조회`를 먼저 확인한다. 확인 실패 시 주문을 진행하지 않는다.
- 주문 수량은 기본 1주가 아니라 `references/rules/trade-execution.md`의 적극적 비중 산정표에 따라 주문가능금액 또는 매도가능수량 기준으로 계산한다.
- 응답 마지막에는 현재 계좌 요약, 보유/현금/주문가능 요약, 미체결·예약 주문 상태, 이번 작업에서 실제 제출된 주문 여부를 명시한다.

## 저장 규칙

- 파일명은 `reports/YYYY-MM-DD_종목명.md`로 저장한다.
- 모든 본문은 한국어로 작성한다.
- 누락 데이터는 삭제하지 않고 `누락`으로 표시한다.
- 투자 권유가 아니라 의사결정 보조 분석임을 리포트 메모에 남긴다.
