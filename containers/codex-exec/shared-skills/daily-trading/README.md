# daily-trading

`daily-trading`은 한국 주식·ETF 포트폴리오 전체를 대상으로 수집, 평결, 주문 실행 경계를 정의하는 프롬프트 계약형 스킬이다.

Sub-agent 모델과 effort는 `scripts/run_subagent.py`가 `codex exec` 명령으로 지정한다.

## 용어 규칙

| 개념 | 표준 표기 |
|---|---|
| 메인 실행 주체 | `Main agent` |
| 1차 독립 종목 평결 단계 | `first-verdict` |
| 2차 포트폴리오 목표수량 평결 단계 | `second-verdict` |
| 평결용 압축 입력 | `decision-brief.json` |

문장 설명은 한국어로 쓰되, stage 이름, 파일명, JSON enum 값은 위 표준 표기를 그대로 사용한다.

## 전체 동작 Flow

| 단계 | 주체 | 사용하는 skill / sub-agent | 주요 입력 | 주요 출력 | 핵심 gate |
|---:|---|---|---|---|---|
| 1 | `Main agent` | `$check-portfolio`(요청 시), KIS read-only API auto-auth | 사용자 요청, portfolio 설정, `run_id`, `started_at`, `CODEX_MCP_TRADING_ENV` | `run.json`, 설정 종목 목록 | 인증 경계와 입력 범위 확정 |
| 2 | `Main agent` | KIS read-only account APIs | 설정 종목 목록, 거래 환경 | `account-before-verdict.json`, 설정 종목 + 현재 보유 종목을 합친 전체 종목 universe | 실패 시 현재 보유 unknown 처리, 주문 준비/실행 차단 |
| 3 | Collection sub-agents | `market`, `$collect-financial-information`, `$collect-news-information` | 전체 종목 universe, 거래 환경 | `market.json`, 선택적 `financial.md`, 선택적 `news.md` | market은 필수, financial/news는 best-effort 평문 참고자료이며 누락·실패만으로 주문을 막지 않음 |
| 4 | `Main agent` | 내부 병합/sanitize | `market.json`, 선택적 `financial.md`, 선택적 `news.md`, `account-before-verdict.json` | `decision-brief.json`, 제외 종목 목록 | 식별자와 가격 snapshot이 있으면 재무/뉴스 누락만으로 제외하지 않음; verdict fan-out 비용을 줄이기 위해 brief는 압축 |
| 5 | `first-verdict` sub-agents | selected 7 personas | `decision-brief.json`, `verdict-format.md` | `verdict-first.json`, `verdicts/first-verdict--<agent_role>--<task_name>.md` | 외부 호출·다른 agent 결과 참조 금지; companion MD는 사람 확인용 |
| 6 | `second-verdict` sub-agents | 2 judge personas | `decision-brief.json`, `verdict-first.json`, `verdict-format.md` | `verdict-second.json`, `verdicts/second-verdict--<agent_role>--<task_name>.md` | 목표수량은 judge가 제안, `Main agent`가 reconciliation 수행; companion MD는 사람 확인용 |
| 7 | `Main agent` | KIS read-only account APIs, 내부 주문 계산, KIS `order_cash` 또는 `order_resv` | `verdict-second.json`, 최신 계좌 상태, 사용자 제한, 명시적 demo/real 실행 요청 | `account-before-order.json`, `execution.json` | 계좌 조회 후 주문 후보 계산과 제출/차단/실패를 한 단계에서 처리; 명시적 주문 요청이 없으면 제출 없이 skipped 처리 |
| 8 | `Main agent` | report template, run artifact update | 모든 run artifact | `reports/YYYY-MM-DD_포트폴리오.md`, 최종 `run.json` | partial/failed artifact를 삭제하지 않음 |

## Main agent 책임

`Main agent`만 아래 작업을 수행할 수 있다.

- stage와 sub-agent 오케스트레이션
- `reports/` 아티팩트 생성과 갱신
- KIS 인증 경계 처리
- read-only 계좌 조회와 `account-before-verdict.json`, `account-before-order.json` 작성
- 모든 sub-agent 출력과 저장 아티팩트 sanitize
- 수집 스냅샷 병합과 종목 제외 판정
- `decision-brief.json` 생성
- 평결 결과 조정과 주문 후보 생성
- 명시적 주문 승인 여부 확인
- 실행 gate를 통과한 주문 후보의 실제 주문 제출

## Sub-Agent

