# daily-trading

`daily-trading`은 한국 주식·ETF 포트폴리오 전체를 대상으로 수집, 평결, 주문 실행 경계를 정의하는 프롬프트 계약형 스킬이다.

Sub-agent 모델과 effort는 `scripts/run_subagent.py`가 `codex exec` 명령으로 지정한다.

`Main agent`는 routine 실행에서 `scripts/run_daily_trading_pipeline.py run`을 먼저 호출한다. Telegram/user-facing 응답은 `pipeline-summary.json`을 직접 말로 재구성하지 않고, `scripts/render_telegram_summary.py`가 `pipeline-summary.json`에서 생성한 `telegram-summary.txt`를 그대로 사용한다. `pipeline-summary.json`은 `verdict_summary`, `account_display_summary`, `evidence_summary`, `telegram_response_policy`, `report_path`, `telegram_summary_path`를 포함하므로 진단이 필요할 때만 읽는다. 명시적으로 승인된 `demo-submit` 또는 `real-submit`에서는 `--submit-orders`를 함께 넘겨 `scripts/execute_orders.py`가 read-only gate 갱신, 기존 pending/reserved 주문 조정 판정, 즉시/예약 주문 제출·정정·취소·차단, 최종 summary 재생성을 수행하게 한다. `--order-path auto`는 KST 기준 `09:00 <= t < 15:30` 평일 실행을 `order_cash`, `15:40 <= t` 또는 `t < 07:30` 실행과 주말 실행을 `order_resv`로 해석하는 기본값이다. 휴장일 판단과 스케줄 활성화는 daily-trading 내부가 아니라 별도 check-holiday 경계에서 처리한다. `--order-path reservation`은 `order_resv`, `--order-path immediate`는 `order_cash` 후보로 명시 고정한다. `--submit-orders` 없이 `execution.requires_main_agent_order_execution=true`가 남아 있으면 그 run은 비제출 gate 요약 상태이며 최종 주문 실행 결과가 아니다. 명시적 지정가 예약 요청에서 사용자별 종목 가격이 없으면 `execution-plan`의 `order_price`를 기본 지정가 후보로 사용하며, 해당 가격이 파이프라인에서 산출됐다는 이유만으로 차단하지 않는다. `scripts/run_subagent.py`, `scripts/build_run_artifacts.py`, `scripts/render_telegram_summary.py`, persona 파일, rule 문서 전문, 중간 JSON은 pipeline 실패 진단 때만 연다. 설치 또는 pipeline/launcher/helper 변경 후에는 `python3 <daily-trading-skill>/scripts/run_daily_trading_pipeline.py self-test`, `python3 <daily-trading-skill>/scripts/run_subagent.py self-test`, `python3 <daily-trading-skill>/scripts/build_run_artifacts.py self-test`, `python3 <daily-trading-skill>/scripts/execute_orders.py self-test`, `python3 <daily-trading-skill>/scripts/render_telegram_summary.py --self-test`로 검증한다.

Routine command:

```text
python3 <daily-trading-skill>/scripts/run_daily_trading_pipeline.py run \
  --workspace-dir <workspace> \
  --output-dir reports/runs/<run_id> \
  --run-id <run_id> \
  --started-at <started_at> \
  --env <acct|paper> \
  --request-type <analysis|prepare|demo-submit|real-submit> \
  [--submit-orders] \
  [--order-path <auto|reservation|immediate>] \
  [--main-events <codex-json-events-path>]
```

