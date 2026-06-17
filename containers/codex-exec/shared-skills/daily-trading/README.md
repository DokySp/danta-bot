# daily-trading

`daily-trading`은 한국 주식·ETF 포트폴리오 전체를 대상으로 수집, 평결, 주문 실행 경계를 정의하는 프롬프트 계약형 스킬이다.

Sub-agent 모델과 effort는 `scripts/run_subagent.py`가 `codex exec` 명령으로 지정한다.

## 용어 규칙

| 개념 | 표준 표기 |
|---|---|
| 메인 실행 주체 | `Main agent` |
| 1차 독립 종목 평결 단계 | `first-verdict` |
| 2차 포트폴리오 목표수량 평결 단계 | `second-verdict` |
| canonical 평결 입력 | `decision-brief.json` |
| sub-agent 평결 입력 | launcher-created lossless selected-symbol slices |

문장 설명은 한국어로 쓰되, stage 이름, 파일명, JSON enum 값은 위 표준 표기를 그대로 사용한다.

## 전체 동작 Flow

| 단계 | 주체 | 사용하는 skill / sub-agent | 주요 입력 | 주요 출력 | 핵심 gate |
|---:|---|---|---|---|---|
| 1 | `Main agent` | `$check-portfolio`(요청 시), KIS direct read-only holdings API auto-auth | 사용자 요청, portfolio 설정, `run_id`, `started_at`, `CODEX_MCP_TRADING_ENV` | `run.json`, `recommanded`/`specified`/`holding`/`universe` JSON | 인증 경계와 입력 범위 확정 |
| 2 | `Main agent` | `$check-portfolio` JSON | check-portfolio `universe`, 거래 환경 | 전체 종목 universe | universe 확장을 위해 현재 보유 종목을 별도 재조회하지 않음 |
| 3 | `Main agent` + Collection sub-agents | KIS MCP 직접 가격·그래프 수집, 필요한 경우 `$collect-financial-information`, `$collect-news-information`, `$get-market-status` | 전체 종목 universe, 거래 환경 | `price-chart.json`, 선택적 financial/news memory 경로, 선택적 market-status text | 가격·관측시각은 필수, financial/news는 같은 날짜 full-universe cache hit 시 sub-agent를 생략하며, financial/news/market-status 누락·실패만으로 주문을 막지 않음 |
| 4 | `Main agent` | 내부 병합/sanitize | `price-chart.json`, `$check-portfolio` JSON, 선택적 `memory/collect-financial-information/financial-YYYY-MM-DD.yaml`, 선택적 `memory/collect-news-information/news-YYYY-MM-DD.yaml`, 선택적 market-status launcher text | `decision-brief.json`, 제외 종목 목록 | 식별자와 가격 snapshot이 있으면 재무/뉴스/market-status 누락만으로 제외하지 않음; verdict fan-out 비용을 줄이기 위해 brief는 압축 |
| 5 | `first-verdict` sub-agents | selected 4 personas | launcher-created `verdict-core`, `verdict-format.md` | `verdict-first.json`, `verdicts/first-verdict--<agent_role>--<task_name>.md` | 외부 호출·다른 agent 결과 참조 금지; sub-agent는 compact JSON만 반환하고 companion MD는 `Main agent`가 생성 |
| 6 | `second-verdict` sub-agent | `judge-midterm` | launcher-created `verdict-core`, selected-symbol first-verdict slice, `verdict-format.md` | `verdict-second.json`, `verdicts/second-verdict--judge-midterm--<task_name>.md` | 목표수량은 단일 judge가 제안하고 `Main agent`가 schema·자산·집중도·계좌 gate만 검증; 실패 시 failed task만 최대 2회 retry |
| 7 | `Main agent` | KIS read-only account APIs, 내부 주문 계산, KIS active 주문 조정, KIS `order_cash` 또는 `order_resv` | `verdict-second.json`, 최신 계좌 상태, 명시적 demo/real 실행 요청 | `account-before-order.json`, `execution.json` | 계좌 조회 후 기존 active 미체결·예약 주문을 목표수량과 대조하고, 필요한 취소·정정·대체와 신규 주문 제출/차단/실패를 한 단계에서 처리; 명시적 주문 요청이 없으면 제출 없이 skipped 처리 |
| 8 | `Main agent` | report template, run artifact update | 모든 run artifact | `reports/YYYY-MM-DD_포트폴리오.md`, 최종 `run.json` | partial/failed artifact를 삭제하지 않음 |

