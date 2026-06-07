# daily-trading

`daily-trading`은 한국 주식·ETF 포트폴리오 전체를 대상으로 수집, 평결, 주문 실행 경계를 정의하는 프롬프트 계약형 스킬이다.

Stage별 모델과 effort는 daily-trading prompt에 주입되는 필수 계약이다. 런타임 validator는 `stage-metrics.json`의 `actual_model`/`actual_effort`가 필수값과 다르면 validation failed로 처리한다.

## 용어 규칙

| 개념 | 표준 표기 |
|---|---|
| 메인 실행 주체 | `Main agent` |
| 1차 독립 종목 평결 단계 | `first-verdict` |
| 2차 포트폴리오 목표수량 평결 단계 | `second-verdict` |
| 최종 주문 리스크 승인 단계 | `final-risk-verdict` |
| 평결용 압축 입력 | `decision-brief.json` |
| 최종 주문 리스크 승인 아티팩트 | `final-order-verdict.json` |

문장 설명은 한국어로 쓰되, stage 이름, 파일명, JSON enum 값은 위 표준 표기를 그대로 사용한다.

## 전체 동작 Flow

| 단계 | 주체 | 사용하는 skill / sub-agent | 주요 입력 | 주요 출력 | 핵심 gate |
|---:|---|---|---|---|---|
| 1 | `Main agent` | `$check-portfolio`(요청 시), `$check-holiday`, KIS `auth_token` | 사용자 요청, portfolio 설정, `run_id`, `started_at`, `CODEX_MCP_TRADING_ENV` | `run.json`, 초기 `stage-metrics.json`, 한국장 `open/closed/unknown`, 실행 종목 universe 초안 | `unknown`이면 실전 주문/예약 제출 차단 |
| 2 | Account sub-agent | `$collect-account-state` with `account-before-verdict` | 실행 종목 universe 초안, 거래 환경 | `account-before-verdict.json` | 실패 시 현재 보유 unknown 처리, 주문 준비/실행 차단 |
| 3 | Collection sub-agents | `market`, `$collect-financial-information`, `$collect-news-information`, `$gate-kis-calls` | 전체 종목 universe, 거래 환경, 재무 cache payload | `market.json`, `financial.json`, `news.json`, 재무 cache update 후보 | collector는 파일 저장·계좌·주문 API 금지 |
| 4 | `Main agent` | 내부 병합/sanitize | `market.json`, `financial.json`, `news.json`, `account-before-verdict.json` | `merged.json`, `decision-brief.json`, 제외 종목 목록 | 식별자와 가격 snapshot이 있으면 재무/뉴스 누락만으로 제외하지 않고 `price_only`로 평결 |
| 5 | `first-verdict` sub-agents | 7 analyst personas, 10 juror personas | `decision-brief.json`, `verdict-format.md` | `verdict-first.json` | 외부 호출·파일 쓰기·다른 agent 결과 참조 금지 |
| 6 | `second-verdict` sub-agents | 3 judge personas | `decision-brief.json`, `verdict-first.json`, `verdict-format.md` | `verdict-second.json` | 목표수량은 judge가 제안, `Main agent`가 reconciliation 수행 |
| 7 | Account sub-agent | `$collect-account-state` with `account-before-order` | `second-verdict` 대상, 주문 후보 가능 종목 | `account-before-order.json` | missing/invalid/failed이면 주문 후보 계산과 제출 차단 |
| 8 | `Main agent` | 내부 주문 후보 계산, `trade-execution.md` | `verdict-second.json`, `account-before-order.json`, 사용자 제한 | 주문 후보 | 명시적 주문 요청이 없으면 제출 없이 skipped 처리 |
| 9 | `final-risk-verdict` sub-agent | `final-risk-verdict.md` persona | `decision-brief.json`, `verdict-second.json`, `account-before-order.json`, 주문 후보 | `final-order-verdict.json` | `approved`가 아니면 주문 제출 차단 |
| 10 | `Main agent` | KIS `order_cash` 또는 `order_resv` | 승인된 주문 후보, 명시적 demo/real 실행 요청 | `execution.json`, 제출/차단/실패 결과 | `Main agent`만 실제 주문 API 호출 가능 |
| 11 | `Main agent` | report template, run artifact update | 모든 run artifact, `stage-metrics.json` | `reports/YYYY-MM-DD_포트폴리오.md`, 최종 `run.json` | partial/failed artifact를 삭제하지 않음 |

## Main agent 책임

`Main agent`만 아래 작업을 수행할 수 있다.

- stage와 sub-agent 오케스트레이션
- `reports/` 아티팩트 생성과 갱신
- KIS 인증 preflight와 토큰 재발급
- 모든 sub-agent 출력과 저장 아티팩트 sanitize
- 수집 스냅샷 병합과 종목 제외 판정
- `decision-brief.json` 생성
- 평결 결과 조정과 주문 후보 생성
- 명시적 주문 승인 여부 확인
- `final-risk-verdict` 승인 후 실제 주문 제출

## Sub-Agent