이 명령은 helper/launcher의 큰 stdout을 `pipeline-command-log.json`에 저장하고 stdout에는 compact summary pointer만 출력한다.

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
| 1 | `Main agent` + `scripts/run_daily_trading_pipeline.py` | `$check-portfolio`(요청 시), KIS direct read-only holdings API auto-auth | 사용자 요청, portfolio 설정, `run_id`, `started_at`, `CODEX_MCP_TRADING_ENV` | `run.json`, `check-portfolio.json`, `pipeline-summary.json`, `reports/YYYY-MM-DD_포트폴리오.md` | Main agent는 pipeline을 먼저 실행하고 summary만 우선 읽음 |
| 2 | `Main agent` | `$check-portfolio` JSON | check-portfolio `universe`, 거래 환경 | 전체 종목 universe | universe 확장을 위해 현재 보유 종목을 별도 재조회하지 않음 |
| 3 | `Main agent` + Collection sub-agents | `scripts/collect_main_evidence.py` direct KIS 가격·계좌 증거 수집, cache miss/universe mismatch 시 `get` 확인 후 1회 `$collect-financial-information`, `$collect-news-information` 및 재 `get` | 전체 종목 universe, 거래 환경 | `price-chart.json`, `account-before-order.json`, 선택적 `collection-summary.json`, 선택적 full-universe 또는 partial financial/news memory 경로 | 가격·관측시각은 필수, financial/news는 같은 날짜 full-universe cache hit 시 collector를 생략하며, cache miss/universe mismatch면 get→collect→get을 한 번만 수행하고 그래도 미완성이면 partial cache를 사용함 |
| 4 | `Main agent` + `scripts/build_run_artifacts.py` | deterministic 병합/sanitize | `price-chart.json`, `$check-portfolio` JSON, 선택적 `memory/collect-financial-information/financial-YYYY-MM-DD.yaml`, 선택적 `memory/collect-news-information/news-YYYY-MM-DD.yaml` | `decision-brief.json`, 제외 종목 목록 | 식별자와 가격 snapshot이 있으면 재무/뉴스 누락만으로 제외하지 않음; Main agent가 직접 JSON을 조립하지 않고 helper를 호출 |
| 5 | `first-verdict` sub-agents + `scripts/build_run_artifacts.py` | selected 3 functional personas, deterministic spec/merge | launcher-created `verdict-core`, `verdict-format.md` | `verdict-first.json`, `verdicts/first-verdict--<agent_role>--<task_name>.md` | 외부 호출·다른 agent 결과 참조 금지; sub-agent는 compact JSON만 반환하고 companion MD와 score merge는 helper가 생성 |
| 6 | `second-verdict` sub-agent + `scripts/build_run_artifacts.py` | `judge-final`, deterministic 대상/spec 생성 | launcher-created `verdict-core`, selected-symbol first-verdict slice, `verdict-format.md` | `verdict-second.json`, `verdicts/second-verdict--judge-final--<task_name>.md` | 목표수량은 단일 judge가 제안하고 목표현금은 만들지 않으며, helper/Main agent가 schema·자산·집중도·계좌 gate만 검증; 실패 시 failed task만 최대 2회 retry |
| 7 | `scripts/build_run_artifacts.py` + `scripts/execute_orders.py` | deterministic 주문 계산, KIS read-only pending/reserved/주문가능 조회, KIS `order_cash`/`order_resv`/정정취소 API | `verdict-second.json`, 최신 계좌 상태, 명시적 demo/real 실행 요청 | `account-before-order.json`, `execution.json`, `order-execution-log.json` | helper가 비제출 주문 수학/gate 요약을 먼저 만들고, 명시 실행 요청에서 `--submit-orders`가 있으면 `execute_orders.py`가 최신 계좌 gate를 갱신한 뒤 즉시/예약 주문을 제출·정정·취소하거나 명확히 차단한다. 명시적 지정가 예약 요청에서는 `execution-plan`의 `order_price`를 기본 지정가 후보로 인정한다 |
| 8 | `scripts/run_daily_trading_pipeline.py summarize` + `scripts/render_telegram_summary.py` | report template, Telegram fixed template, run artifact update | 최종 `execution.json`, `run.json`, `verdict-second.json`, `pipeline-summary.json` | 최종 `pipeline-summary.json`, `telegram-summary.txt`, `reports/YYYY-MM-DD_포트폴리오.md`, 최종 `run.json` | partial/failed artifact를 삭제하지 않음; Telegram 응답은 `telegram-summary.txt`를 그대로 사용 |

## Main agent 책임

`Main agent`만 아래 작업을 수행할 수 있다.