## Main agent 책임

`Main agent`만 아래 작업을 수행할 수 있다.

- stage와 sub-agent 오케스트레이션
- `reports/` 아티팩트 생성과 갱신
- KIS 인증 경계 처리
- `$check-portfolio` JSON universe 사용, read-only 계좌 조회와 `account-before-order.json` 작성
- 모든 sub-agent 출력과 저장 아티팩트 sanitize
- 수집 스냅샷 병합과 종목 제외 판정
- `decision-brief.json` 생성
- 평결 결과 조정과 주문 후보 생성
- 명시적 주문 승인 여부 확인
- 기존 active 미체결·예약 주문의 유지·취소·정정·대체 판단과 실행
- 실행 gate를 통과한 주문 후보의 실제 주문 제출

## Sub-Agent

| Agent | 역할 | launcher model | launcher effort |
|---|---|---|---|
| `collect-financial-information` | KIS quotation/financial/estimate API 기반 재무 YAML 캐시 경로 | `gpt-5.4-mini` | `low` |
| `collect-news-information` | KIS 뉴스 YAML 캐시 경로/요약 | `gpt-5.4-mini` | `low` |
| `get-market-status` | Google Finance/KIS 기반 S&P 500, Nasdaq, Dow, 코스피, 코스닥 상태와 등락률 요약 | `gpt-5.4-mini` | `low` |
| selected 4 personas | `first-verdict` 독립 종목 점수 | `gpt-5.4-mini` | `medium` |
| `judge-midterm` | `second-verdict` 포트폴리오 목표수량 | `gpt-5.5` | `low` |

## API 권한

`Main agent` 계좌 조회 허용 범위:

- 계좌 자산 요약
- 잔고와 보유수량
- 당일 체결
- 미체결 주문
- 예약 주문
- 매수가능 조회
- 매도가능 조회

`Main agent` 주문 실행 허용 범위:

- 명시 승인과 `references/rules/trade-execution.md` gate를 통과한 `order_cash`
- 명시 승인과 `references/rules/trade-execution.md` gate를 통과한 `order_resv`
- 명시 승인과 현재 KIS/MCP API detail 또는 검증된 template이 있는 기존 active 미체결·예약 주문 취소·정정·대체

가격·그래프 수집 허용 범위:

- `Main agent`가 대상 종목에 대해 `kis-trade-mcp` 현재가와 일/주/월 그래프 조회를 직접 수행
- 결과는 `reports/runs/<run_id>/price-chart.json`에 저장

Financial 수집 허용 범위:

- KIS quotation/financial/estimate API

News 수집 허용 범위:

- KIS 뉴스 및 KIS 공시 계열 API만 허용

Market status 수집 허용 범위:

- `$get-market-status`가 정의한 S&P 500, Nasdaq, Dow, 코스피, 코스닥 지수 상태·등락률·짧은 의견
- 미국 지수는 `$get-market-status`의 Google Finance 기준을 따른다.
- 결과는 launcher `parsed_text`로만 전달하고, 별도 `reports/runs/<run_id>/market-status.md` 파일을 쓰지 않는다.

모든 sub-agent 금지 범위:

- 주문 제출
- 예약 주문 제출
- 정정
- 취소
- canonical 아티팩트 쓰기
- Markdown sidecar 쓰기
- diff 또는 code fence 출력
- 민감정보 반환

`first-verdict`, `second-verdict` agent 금지 범위:

- KIS, MCP, web, network, 외부 출처 호출
- 명시된 artifact/persona/rule 파일을 `cat`, `jq`로 read-only 조회하는 경우를 제외한 shell 사용
- unlisted 파일 읽기
- 파일 쓰기
- 재수집
- 주문 API
- raw prompt fallback

`Main agent`가 평결 JSON에서 생성하는 companion Markdown은 사람이 각 자산 판단을 확인하기 위한 보조 산출물이다. 파일명은 `references/rules/verdict-format.md`의 safe-name 규칙을 따른다. 이 Markdown은 점수 집계, 목표수량 조정, 주문 후보 계산, 실행 gate의 입력으로 쓰지 않는다.

## 아티팩트

Run 아티팩트는 `reports/runs/<run_id>/` 아래에 둔다.

