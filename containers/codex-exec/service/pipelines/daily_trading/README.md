# daily-trading

`daily-trading`은 한국 주식·ETF 포트폴리오 전체를 대상으로 수집, 평결, 주문 실행을 수행하는 Python pipeline package다. Codex skill entrypoint로 노출하지 않는다.

Sub-agent 모델과 effort는 `CODEX_RUNTIME_CONFIG_FILE`이 가리키는 `codex-runtime.yaml`의 `daily_trading`에서 조정하고, `scripts/run_subagent.py`가 실행 직전에 해당 파일을 읽어 `codex exec` 명령에 지정한다. 모델과 effort는 비어 있지 않은 문자열인지만 확인하며, 모델별 지원 여부는 `codex exec`가 판단한다.

Scheduled daily-trading jobs with a `daily_trading` block in `schedules.yaml` are executed by the codex-exec Python direct runner, which calls `scripts/run_daily_trading_pipeline.py run` without starting Main Codex. Telegram/user-facing 응답은 `pipeline-summary.json`을 직접 말로 재구성하지 않고, `scripts/render_telegram_summary.py`가 만든 짧은 `telegram-summary.txt`를 본문으로 전송한 뒤 `scripts/render_html_report.py`가 만든 `daily-trading-report.html`을 첨부한다. HTML은 해당 run 시각까지 같은 날짜의 run을 누적하여 거래·체결, 전체 Analyst 대상, Judge 단계별 판단, 재무·시간별 뉴스, 계좌·KOSPI 추이와 보유현황을 한 파일의 탭 UI로 제공한다. `today-fills.json`은 계좌 전체 일별 체결을 보존하고 HTML은 당일 run들의 체결을 주문번호 기준으로 중복 제거하며, 수집 실패나 과거 universe 범위 artifact는 당일 전체 체결로 단정하지 않고 상태를 표시한다. 확인되지 않은 원금이나 계좌수익률은 추정하지 않으며, HTML 생성 실패는 완료된 거래를 실패로 바꾸지 않고 `html-report` non-required stage와 `html_report_available=false`로 남긴다. `pipeline-summary.json`은 `review_summary`, `account_display_summary`, `today_fills_summary`, `evidence_summary`, `telegram_response_policy`, `report_path`, `telegram_summary_path`, `html_report_path`를 포함하므로 진단이 필요할 때만 읽는다. `telegram-summary.txt`의 매수/매도/유지 카운트는 `review_summary.final_sell_count/final_buy_count/final_hold_count`로, 각 종목의 현재 보유수량→최종 보유수량 방향에서 도출한 최종 결정이며 미해결·무효 Judge 후보는 유지로 합산하지 않고 `unresolved_candidate_count`로 분리한다(있을 때 `미결`로 표시). `sell_candidate_count/buy_candidate_count`는 Judge 매도/매수 후보 카운트, `hold_symbol_count`는 `scored_count - len(candidate_directions)`로 산출한 "candidate_directions에 아예 선정되지 않은 scored 종목 수"이며 Judge의 보유(유지) 판단이나 최종 평결이 아니다. 셋 다 최종 평결이 아니며, 최종 카운트가 없는 구버전 summary를 렌더할 때만 `Judge 검토: 매도 후보 · 매수 후보 · 미선정`으로 표기한다. `당일 누계`(매수/매도 금액·체결 건수)는 `account-before-order.json`과 수집 시점 `today-fills.json`에서 온 이번 run 주문 전 스냅샷이므로 `당일 누계(이번 run 주문 전 기준)`으로 표기하고 텔레그램 전송 시점 값으로 오인하지 않는다. 명시적으로 승인된 `demo-submit` 또는 `real-submit`에서는 `--submit-orders`를 함께 넘겨 `scripts/execute_orders.py`가 read-only gate 갱신, 기존 pending/reserved 주문 조정 판정, 즉시/예약 주문 제출·정정·취소·차단, 최종 summary 재생성을 수행하게 한다. `--order-path auto`는 KST 기준 `09:00 <= t < 15:30` 평일 실행을 `order_cash`, `15:40 <= t` 또는 `t < 07:30` 실행과 주말 실행을 `order_resv`로 해석하는 기본값이다. 휴장일 판단과 스케줄 활성화는 daily-trading 내부가 아니라 별도 check-holiday 경계에서 처리한다. `--order-path reservation`은 `order_resv`, `--order-path immediate`는 `order_cash` 후보로 명시 고정한다. `--submit-orders` 없이 `execution.requires_main_agent_order_execution=true`가 남아 있으면 그 run은 비제출 gate 요약 상태이며 최종 주문 실행 결과가 아니다. 명시적 지정가 예약 요청에서 사용자별 종목 가격이 없으면 `execution-plan`의 `order_price`를 기본 지정가 후보로 사용하며, 해당 가격이 파이프라인에서 산출됐다는 이유만으로 차단하지 않는다. `scripts/run_subagent.py`, `scripts/build_run_artifacts.py`, `scripts/render_telegram_summary.py`, `scripts/render_html_report.py`, `prompts/*.md`, 중간 JSON은 pipeline 실패 진단 때만 연다. 설치 또는 pipeline/launcher/helper 변경 후에는 `python3 <daily-trading-pipeline>/scripts/run_daily_trading_pipeline.py self-test`, `python3 <daily-trading-pipeline>/scripts/run_subagent.py self-test`, `python3 <daily-trading-pipeline>/scripts/build_run_artifacts.py self-test`, `python3 <daily-trading-pipeline>/scripts/execute_orders.py self-test`, `python3 <daily-trading-pipeline>/scripts/render_telegram_summary.py --self-test`, `python3 <daily-trading-pipeline>/scripts/render_html_report.py --self-test`로 검증한다. 각 `self-test` 명령은 기존 CLI 호환 진입점이고 실제 fixture와 검증 구현은 `tests/test_*.py`에 둔다.