- stage와 sub-agent 오케스트레이션
- `reports/` 아티팩트 생성과 갱신
- KIS 인증 경계 처리와 direct main-evidence helper 실행
- `$check-portfolio` JSON universe 사용, read-only 계좌 조회와 `account-before-order.json` 작성
- 모든 sub-agent 출력과 저장 아티팩트 sanitize
- 수집 스냅샷 병합과 종목 제외 판정
- `decision-brief.json` 생성
- 평결 결과 조정과 주문 후보 생성
- 명시적 주문 승인 여부 확인
- 명시 submit run에서 `scripts/execute_orders.py` 실행과 실패 진단
- Telegram/user-facing 응답은 `telegram-summary.txt`를 그대로 전달하고, 임의 재요약하지 않음

## Sub-Agent

| Agent | 역할 | launcher model | launcher effort |
|---|---|---|---|
| `collect-financial-information` | KIS quotation/financial/estimate API 기반 재무 YAML 캐시 경로 | `gpt-5.4-mini` | `low` |
| `collect-news-information` | KIS 뉴스 YAML 캐시 경로/요약 | `gpt-5.4-mini` | `low` |
| selected 3 functional personas | `first-verdict` 독립 종목 점수 | `gpt-5.5` | `medium` |
| `judge-final` | `second-verdict` 포트폴리오 목표수량 | `gpt-5.5` | `medium` |

## API 권한

`Main agent` 계좌 조회 허용 범위:

- 계좌 자산 요약
- 잔고와 보유수량
- 당일 체결
- 미체결 주문
- 예약 주문
- 매수가능 조회
- 매도가능 조회는 검증된 direct template이 있을 때만 사용하고, 현재 runner는 현재 보유수량에서 active 매도 예약을 뺀 값을 매도 gate로 사용

`scripts/execute_orders.py` 주문 실행 허용 범위:

- 명시 승인과 `references/rules/trade-execution.md` gate를 통과한 `order_resv`
- 명시 승인과 `references/rules/trade-execution.md` gate를 통과한 `order_cash`
- 필수 원주문 식별자가 있는 기존 active pending/reserved 주문의 정정·취소·대체 제출

가격·계좌 증거 수집 허용 범위:

- `Main agent`가 `scripts/collect_main_evidence.py`를 실행해 대상 종목의 direct KIS 현재가와 sanitized 계좌 스냅샷을 수집
- 결과는 `reports/runs/<run_id>/price-chart.json`, `account-before-order.json`, 선택적 `collection-summary.json`에 저장
- helper는 주문 제출, 예약, 정정, 취소를 수행하지 않으며, active 주문/주문가능 조회가 누락된 경우 실제 주문 단계는 `references/rules/trade-execution.md` gate에 따라 차단한다.

Financial 수집 허용 범위:

- KIS quotation/financial/estimate API

News 수집 허용 범위:

- KIS 뉴스 및 KIS 공시 계열 API만 허용

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
- `collection-summary.json` (optional direct main-evidence helper summary)
- financial memory 경로 `memory/collect-financial-information/financial-YYYY-MM-DD.yaml` (optional best-effort cache path)
- 뉴스 memory 경로 `memory/collect-news-information/news-YYYY-MM-DD.yaml` (optional best-effort cache path)
- `decision-brief.json`
- `verdict-inputs/*.verdict-core.json` / `verdict-inputs/*.verdict-first-slice.json` (launcher-created non-canonical lossless slices)
- `verdict-first.json`
- `verdict-second.json`
- `verdicts/<stage>--<agent_role>--<task_name>.md` (verdict agent별 사람 확인용 companion Markdown)
- `account-before-order.json`
- `execution.json`
- `pipeline-summary.json` (`verdict_summary`, `report_path` 포함)

사람이 읽는 최종 포트폴리오 보고서는 run directory 밖의 `reports/YYYY-MM-DD_포트폴리오.md`에 둔다.

## 평결 입력

`decision-brief.json`은 Main agent가 수집 결과를 합쳐 만든 canonical verdict input이다. Sub-agent에는 launcher가 `decision-brief.json`에서 파생한 lossless `verdict-core` slice를 전달한다. `second-verdict`에는 `verdict-first.json` 전체가 아니라 selected-symbol first-verdict slice를 전달하고, 보유/가격 정보는 함께 전달되는 `verdict-core`에서 읽는다.