- `run.json`
- `price-chart.json`
- financial memory 경로 `memory/collect-financial-information/financial-YYYY-MM-DD.yaml` (optional best-effort cache path)
- 뉴스 memory 경로 `memory/collect-news-information/news-YYYY-MM-DD.yaml` (optional best-effort cache path)
- `decision-brief.json`
- `verdict-inputs/*.verdict-core.json` / `verdict-inputs/*.verdict-first-slice.json` (launcher-created non-canonical lossless slices)
- `verdict-first.json`
- `verdict-second.json`
- `verdicts/<stage>--<agent_role>--<task_name>.md` (verdict agent별 사람 확인용 companion Markdown)
- `account-before-order.json`
- `execution.json`

## 평결 입력

`decision-brief.json`은 Main agent가 수집 결과를 합쳐 만든 canonical verdict input이다. Sub-agent에는 launcher가 `decision-brief.json`에서 파생한 lossless `verdict-core` slice를 전달한다. `second-verdict`에는 `verdict-first.json` 전체가 아니라 selected-symbol first-verdict slice를 전달하고, 보유/가격 정보는 함께 전달되는 `verdict-core`에서 읽는다.

`decision-brief.json`에는 종목 식별자, eligibility, `evidence_mode`, 가격, 핵심 price/chart signal, 가능한 financial memory 경로와 짧은 financial summary, 가능한 뉴스 memory 경로와 짧은 KIS 뉴스/공시 요약, 가능한 market-status 요약, 계좌 노출, 누락/오류 사유를 압축해서 담는다. financial/news memory 파일과 market-status launcher text는 사람이 읽는 참고자료이다. Main agent는 그중 짧은 bullet만 선별해 `decision-brief.json`에 복사한다. `reports/runs/<run_id>/financial.md`, `reports/runs/<run_id>/news.md`, `reports/runs/<run_id>/market-status.md`는 만들지 않는다. 긴 raw API payload, 기사 원문, 반복 source detail, 민감정보는 제외한다. 종목별 price/chart signal은 최대 5개, financial summary는 최대 3개 bullet, KIS 뉴스/공시는 최대 3개 item, market-status는 최대 5개 run-level bullet, warning/error는 최대 5개로 제한하고 반복되는 domain-wide 누락 사유는 한 번만 요약한다.

재무/뉴스가 없더라도 식별자, 종목명, 현재가 또는 직전 거래일 가격 snapshot, 관측시각이 있으면 `eligible_for_verdict=true`로 유지한다.

같은 날짜 full-universe financial/news cache hit이면 해당 collector를 실행하지 않는다. Cache miss 또는 universe mismatch일 때만 collector를 실행하고, 그 사유를 run artifact에 남긴다.

`first-verdict`는 4개 persona를 유지한다. Main agent가 가격, 보유, 뉴스, active 주문 상태의 안정성을 증명할 수 있는 unchanged symbol만 직전 유효 run의 verdict row를 병합할 수 있다. 증명할 수 없으면 재평가하고, sell/stop-loss 후보, 당일 체결, 가격 급변, 신규 뉴스, active 주문, score boundary 근처 종목은 반드시 재평가한다. Launcher 자동 wrapper 재사용은 같은 spec fingerprint에 한정된다.

종목이 eligible이고 가격 관측값과 목표수량, 계좌 제약, 주문유형을 통과하면 재무/뉴스 누락·partial·failed·no-data는 `order_cash`, `order_resv`, demo, real 제출을 단독으로 차단하지 않는다.

## 주문 경계

`Main agent`는 `references/rules/trade-execution.md`의 실행 gate를 직접 적용한다. 별도 최종 리스크 sub-agent와 승인 아티팩트는 사용하지 않는다.

`expected_holding_quantity`는 현재 후보 주문 제출 전, 이미 존재하는 미체결·예약 수량만 반영한 예상 보유수량이다. `target_holding_quantity`와 다르다는 이유만으로 후보 불일치나 주문 차단으로 판단하지 않는다.

기존 active 미체결·예약 주문이 목표수량, 방향, 잔여수량, 가격, 주문유형, 예약/즉시 경로와 맞지 않으면 `Main agent`는 새 주문을 내기 전에 유지·취소·정정·대체 중 하나로 조정한다. 조정 API를 안전하게 확인할 수 없거나 조정 결과가 불확실하면 같은 종목의 대체 주문은 제출하지 않고 `execution.json`에 `blocked`로 남긴다.

사용자 또는 schedule이 demo 또는 real 실행을 명시했고 `references/rules/trade-execution.md`의 모든 실행 gate를 통과한 경우에만 `Main agent`가 주문을 제출한다.