| Agent | 역할 | launcher model | launcher effort |
|---|---|---|---|
| `market` | KIS 시세, 차트, 호가/체결, 수급, 랭킹, 업종, ETF/NAV 수집 | `gpt-5.5` | `low` |
| `collect-financial-information` | KIS 및 공식 재무 정보 평문 요약 | `gpt-5.5` | `low` |
| `collect-news-information` | KIS 뉴스/공시 평문 요약 | `gpt-5.5` | `low` |
| selected 7 personas | `first-verdict` 독립 종목 점수 | `gpt-5.5` | `low` |
| 2 judge personas | `second-verdict` 포트폴리오 목표수량 | `gpt-5.5` | `low` |

## API 권한

`Main agent` 계좌 조회 허용 범위:

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

News 수집 허용 범위:

- KIS 뉴스 및 KIS 공시 계열 API만 허용

모든 sub-agent 금지 범위:

- 주문 제출
- 예약 주문 제출
- 정정
- 취소
- canonical 아티팩트 쓰기
- 민감정보 반환

`first-verdict`, `second-verdict` agent 금지 범위:

- KIS, MCP, web, network, shell, 외부 출처 호출
- 제공된 persona 본문 외 파일 읽기
- `reports/runs/<run_id>/verdicts/<stage>--<agent_role>--<task_name>.md` 외 파일 쓰기
- 재수집
- 주문 API

평결 agent가 쓰는 companion Markdown은 사람이 각 자산 판단을 확인하기 위한 보조 산출물이다. 파일명은 `references/rules/verdict-format.md`의 safe-name 규칙을 따른다. 이 Markdown은 점수 집계, 목표수량 조정, 주문 후보 계산, 실행 gate의 입력으로 쓰지 않는다.

## 아티팩트

Run 아티팩트는 `reports/runs/<run_id>/` 아래에 둔다.

- `run.json`
- `market.json`
- `financial.md` (optional best-effort plain text)
- `news.md` (optional best-effort plain text)
- `account-before-verdict.json`
- `decision-brief.json`
- `verdict-first.json`
- `verdict-second.json`
- `verdicts/<stage>--<agent_role>--<task_name>.md` (verdict agent별 사람 확인용 companion Markdown)
- `account-before-order.json`
- `execution.json`

## 평결 입력

`decision-brief.json`은 `first-verdict`, `second-verdict`의 기본 입력이자 Main agent가 수집 결과를 합쳐 만든 canonical verdict input이다.

`decision-brief.json`에는 종목 식별자, eligibility, `evidence_mode`, 가격, 핵심 market signal, 가능한 financial summary, 가능한 KIS 뉴스/공시 요약, 계좌 노출, 누락/오류 사유를 압축해서 담는다. `financial.md`와 `news.md`는 사람이 읽는 참고자료이며, Main agent는 그중 짧은 bullet만 선별해 `decision-brief.json`에 복사한다. 긴 raw API payload, 기사 원문, 반복 source detail, 민감정보는 제외한다. 종목별 market signal은 최대 5개, financial summary는 최대 3개 bullet, KIS 뉴스/공시는 최대 3개 item, warning/error는 최대 5개로 제한하고 반복되는 domain-wide 누락 사유는 한 번만 요약한다.

재무/뉴스가 없더라도 식별자, 종목명, 현재가 또는 직전 거래일 가격 snapshot, 관측시각이 있으면 `eligible_for_verdict=true`로 유지한다.

종목이 eligible이고 가격 관측값과 목표수량, 계좌 제약, 주문유형, 사용자 제한, 당일 반복매매 guard를 통과하면 재무/뉴스 누락·partial·failed·no-data는 `order_cash`, `order_resv`, demo, real 제출을 단독으로 차단하지 않는다.

## 주문 경계

`Main agent`는 `references/rules/trade-execution.md`의 실행 gate를 직접 적용한다. 별도 최종 리스크 sub-agent와 승인 아티팩트는 사용하지 않는다.

`expected_holding_quantity`는 현재 후보 주문 제출 전, 이미 존재하는 미체결·예약 수량만 반영한 예상 보유수량이다. `target_holding_quantity`와 다르다는 이유만으로 후보 불일치나 주문 차단으로 판단하지 않는다.

사용자 또는 schedule이 demo 또는 real 실행을 명시했고 `references/rules/trade-execution.md`의 모든 실행 gate를 통과한 경우에만 `Main agent`가 주문을 제출한다.