| Agent | 역할 | 필수 모델 | 필수 effort |
|---|---|---|---|
| `collect-account-state` | read-only 계좌 스냅샷 | `gpt-5.3-codex-spark` | `low` |
| `market` | KIS 시세, 차트, 호가/체결, 수급, 랭킹, 업종, ETF/NAV 수집 | `gpt-5.3-codex-spark` | `low` |
| `collect-financial-information` | cache-first KIS 및 공식 재무 정보 수집 | `gpt-5.3-codex-spark` | `low` |
| `collect-news-information` | KIS 뉴스/공시 요약 전용 수집 | `gpt-5.3-codex-spark` | `low` |
| 7 analyst personas | `first-verdict` 독립 종목 점수 | `gpt-5.5` | `low` |
| 10 juror personas | `first-verdict` 독립 종목 점수 | `gpt-5.5` | `low` |
| 3 judge personas | `second-verdict` 포트폴리오 목표수량 | `gpt-5.5` | `high` |
| `final-risk-verdict` | 최종 주문 리스크 승인 | `gpt-5.5` | `high` |

`Main agent` 오케스트레이션과 실전 주문 실행의 필수값은 `gpt-5.5` 및 `medium` effort다.

## API 권한

`collect-account-state` 허용 범위:

- 계좌 자산 요약
- 잔고와 보유수량
- 당일 체결
- 미체결 주문
- 예약 주문
- 매수가능 조회
- 매도가능 조회

Market 수집 허용 범위:

- KIS 현재가, 차트, 호가/체결, 수급, 랭킹, 업종, ETF/NAV API

Financial 수집 허용 범위:

- KIS 재무 및 추정 API
- DART, KRX, 감독기관·정부 공표 자료, 발행사 IR, 공식 공시
- `reports/cache/financial/<YYYY-MM-DD>.json`의 cache hit 재사용

News 수집 허용 범위:

- KIS 뉴스 및 KIS 공시 계열 API만 허용

모든 sub-agent 금지 범위:

- 주문 제출
- 예약 주문 제출
- 정정
- 취소
- 아티팩트 쓰기
- 민감정보 반환

`first-verdict`, `second-verdict`, `final-risk-verdict` agent 금지 범위:

- KIS, MCP, web, network, shell, 외부 출처 호출
- 제공된 persona 본문 외 파일 읽기
- 파일 쓰기
- 재수집
- 주문 API

## 아티팩트

Run 아티팩트는 `reports/runs/<run_id>/` 아래에 둔다.

- `run.json`
- `stage-metrics.json`
- `market.json`
- `financial.json`
- `news.json`
- `account-before-verdict.json`
- `merged.json`
- `decision-brief.json`
- `verdict-first.json`
- `verdict-second.json`
- `account-before-order.json`
- `final-order-verdict.json`
- `execution.json`

재사용 가능한 재무 캐시는 `reports/cache/financial/<YYYY-MM-DD>.json`에 두며 한국 거래일 기준 하루 동안 유효하다.

## 평결 입력

`decision-brief.json`은 `first-verdict`, `second-verdict`, `final-risk-verdict`의 기본 입력이다. 원본 `merged.json`은 출처 추적과 감사 용도로 보존하지만 verdict sub-agent의 기본 입력은 아니다.

`decision-brief.json`에는 종목 식별자, eligibility, `evidence_mode`, 가격, 한국장 상태, 핵심 market signal, 가능한 financial summary, 가능한 KIS 뉴스/공시 요약, 계좌 노출, 누락/오류 사유를 압축해서 담는다. 긴 raw API payload, 기사 원문, 반복 source detail, 민감정보는 제외한다.

재무/뉴스가 없더라도 식별자, 종목명, 현재가 또는 직전 거래일 가격 snapshot, 관측시각이 있으면 `eligible_for_verdict=true`, `evidence_mode="price_only"`로 유지한다.

## 주문 경계

`final-risk-verdict` sub-agent는 `approved`, `blocked`, `needs_review`만 반환할 수 있다. 목표수량을 다시 계산하거나, judge 결과를 대체하거나, 주문 후보를 변경하거나, 주문 API를 호출할 수 없다.

`Main agent`는 `final-order-verdict.json`이 missing, invalid, `failed`, `blocked`, `needs_review`이면 주문 제출을 차단한다.

`final-risk-verdict`가 승인하더라도, 사용자 또는 schedule이 demo 또는 real 실행을 명시했고 `references/rules/trade-execution.md`의 모든 실행 gate와 market status gate를 통과한 경우에만 `Main agent`가 주문을 제출한다. `closed`에서는 명시된 예약주문만 `order_resv`로 허용하고, `unknown`에서는 실전 주문과 예약주문 제출을 차단한다.

## Stage Metrics

`stage-metrics.json`은 stage, agent role, 권장 모델, 권장 effort, 실제 모델, 실제 effort, 시작 시각, 종료 시각, 소요 시간, 상태, token usage 확보 여부를 기록한다.

정확한 token usage를 얻지 못하면 token 필드는 `null`로 두고, `token_source`는 `unavailable`로 기록하며, reason을 함께 남긴다.