Telegram으로 전송하는 HTML 문서명은 각 실행을 구분할 수 있도록 `daily-trading-report-<run_id>.html`을 사용하며, run 디렉터리 안의 원본 artifact 이름은 `daily-trading-report.html`로 유지한다.

명시적 주문 run은 Judge 전에 `order-lifecycle.json`을 생성해 같은 날 이전 제출 주문의 최신 KIS 상태와 현재 pending/reserved 주문을 복원한다. 확인 체결량이 계좌·당일체결 스냅샷보다 앞선 종목은 수량 상태가 일치할 때까지 주문을 차단하고, 이번 Judge 대상에서 빠진 이전 active 주문은 현재 수량을 목표로 한 정리 전용 실행 행으로 취소한다. `pipeline-summary.json`의 `order_lifecycle`과 Telegram의 `사전 주문상태`가 이 결과를 요약한다.

Repository checkout에서 daily-trading tests만 실행하려면 `PYTHONPATH=containers/codex-exec python3 -m unittest discover -s containers/codex-exec/service/pipelines/daily_trading/tests -t containers/codex-exec -p 'test_*.py'`로 실행한다. 저장소 전체 회귀(telegram-gateway, 하이픈 스킬 포함)는 repo root의 `python3 scripts/run_tests.py`로 실행한다.

Routine command:

```text
python3 <daily-trading-pipeline>/scripts/run_daily_trading_pipeline.py run \
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
| 1차 독립 종목 평결 단계 | `analyst-review` |
| 최종 평결 전 낙관/비관 토론 단계 | `judge-debate` |
| 2차 포트폴리오 최종 보유수량 평결 단계 | `judge-review` |
| canonical 평결 입력 | `decision-brief.json` |
| sub-agent 평결 입력 | launcher-created selected-symbol slices; analyst-review는 output view profile 기반 role-scoped slice |

문장 설명은 한국어로 쓰되, stage 이름, 파일명, JSON enum 값은 위 표준 표기를 그대로 사용한다.

## 전체 동작 Flow

| 단계 | 주체 | 사용하는 skill / sub-agent | 주요 입력 | 주요 출력 | 핵심 gate |
|---:|---|---|---|---|---|
| 1 | scheduled direct runner 또는 `Main agent` + `scripts/run_daily_trading_pipeline.py` | `$check-portfolio`(요청 시), KIS direct read-only holdings API auto-auth | structured schedule config 또는 사용자 요청, portfolio 설정, `run_id`, `started_at`, `CODEX_MCP_TRADING_ENV` | `run.json`, `check-portfolio.json`, `pipeline-summary.json`, `telegram-summary.txt`, `daily-trading-report.html`, `reports/YYYY-MM-DD_포트폴리오.md` | scheduled direct runner는 Main Codex 없이 pipeline을 실행하고 짧은 Telegram 본문과 HTML 첨부를 전송함; manual/fallback Main agent는 pipeline을 먼저 실행하고 summary만 우선 읽음 |
| 2 | `Main agent` | `$check-portfolio` JSON | check-portfolio `universe`, 거래 환경 | 전체 종목 universe | universe 확장을 위해 현재 보유 종목을 별도 재조회하지 않음 |
| 3 | `Main agent` + deterministic helpers + financial collection sub-agent | `scripts/collect_main_evidence.py` direct KIS 가격·계좌·장중 외인기관 추정 수급 수집, cache miss/universe mismatch 시 1회 `$collect-financial-information`, deterministic `symbol_news` KIS 수집, 저장된 `market_news` DB를 읽는 `news_context`, deterministic `market_index_snapshot` 수집 | 전체 종목 universe, 거래 환경 | `price-chart.json`, `account-before-order.json`, 선택적 `account-asset-snapshot.json`, financial cache, `memory/symbol-news-cache/symbol-news-YYYY-MM-DD.yaml`, `news-context.json`, 선택적 `market-index-snapshot.json` | 가격·관측시각은 필수다. `symbol_news`와 `market_news`는 서로 다른 수집 계약이며, `news_context`가 직전 거래 run 이후 구간을 선택하고 두 범위의 중복을 제거한다. optional 뉴스·재무·지수 실패는 단독 주문 차단 사유가 아니다. |
| 4 | `Main agent` + `scripts/build_run_artifacts.py` | deterministic 병합/sanitize | `price-chart.json`, `$check-portfolio` JSON, 선택적 financial cache, 선택적 `symbol_news` cache, 선택적 `news-context.json`, 선택적 `market-index-snapshot.json` | `decision-brief.json`, 제외 종목 목록 | 종목별 `symbol_news_summary`와 top-level `market_news_context`를 분리한다. 식별자와 가격 snapshot이 있으면 optional 근거 누락만으로 제외하지 않는다. |
| 5 | `analyst-review` sub-agents + `scripts/build_run_artifacts.py` | selected 2 execution personas, deterministic spec/merge into 4 canonical views | launcher-created role-scoped `review-core`, `analyst-review-format.md` | `analyst-review.json`, `reviews/analyst-review--<agent_role>--<task_name>.md` | `analyst-quality-risk`는 `analyst-quality-value`와 `analyst-risk-allocation` view를 독립 산출하고, `analyst-momentum-news`는 `analyst-momentum-cycle`과 `analyst-news-flow` view를 독립 산출; sub-agent는 compact JSON만 반환하고 companion MD와 score merge는 helper가 생성 |
| 6 | `scripts/run_daily_trading_pipeline.py` + persistent Bull/Bear sessions + `judge-review` | 점수 밴드로 후보를 고른 뒤 Bull/Bear opening과 같은 session의 `rebuttal-1`을 실행하고 단일 judge 최종 단계로 넘김 | launcher-created selected-symbol slices, `debate-format.md`, `debate-bull.md`/`debate-bear.md`, 정규화된 `judge-debate.json`, `judge-review-format.md` | `debate/*.json`, compact opponent opening, attempt별 wrapper/raw/events, `judge-debate.json`, `judge-review.json`, judge sidecar | `rebuttal-1`은 session이 기억하는 자기 opening을 다시 읽지 않고 압축된 상대 opening만 입력한다. 각 phase는 Bull/Bear `wait_all` barrier이며 성공한 쪽은 재실행하지 않고 실패한 쪽만 같은 session으로 1회 retry한다. judge는 sub-agent를 생성하지 않으며 토론 불완전·누락 또는 논거 불충분 시 기준 노출 유지 |
| 7 | `scripts/build_run_artifacts.py` + `scripts/execute_orders.py` | deterministic 주문 계산, KIS read-only pending/reserved/주문가능/당일주문체결 조회, KIS `order_cash`/`order_resv`/정정취소 API | `judge-review.json`, 최신 계좌 상태, 명시적 demo/real 실행 요청 | `order-lifecycle.json`, `account-before-order.json`, `execution.json`, `order-execution-log.json` | 명시 주문 run은 Judge 전에 이전 제출 주문과 현재 active 주문을 조회한다. helper가 주문 수학/gate 요약을 만들고, `--submit-orders`가 있으면 `execute_orders.py`가 실행 직전 gate를 다시 갱신한 뒤 즉시/예약 주문을 제출·정정·취소하거나 차단한다. 즉시주문 제출 후에는 KIS 당일주문체결 조회로 `filled/pending/rejected/canceled/unconfirmed` 상태를 기록하고, 체결 외 결과는 실행을 `partial`로 유지한다. 명시적 지정가 예약 요청에서는 `execution-plan`의 `order_price`를 기본 지정가 후보로 인정한다 |
| 8 | `scripts/run_daily_trading_pipeline.py summarize` + `scripts/render_telegram_summary.py` | report template, Telegram fixed template, run artifact update | 최종 `execution.json`, `run.json`, `judge-review.json`, `pipeline-summary.json` | 최종 `pipeline-summary.json`, `telegram-summary.txt`, `reports/YYYY-MM-DD_포트폴리오.md`, 최종 `run.json` | partial/failed artifact를 삭제하지 않음; Telegram 응답은 `telegram-summary.txt`를 그대로 사용 |

`account-before-order.status`와 `pipeline-summary.account_collection_status`는 잔고·보유수량 수집 상태만 나타낸다. 미체결·주문가능 조회의 실행 상태는 `order_gate_status=not_run|success|failed|not_required`로 별도 기록하며, 게이트 미실행만으로 계좌 수집을 `partial`로 낮추지 않는다. `pipeline-summary.evidence_summary.investor_flow`는 장중 추정 수급의 사용 가능 종목 수와 누락 종목 수를 별도로 제공한다.

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
| `collect-financial-information` | KIS quotation/financial/estimate API 기반 재무 YAML 캐시 경로 | `gpt-5.6-luna` | `low` |
| selected 2 analyst-review execution personas | `analyst-review` 독립 종목 점수 (`analyst-quality-risk`와 `analyst-momentum-news`가 각각 두 view 산출) | `gpt-5.6-sol` | `xhigh` |
| `debate-bull`, `debate-bear` | `judge-debate` 낙관/비관 opening 및 동일 session의 1·2차 반박 | `gpt-5.6-sol` | `xhigh` |
| `judge` | `judge-review` 포트폴리오 목표금액 | `gpt-5.6-sol` | `xhigh` |

## API 권한

`Main agent` 계좌 조회 허용 범위:

- 계좌 자산 요약
- 잔고와 보유수량
- 당일 체결
- 미체결 주문
- 예약 주문
- 매수가능 조회
- 매도가능 조회는 검증된 direct template이 있을 때만 사용하고, 현재 runner는 현재 보유수량에서 active 매도 예약을 뺀 값을 매도 gate로 사용

당일 체결과 최근 제출 거래 이력은 빈 목록만으로 `거래 없음`으로 확정하지 않는다. 당일 체결은 `collection_status`, 최근 제출 거래는 `coverage_status`가 `complete`일 때만 빈 목록을 확인된 부재로 해석하며, `partial`/`unavailable`은 미확인 상태로 유지한다. Judge가 당일 매수 이력이 미확인인 상황에서 기준 목표금액을 늘리려면 새 근거나 실질적으로 바뀐 가격·포트폴리오 맥락을 `additional_buy_reason`에 제시해야 한다.

`scripts/execute_orders.py` 주문 실행 허용 범위:

- 명시 승인과 `execute_orders.py` gate를 통과한 `order_resv`
- 명시 승인과 `execute_orders.py` gate를 통과한 `order_cash`
- 필수 원주문 식별자가 있는 기존 active pending/reserved 주문의 정정·취소·대체 제출

가격·계좌 증거 수집 허용 범위:

- `Main agent`가 `scripts/collect_main_evidence.py`를 실행해 대상 종목의 direct KIS 현재가와 sanitized 계좌 스냅샷을 수집
- 결과는 `reports/runs/<run_id>/price-chart.json`, `account-before-order.json`, 선택적 `account-asset-snapshot.json`, 선택적 `collection-summary.json`에 저장
- `account-asset-snapshot.json`은 MTS 총자산 추이 표시용 optional snapshot이며, allowlist 필드만 저장하고 성공 시 `memory/account-assets/account-assets.jsonl`에 append-only로 누적한다. 이 값은 `decision-brief`, review sub-agent, 주문 gate 입력으로 사용하지 않는다.
- helper는 주문 제출, 예약, 정정, 취소를 수행하지 않으며, active 주문/주문가능 조회가 누락된 경우 실제 주문 단계는 `execute_orders.py` gate에 따라 차단한다.

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

`analyst-review`, `judge-review` agent 금지 범위:

- KIS, MCP, web, network, 외부 출처 호출
- 명시된 artifact/persona/rule 파일을 `cat`, `jq`로 read-only 조회하는 경우를 제외한 shell 사용
- unlisted 파일 읽기
- 파일 쓰기
- 재수집
- 주문 API
- raw prompt fallback

평결 JSON에서 생성하는 companion Markdown은 사람이 각 자산 판단을 확인하기 위한 보조 산출물이다. 파일명은 `prompts/analyst-review-format.md`의 safe-name 규칙을 따른다. 이 Markdown은 점수 집계, 최종 보유수량 조정, 주문 후보 계산, 실행 gate의 입력으로 쓰지 않는다.

## 아티팩트

Run 아티팩트는 `reports/runs/<run_id>/` 아래에 둔다.

- `run.json`
- `model-usage.jsonl` (daily-trading Main/sub-agent에 실제 전달한 model, reasoning effort, stage, role, task를 한 줄씩 기록; 알려진 daily-trading 호출은 실행 직전, 사후 감지된 Main fallback은 run artifact 확인 직후 기록)
- `price-chart.json`
- `collection-summary.json` (optional direct main-evidence helper summary)
- `account-asset-snapshot.json` (optional total-asset snapshot for reporting/dashboard only)
- financial memory 경로 `memory/collect-financial-information/financial-YYYY-MM-DD.yaml` (optional best-effort cache path)
- 뉴스 memory 경로 `memory/symbol-news-cache/symbol-news-YYYY-MM-DD.yaml` (optional best-effort cache path)
- `news-context.json` (직전 거래 run 이후 구간의 `symbol_news`/`market_news` 중복 제거 결과)
- `decision-brief.json`
- `review-inputs/*.review-core.json` / `review-inputs/*.analyst-review-slice.json` (launcher-created non-canonical selected-symbol slices; analyst-review `review-core`는 role-scoped)
- `analyst-review.json`
- `debate/opening.json`, `debate/opening-<side>-compact.json`, `debate/rebuttal-1.json` 및 실행 phase/attempt별 spec
- `subagents/judge-debate-*.wrapper.json`, `.raw.txt`, `.events.jsonl` (Bull/Bear session ID와 명시적 판단·반박 로그)
- `judge-debate.json` (judge가 읽는 정규화된 전체 토론 artifact)
- `judge-review.json`
- `reviews/<stage>--<agent_role>--<task_name>.md` (review agent별 사람 확인용 companion Markdown)
- `account-before-order.json`
- `order-lifecycle.json` (Judge 전 active 주문, 같은 날 이전 cash 주문의 최신 KIS 상태, 계좌 수량 일치 여부)
- `execution.json`
- `pipeline-summary.json` (`review_summary`, `report_path` 포함)

사람이 읽는 최종 포트폴리오 보고서는 run directory 밖의 `reports/YYYY-MM-DD_포트폴리오.md`에 둔다.

## 평결 입력

`decision-brief.json`은 Main agent가 수집 결과를 합쳐 만든 canonical review input이다. `analyst-review` sub-agent에는 launcher가 `decision-brief.json`에서 파생한 role-scoped `review-core` slice를 전달한다. 이 slice는 해당 execution agent가 산출해야 하는 view들의 입력 profile union만 보존한다. `judge-debate` opening의 Bull/Bear에도 같은 selected-symbol 입력을 주고, 이후 두 반박은 opening에서 생성된 각 session을 재개하면서 상대의 직전 raw JSON artifact를 명시적으로 전달한다. `judge-review`에는 `analyst-review.json` 전체가 아니라 selected-symbol analyst-review slice와 정규화된 `judge-debate.json`을 전달하고, 보유/가격 정보는 함께 전달되는 `review-core`에서 읽는다. `judge-review`의 `review-core`에는 launcher가 `account-before-order.json`의 broker 평균매입가(`pchs_avg_pric`)에서 직접 계산한 `position_cost_context`(평균매입가, 매입금액, 보유수량, 현재 판단가, 평균매입가 대비 괴리율)를 종목별로 추가한다. 이 필드는 `decision-brief.json`에는 저장되지 않고 `judge-review` stage에서만 그때그때 합성되므로, `analyst-review`와 `judge-debate`의 review-core에는 구조적으로 나타나지 않는다. `judge`는 `position_cost_context`를 손익·리스크·포지션 조정의 참고 정보로 활용하고, 최종 방향과 목표 노출은 thesis, 시장 근거, 포트폴리오 위험을 함께 고려한다.

`decision-brief.json`에는 종목 식별자, eligibility, `evidence_mode`, 가격, 핵심 price/chart signal, compact 기간봉/분봉·호가·체결·수급 요약, top-level `market_index_snapshot`과 `market_news_context`, financial/ETF summary, 종목별 `symbol_news_summary`, 계좌 노출, 누락/오류 사유를 압축해서 담는다. `market_news_context`는 직전 완료 거래 run 이후 현재 run까지 저장된 국내·해외·거시·지정학 기사 중 중복 제거된 최대 30건이며 종목별로 복사하지 않는다. `analyst-momentum-cycle`과 `judge`는 개별 종목과 명시적으로 연결되는 기사만 사용하고, 시장뉴스 자체를 자동 주문 신호로 취급하지 않는다. `analyst-news-flow`는 종목별 KIS `symbol_news_summary`만 사용한다.

재무/뉴스가 없더라도 식별자, 종목명, 현재가 또는 직전 거래일 가격 snapshot, 관측시각이 있으면 `eligible_for_review=true`로 유지한다.

같은 날짜 financial/`symbol_news` cache는 top-level `symbols` 키가 전체 universe를 덮으면 그대로 사용한다. 종목뉴스 coverage는 cache date와 article date가 일치하는 비어 있지 않은 기사만 센다. stale-only 종목뉴스 캐시는 full-universe cache로 보지 않고 deterministic collector를 한 번 실행한다. `market_news`는 scheduler가 15분마다 별도 SQLite 공간에 누적하며 daily-trading run은 네트워크 재수집 없이 DB를 읽는다. `news_context`는 직전 완료 거래 run 시각을 시작점으로 하되 최대 72시간으로 제한하고, URL과 정규화 제목으로 두 뉴스 범위의 중복을 제거한다.

`analyst-review`는 canonical score 관점 4개(`analyst-quality-value`, `analyst-risk-allocation`, `analyst-momentum-cycle`, `analyst-news-flow`)를 유지하되 실행 sub-agent는 2개다. `analyst-quality-risk`가 `analyst-quality-value`와 `analyst-risk-allocation` view를 서로 독립적으로 산출하고, `analyst-momentum-news`가 `analyst-momentum-cycle`과 `analyst-news-flow` view를 서로 독립적으로 산출한다. `analyst-quality-value`는 주식의 usable 재무 요약 또는 ETF/ETN의 usable ETF 요약이 전혀 없으면 `no_financial_excluded`, `analyst-news-flow`는 cache date와 article date가 일치하는 usable 뉴스/공시 요약이 없으면 `no_news_excluded` 감사용 row로 보존하되 analyst-review 평균 모수와 judge-review 입력에서 제외한다. 현재가·등락률·업종명 또는 ETF 거래량만 있는 요약은 quality/value evidence로 보지 않는다. 모든 score는 JSON 정수 `0..10`이어야 하며 비정상 값은 중립 5로 대체하지 않고 검증 실패로 처리한다. Optional 영역의 누락은 포함되는 view의 근거가 얇거나 Judge 근거가 불충분하다는 뜻으로 사용하지 않는다. Main agent가 가격, 보유, 뉴스, active 주문 상태의 안정성을 증명할 수 있는 unchanged symbol만 직전 유효 run의 review row를 병합할 수 있다. 증명할 수 없으면 재평가하고, sell/stop-loss 후보, 당일 체결, 가격 급변, 신규 뉴스, active 주문, score boundary 근처 종목은 반드시 재평가한다. Launcher 자동 wrapper 재사용은 같은 spec fingerprint에 한정된다.

종목이 eligible이고 가격 관측값과 최종 보유수량, 계좌 제약, 주문 API/경로를 통과하면 재무/뉴스 누락·partial·failed·no-data는 `order_cash`/`order_resv` demo/real 제출을 단독으로 차단하지 않는다.

## 주문 경계

`scripts/execute_orders.py`는 Python 코드로 구현된 실행 gate를 적용한다. 별도 최종 리스크 sub-agent와 승인 아티팩트는 사용하지 않는다.

`expected_holding_quantity`는 현재 후보 주문 제출 전, 이미 존재하는 미체결·예약 수량만 반영한 예상 보유수량이다. `final_holding_quantity`와 다르다는 이유만으로 후보 불일치나 주문 차단으로 판단하지 않는다.

기존 active pending/reserved 주문이 최종 보유수량, 방향, 잔여수량, 가격, 주문 API, 주문 경로와 맞지 않으면 `scripts/execute_orders.py`는 필수 원주문 식별자가 있을 때 같은 방향/API/경로 주문은 정정하고, 취소가 필요한 대체 주문은 취소 요청이 접수된 뒤 같은 명시 실행 run에서 검증된 대체 주문을 제출한다. 식별자가 없거나 취소/정정/대체 주문 결과가 불확실하면 `execution.json`에 `blocked`로 남긴다.

사용자 또는 schedule이 demo 또는 real 실행을 명시했고 `--submit-orders`가 전달됐으며 `execute_orders.py`의 모든 실행 gate를 통과한 경우에만 `scripts/execute_orders.py`가 주문 API를 호출한다.

### portfolio-except 제외 종목

`config/portfolio-except.txt`(형식은 `portfolio.txt`와 동일, env `PORTFOLIO_EXCEPT_FILE`로 override)는 봇 매매 제외 종목 목록이다. 두 겹으로 강제된다.

- `$check-portfolio`가 recommanded/specified/holding 세 소스 모두에서 해당 종목을 빼고 `universe`를 조립하므로, 제외 종목은 decision-brief, analyst-review, judge-review에 아예 나타나지 않는다. payload의 `portfolio_except` 키로 제외 목록을 노출한다.
- `scripts/execute_orders.py`의 `reconcile`이 최종 gate로, 어떤 경로로든 계획에 제외 종목 주문이 들어오면 `symbol_in_portfolio_except_list`로 `blocked` 처리한다. 제외 종목의 기존 active 주문도 계획 순회에서 차단되므로 정정/취소 대상이 되지 않는다. deferred-buy-retry도 enqueue와 실행 시점 양쪽에서 같은 목록을 확인한다.

목록은 파일을 직접 수정하거나 Telegram `/add_portfolio_except_ticker`, `/remove_portfolio_except_ticker` 명령으로 관리하며, 변경은 다음 run부터 반영된다(`/app/config` bind mount). 이미 접수된 미체결/예약 주문은 자동 취소하지 않으므로 필요하면 사용자가 직접 정리한다.

## 스케줄 daily-trading broker-preflight gate

`schedules.yaml`의 `daily_trading` submit(`demo-submit`/`real-submit`, `--submit-orders`) run은 전체 리뷰(universe 증거 수집, `analyst-review`, `judge-debate`, `judge-review`, 주문 실행) 전에 결정론적 broker-preflight gate를 거친다. `service/telegram/worker.py`의 `$execute-trade`는 `invocation_type=manual`, `service/scheduler/scheduler.py`가 실행하는 schedule job은 `invocation_type=scheduled`로 `run_daily_trading_pipeline.py run --invocation-type`에 전달한다. `analysis`/`prepare` request-type이나 `--submit-orders` 없는 run은 gate를 타지 않고 기존 동작 그대로 실행된다(오프라인 분석/unit test가 broker 접근을 요구하지 않도록 유지).

Gate는 `scripts/collect_main_evidence.py collect --skip-price-chart --skip-account-asset`로 전체 universe 가격/차트와 optional account-asset snapshot 없이 계좌(`account-before-order.json`)와 당일 체결(`today-fills.json`)만 수집하고, `scripts/execute_orders.py preflight`(non-required)로 기존 active pending/reserved 주문과 같은 날 이전 cash 주문의 최신 상태를 반영한다. 이 preflight는 새 주문을 제출하지 않는다.

**Safety 판정** — 다음 중 하나라도 해당하면 안전하지 않은 것으로 보고 `decision=safety_block`을 기록하며, LLM을 호출하지 않고 새 주문도 제출하지 않는다: 계좌 조회 실패, active order lifecycle lookup 불완전, 주문가능금액 조회 불가, `holding_state_issue_count>0`, 당일 체결(`today-fills.json`) 조회가 `status=success`·`skipped=false`·`fill_scope=account`가 아닌 경우, 또는 universe 밖 실보유 종목이 현재 `portfolio_except`에 명시되지 않은 경우(`unexpected_non_universe_holding`). safety_block은 실패한 실행이 아니라 완료된 실행이며 `run.json`/`pipeline-summary.json` status는 `partial`로 남아 경고 요약과 함께 성공 종료한다.

**Full-review 판정** — 안전한 preflight에서만 판단한다. `invocation_type=manual`은 항상 full이다. scheduled는 다음 중 하나면 full이다: 그 날의 첫 안전 run(`first_safe_run_of_day`), 직전 같은 날짜/환경 preflight 대비 broker fingerprint 변경(`broker_fingerprint_changed`), 또는 `daily-trading-full-review-times.yaml`의 고정 KST 시각 중 현재 시각 이하 최신 시각이 아직 성공적으로 완료된 full review로 충족되지 않은 경우(`fixed_review_time_due`) — 이 규칙 덕분에 09:05 full review가 실패/누락되면 실제 시작 분과 무관하게 다음 invocation(예: 09:20)이 다시 full을 선택한다. 셋 다 아니면 `decision=skipped`로 전체 증거 수집/optional cache/LLM/실행 이전에 종료한다.

Fingerprint는 `universe`, holdings(`current_live_holding_quantity`/당일 매수·매도수량), `active_status=active`인 active order만(identity/방향/가격/잔여수량/status), 당일 체결(identity/종목/방향/체결수량), 주문가능금액, 그리고 `daily-trading-strategy-policy.yaml`/fixed-time config/`review-extra-instructions` sha256을 합친 `config_fingerprint`로 구성한다. `observed_at`/`generated_at` 등 변동 timestamp는 제외해 같은 broker 상태의 두 preflight가 항상 같은 해시를 만들도록 한다.

State 저장은 `memory/daily-trading/review-trigger-state-<real|demo>.json`(env별 분리)에 `date`, `fingerprint`, `fingerprint_payload`(sanitized canonical payload), `last_satisfied_time`을 원자적/fcntl-lock으로 기록한다. `decision=skipped`는 즉시 fingerprint를 저장한다. `decision=full`은 이 시점에 저장하지 않고, 전체 리뷰가 성공적으로 끝난 뒤(`finalize_review_gate_state`, 실행 후 관측된 broker 상태로 재계산한 fingerprint 사용)에만 fingerprint와 `last_satisfied_time`을 갱신한다 — crash/실패한 full review는 due slot도 fingerprint도 충족시키지 않으며, 다음 invocation은 저장된 이전 state와 다시 비교해 같은 broker 변경을 또 감지한다. state 파일 쓰기 자체가 실패해도 완료된 거래 run을 실패로 바꾸지 않고 non-required `review-trigger-state-persist` partial stage만 남기며(다음 invocation은 이전 state와 비교하므로 보수적으로 재판정된다), full review 대상 KST 고정 시각은 `daily-trading-full-review-times.yaml`(env `DAILY_TRADING_FULL_REVIEW_TIMES_CONFIG`로 override 가능)에서 읽고 오름차순 HH:MM인지 로드 시 검증한다.

각 run은 `reports/runs/<run_id>/review-trigger.json`에 `decision`, `reasons`, `due_slot`, `prior_state`, `fingerprint`, `fingerprint_payload`, `changed_components`(직전 payload 대비 달라진 top-level 구성요소: `holdings`/`active_orders`/`fills`/`cash`/`universe`/`config`), `safety`, `full_review_selected`, `full_review_completed`를 기록한다(계좌번호·credential·raw broker response는 포함하지 않음). `full_review_selected`와 `full_review_completed`를 분리해 "이 gate가 full을 선택했다"와 "그 full review가 실제로 성공 완료됐다"를 구분하므로, crash한 full review가 due slot을 충족한 것으로 잘못 기록되지 않는다.

Gate가 `full`을 선택하면 이미 수집·lifecycle-reconcile된 `account-before-order.json`/`today-fills.json`을 그대로 이어받는다 — 이어지는 `collect_main_evidence()` 호출은 `--reuse-account`로 계좌/체결을 재수집하지 않고 가격/차트와 optional account-asset snapshot만 수집하며(run_id/status 일치 여부를 확인한 뒤 재사용), lifecycle preflight도 다시 실행하지 않는다. 4/6 analyst-review 선정, judge-debate, judge-review, `execute_orders.py`의 주문 전 gate 재확인(`refresh_gates`)과 모든 실행 gate는 기존과 동일하게 유지된다.

## 유지보수 계약

이 섹션은 사람이 보는 유지보수 계약이다. Runtime에서 이 문장을 직접 읽지 않으며, 실제 동작은 `scripts/` 아래 Python 코드에 구현되어 있다. LLM review sub-agent가 runtime에서 읽는 Markdown은 `prompts/` 아래 파일뿐이다.

### 인증과 토큰

- Direct KIS helper는 app key, 계좌 설정, 거래 환경을 runtime 환경변수에서 읽는다.
- 토큰 발급과 갱신은 shared `kis-token` helper를 사용한다.
- 인증, credential, token, permission, 계좌 설정 오류는 local trading retry 대상이 아니다. Sanitized error로 중단하거나 주문을 차단한다.
- 아티팩트, prompt, 보고서, Telegram 응답에는 계좌번호, product code, HTS ID, app key, app secret, access token, raw request header를 노출하지 않는다.

### 수집 경계

- 필수 가격, 차트, 최초 계좌 증거는 `collect_main_evidence.py`가 수집한다.
- Financial, `symbol_news`, `market_news` domain은 optional best-effort 입력이다. 해당 데이터 부재만으로 review 또는 주문 실행을 차단하지 않는다.
- Market index snapshot은 optional best-effort run-level 입력이다. 해당 데이터 부재만으로 review 또는 주문 실행을 차단하지 않는다.
- 같은 날짜 full-universe financial/`symbol_news` cache가 없거나 불완전할 때 cache collection은 pipeline run당 한 번만 시도한다. `market_news` 수집은 daily-trading이 아니라 scheduler job이 담당한다.
- Collection sub-agent는 account, balance, order, order-available, fill-history, pending-order, reservation, correction, cancellation API를 호출하지 않는다.

### 아티팩트 계약

- `run.json`은 `run_id`, `started_at`, status, stage records를 보존한다.
- `decision-brief.json`은 compact canonical review input이다.
- `analyst-review.json`은 analyst-review score view 병합 결과다.
- `judge-review.json`은 단일 judge target position value와 helper가 정규화한 final holding set이다.
- `execution.json`은 final holding delta, gate decision, active-order reconciliation, submitted/skipped/blocked order result, sanitized error를 기록한다.
- `pipeline-summary.json`은 service output을 위한 compact diagnostic source다.
- `telegram-summary.txt`는 `pipeline-summary.json`에서 렌더링한 고정 user-facing 응답이며, service code는 raw artifact에서 새 summary를 재구성하지 않는다.
- `subagents/<task>.raw.txt`는 `codex exec -o`가 쓴 최종 sub-agent output이다.
- `subagents/<task>.events.jsonl`과 `subagents/<task>.stderr.txt`는 토큰 spike 디버깅용 raw `codex exec --json` 이벤트/표준에러다. wrapper의 additive `event_diagnostics`는 event type, usage event sequence, tool call/result 크기, 반복 command/file-read fingerprint를 content-light로 요약해 큰 prompt/artifact 입력, 큰 tool result, 반복 tool loop, usage event 중복/누적 집계를 구분하게 한다. MCP 초기화 오류는 성공 wrapper도 실패로 바꾸지 않고 `degraded_dependencies`에 서버 식별자(없으면 `mcp:unknown`), phase, HTTP status를 기록하며 anomaly retention 대상으로 보존한다.
- Raw event/stderr debug artifact는 경로, 명령, tool 출력 등 민감한 운영 정보를 포함할 수 있으므로 retention 정책에 따라 제한 보존하고, portfolio report/Telegram summary/user-facing 응답이나 execution truth로 사용하지 않는다.

### Review 계약

- `analyst-review`는 두 execution persona가 네 canonical view를 산출한다: `analyst-quality-value`, `analyst-risk-allocation`, `analyst-momentum-cycle`, `analyst-news-flow`.
- `judge-review`는 `judge`만 사용한다.
- Review sub-agent는 supplied artifact, 자기 prompt, 자기 스테이지의 format 파일(`prompts/analyst-review-format.md` 또는 `prompts/judge-review-format.md`)만 읽는다.
- Review sub-agent는 KIS, MCP, web, network, account/order API 또는 외부 출처를 호출하지 않는다.
- Review sub-agent는 compact JSON만 반환하고, 파일 쓰기, Markdown sidecar 생성, diff 출력, code fence 출력을 하지 않는다.

### 주문 실행 계약

- `judge-review`는 target position value를 결정하고, deterministic helper가 이를 half-up 반올림 final holding quantity와 order candidate로 변환한다.
- 실제 order API는 명시적 demo/real authorization 이후 모든 gate를 통과했을 때만 `execute_orders.py`가 호출한다.
- 지원 대상은 immediate cash order, reservation order, active order reconciliation을 위한 supported correction/cancellation API다.
- 주문 전 `execute_orders.py`는 active pending/reserved order와 order-available quantity를 포함한 required read-only account gate를 갱신한다.
- 기존 active order는 symbol, direction, remaining quantity, price, environment, API, order path가 원하는 candidate와 일치할 때만 유지한다. 아니면 required identifier/API support에 따라 정정, 취소/대체, 또는 차단한다.
- Buy/sell quantity는 최신 order-available 및 sell-available gate에 따라 축소되거나 차단되어야 한다.
- 불확실한 order result는 blind retry하지 않는다. 먼저 read-only state를 갱신하고, 최신 상태가 주문 미접수를 증명할 때만 재시도한다.

### 보고 계약

- Portfolio report와 Telegram summary는 final artifact에서 생성하며 raw helper output을 수동 재요약하지 않는다.
- Human-review Markdown sidecar는 정보 제공용이다. Scoring, final holding quantity, order gate 입력으로 쓰지 않는다.
- Optional financial/news data 부재는 missing evidence로 보고할 수 있지만, 문구만으로 hard blocker가 되면 안 된다.

### Strategy mapping 계약

- Strategy label은 signal과 reason code 해석을 위한 유지보수 vocabulary다.
- Runtime scoring과 final holding calculation은 structured artifact와 Python implementation에 의해 결정되며, 이 문서를 다시 읽어 동작하지 않는다.