`decision-brief.json`에는 종목 식별자, eligibility, `evidence_mode`, 가격, 핵심 price/chart signal, compact 기간봉/분봉·호가·체결·수급 요약, 가능한 financial memory 경로와 짧은 financial/ETF summary, 가능한 뉴스 memory 경로와 짧은 KIS 뉴스/공시 요약, 계좌 노출, 누락/오류 사유를 압축해서 담는다. financial/news memory 파일은 사람이 읽는 참고자료이다. Main agent는 그중 짧은 bullet만 선별해 `decision-brief.json`에 복사한다. `reports/runs/<run_id>/financial.md`, `reports/runs/<run_id>/news.md`는 만들지 않는다. 긴 raw API payload, 기사 원문, 반복 source detail, 민감정보는 제외한다. 종목별 price/chart signal은 최대 12개, financial summary는 최대 3개 bullet, KIS 뉴스/공시는 최대 3개 item, warning/error는 최대 5개로 제한하고 반복되는 domain-wide 누락 사유는 한 번만 요약한다.

재무/뉴스가 없더라도 식별자, 종목명, 현재가 또는 직전 거래일 가격 snapshot, 관측시각이 있으면 `eligible_for_verdict=true`로 유지한다.

같은 날짜 financial/news cache는 top-level `symbols` 키가 전체 universe를 덮으면 그대로 사용한다. Cache miss 또는 universe mismatch면 helper `get`을 확인한 뒤 collector를 한 번 실행하고, 다시 `get`으로 받은 cache가 전체 universe를 덮지 못하면 존재하는 partial cache를 그대로 사용한다. 수집 실패 후 추가 재시도는 하지 않으며, 캐시가 없으면 해당 optional domain 없이 진행한다.

`first-verdict`는 3개 기능형 persona(`analyst-quality-value`, `analyst-momentum-cycle`, `analyst-risk-allocation`)를 유지한다. Main agent가 가격, 보유, 뉴스, active 주문 상태의 안정성을 증명할 수 있는 unchanged symbol만 직전 유효 run의 verdict row를 병합할 수 있다. 증명할 수 없으면 재평가하고, sell/stop-loss 후보, 당일 체결, 가격 급변, 신규 뉴스, active 주문, score boundary 근처 종목은 반드시 재평가한다. Launcher 자동 wrapper 재사용은 같은 spec fingerprint에 한정된다.

종목이 eligible이고 가격 관측값과 목표수량, 계좌 제약, 주문 API/경로를 통과하면 재무/뉴스 누락·partial·failed·no-data는 `order_cash`/`order_resv` demo/real 제출을 단독으로 차단하지 않는다.

## 주문 경계

`scripts/execute_orders.py`는 `references/rules/trade-execution.md`의 실행 gate를 적용한다. 별도 최종 리스크 sub-agent와 승인 아티팩트는 사용하지 않는다.

`expected_holding_quantity`는 현재 후보 주문 제출 전, 이미 존재하는 미체결·예약 수량만 반영한 예상 보유수량이다. `target_holding_quantity`와 다르다는 이유만으로 후보 불일치나 주문 차단으로 판단하지 않는다.

기존 active pending/reserved 주문이 목표수량, 방향, 잔여수량, 가격, 주문 API, 주문 경로와 맞지 않으면 `scripts/execute_orders.py`는 필수 원주문 식별자가 있을 때 같은 방향/API/경로 주문은 정정하고, 취소가 필요한 대체 주문은 취소 요청만 제출한 뒤 재조회가 필요하다는 `blocked` 결과로 남긴다. 식별자가 없거나 조정 결과가 불확실하면 `execution.json`에 `blocked`로 남긴다.

사용자 또는 schedule이 demo 또는 real 실행을 명시했고 `--submit-orders`가 전달됐으며 `references/rules/trade-execution.md`의 모든 실행 gate를 통과한 경우에만 `scripts/execute_orders.py`가 주문 API를 호출한다.
