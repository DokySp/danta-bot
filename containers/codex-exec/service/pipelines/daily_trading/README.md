# daily-trading

`daily-trading`은 한국 주식·ETF 포트폴리오 전체를 대상으로 수집, 평결, 주문 실행을 수행하는 Python pipeline package다. Codex skill entrypoint로 노출하지 않는다.

Sub-agent 모델과 effort는 `CODEX_RUNTIME_CONFIG_FILE`이 가리키는 `codex-runtime.yaml`의 `daily_trading`에서 조정하고, `scripts/run_subagent.py`가 실행 직전에 해당 파일을 읽어 `codex exec` 명령에 지정한다. 모델과 effort는 비어 있지 않은 문자열인지만 확인하며, 모델별 지원 여부는 `codex exec`가 판단한다.

Scheduled daily-trading jobs with a `daily_trading` block in `schedules.yaml` are executed by the codex-exec Python direct runner, which calls `scripts/run_daily_trading_pipeline.py run` without starting Main Codex. Telegram/user-facing 응답은 `pipeline-summary.json`을 직접 말로 재구성하지 않고, `scripts/render_telegram_summary.py`가 만든 짧은 `telegram-summary.txt`를 본문으로 전송한 뒤 `scripts/render_html_report.py`가 만든 `daily-trading-report.html`을 첨부한다. HTML은 해당 run 시각까지 같은 날짜의 run을 누적하여 거래·체결, 전체 Analyst 대상, Judge 단계별 판단, 재무·시간별 뉴스, 계좌·KOSPI 추이와 보유현황을 한 파일의 탭 UI로 제공한다. `today-fills.json`은 계좌 전체 일별 체결을 보존하고 HTML은 당일 run들의 체결을 주문번호 기준으로 중복 제거하며, 수집 실패나 과거 universe 범위 artifact는 당일 전체 체결로 단정하지 않고 상태를 표시한다. 확인되지 않은 원금이나 계좌수익률은 추정하지 않으며, HTML 생성 실패는 완료된 거래를 실패로 바꾸지 않고 `html-report` non-required stage와 `html_report_available=false`로 남긴다. `pipeline-summary.json`은 `review_summary`, `account_display_summary`, `today_fills_summary`, `evidence_summary`, `telegram_response_policy`, `report_path`, `telegram_summary_path`, `html_report_path`를 포함하므로 진단이 필요할 때만 읽는다. `telegram-summary.txt`의 매수/매도/유지 카운트는 `review_summary.final_sell_count/final_buy_count/final_hold_count`로, 각 종목의 현재 보유수량→최종 보유수량 방향에서 도출한 최종 결정이며 미해결·무효 Judge 판단은 유지로 합산하지 않고 `unresolved_review_scope_count`로 분리한다(있을 때 `미결`로 표시). `held_review_scope_count/active_order_review_scope_count/unheld_review_scope_count`는 `review_scope_reasons`(held_position/active_order/unheld_score_rank)에서 산출한 이번 run의 Judge 심사대상 구성이고, `hold_symbol_count`는 `scored_count - len(review_scope_reasons)`로 산출한 "심사대상에 아예 선정되지 않은 scored 종목 수"이며 Judge의 보유(유지) 판단이나 최종 평결이 아니다. 이 값들은 최종 평결이 아니며, 최종 카운트가 없는 구버전 summary를 렌더할 때만 `Judge 검토: 보유 심사대상 · 활성주문 · 비보유 상위선정 · 미선정`으로 표기한다.

`today-fills.json.previous_session`은 차트에서 확인한 전 거래일의 대상 종목 체결과 실전계좌 KIS 종목별 실현손익을 보존한다. Judge의 `prior_decision_context`는 이를 전 거래일 목표수량 경로·종료 보유수량·종가 대비 마지막 체결가와 합치고, 결과를 과거 판단의 점수나 주문 게이트로 사용하지 않는다. 모의계좌처럼 KIS 실현손익 API가 지원되지 않는 경우에는 추정하지 않고 `unavailable`로 둔다.

### 점수-게이트 재설계 소유권 경계 (score-gate redesign)

Analyst의 0-10 점수는 evidence 강도/방향의 advisory/ranking/reporting 입력일 뿐이며, 후보 방향·목표 방향·주문 허용 규칙이 아니다. 보유 중인 모든 eligible 종목은 점수 유무와 무관하게 Judge 대상에 포함되고, 비보유 종목은 `daily-trading-strategy-policy.yaml`의 `unheld_review_top_k`만큼 점수 순으로 추가된다. Judge는 종목별 `target_position_value_krw`를 최종 전략 결정으로 내리고 pipeline은 이를 주 단위 수량으로 반올림한다. `decision_basis`, thesis 필드, `additional_buy_reason`은 감사와 다음 판단의 문맥일 뿐 목표를 허용하거나 차단하지 않는다. 주문 단계는 명시적 submit 승인, 계좌·현금·보유수량·매도가능수량, 활성 주문, 주문 가격·시장 상태와 lifecycle 검증을 유지한다.

`당일 누계`(매수/매도 금액·체결 건수)는 `account-before-order.json`과 수집 시점 `today-fills.json`에서 온 이번 run 주문 전 스냅샷이므로 `당일 누계(이번 run 주문 전 기준)`으로 표기한다. 명시적으로 승인된 `demo-submit` 또는 `real-submit`에서는 `--submit-orders`를 함께 넘겨 `scripts/execute_orders.py`가 broker gate 갱신, 기존 pending/reserved 주문 조정, 주문 제출·정정·취소·차단과 최종 summary 재생성을 수행한다. `--order-path auto --exchange AUTO`는 KIS 종목정보로 종목별 거래소를 먼저 정한 뒤 해당 거래소의 즉시주문 시간 또는 KRX 예약주문 경로를 선택한다. 예약주문은 `KRX`만 허용하며 23:40~00:10 서버 초기화 시간에는 제출하지 않는다. `--exchange KRX|NXT|SOR`를 명시하면 즉시주문의 증거수집 시장(`J|NX|UN`)과 KIS `EXCG_ID_DVSN_CD`를 함께 지정한다. 설치 또는 pipeline 변경 후에는 README 하단의 daily-trading self-test와 저장소 테스트를 실행한다.

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
  [--exchange <KRX|NXT|SOR>] \
  [--main-events <codex-json-events-path>]
```

이 명령은 helper/launcher의 큰 stdout을 `pipeline-command-log.json`에 저장하고 stdout에는 compact summary pointer만 출력한다.

## 용어 규칙

| 개념 | 표준 표기 |
|---|---|
| 메인 실행 주체 | `Main agent` |
| 1차 독립 종목 평결 단계 | `analyst-review` |
| 2차 포트폴리오 최종 보유수량 평결 단계 (내부 대립 관점 검토 포함) | `judge-review` |
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
| 6 | `scripts/run_daily_trading_pipeline.py` + `judge-review` | 보유 eligible 전종목과 비보유 점수순 상위 종목을 고른 뒤 단일 judge 호출로 평결 | launcher-created selected-symbol slices, `judge-review-format.md` | `judge-review.json`, judge sidecar | 점수는 비보유 top-K 순위에만 쓰이며 방향·주문 허용을 정하지 않는다. judge는 별도 sub-agent를 생성하지 않고 이 한 번의 호출 안에서 종목별 `opposing_view`(increase_case/reduce_case)를 구성한다. 중요도·최신성·포트폴리오 영향을 비교해 근거의 순우위 강도에 비례한 목표금액을 정하며, 충돌 자체는 자동 보유 규칙이 아니다 |
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
| selected 2 analyst-review execution personas | `analyst-review` 독립 종목 점수 (`analyst-quality-risk`와 `analyst-momentum-news`가 각각 두 view 산출) | `gpt-6-astra` | `xhigh` |
| `judge` | `judge-review` 포트폴리오 목표금액 (내부 대립 관점 검토 포함) | `gpt-6-astra` | `xhigh` |

## API 권한

`Main agent` 계좌 조회 허용 범위:

- 계좌 자산 요약
- 잔고와 보유수량
- 당일 체결
- 미체결 주문
- 예약 주문
- 매수가능 조회
- 매도가능 조회는 검증된 direct template이 있을 때만 사용하고, 현재 runner는 현재 보유수량에서 active 매도 예약을 뺀 값을 매도 gate로 사용

당일 체결과 최근 제출 거래 이력은 빈 목록만으로 `거래 없음`으로 확정하지 않는다. 당일 체결은 `collection_status`, 최근 제출 거래는 `coverage_status`가 `complete`일 때만 빈 목록을 확인된 부재로 해석하며, `partial`/`unavailable`은 미확인 상태로 유지한다. `additional_buy_reason`은 당일 재매수 판단의 설명이 필요할 때만 기록한다.

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

과거 시점 재현용 `scripts/agent_replay_backtest.py`는 허용 입력을 prompt에 직접 포함하고 user config와 MCP를 불러오지 않는 read-only·ephemeral 실행을 사용한다. event stream에서 도구 호출이 관측되면 해당 wrapper를 실패 처리한다. replay는 생산 Judge 목표를 그대로 가상 체결하며 별도 전략 규칙을 추가하지 않는다. `scripts/account_performance_audit.py`는 보관된 계좌·체결·시장지수 아티팩트로 실적, 무거래 기준, MDD와 회전율을 읽기 전용 계산한다.

평결 JSON에서 생성하는 companion Markdown은 사람이 각 자산 판단을 확인하기 위한 보조 산출물이다. 파일명은 `prompts/analyst-review-format.md`의 safe-name 규칙을 따른다. 이 Markdown은 점수 집계, 최종 보유수량 조정, 주문 후보 계산, 실행 gate의 입력으로 쓰지 않는다.

## Stable 분석 기준과 실험 보존

전략·Judge 프롬프트·deferred-buy-retry는 `v20260825-001-stable` (`f90efa6`) 기준이며 Astra 모델 설정과 감사·replay 도구는 유지한다. 미검증 보유·매도 프롬프트 변경은 별도 사본으로 보존하고 활성 프롬프트에서 제외했다. Phase 1~3의 최소 보유기간, 교체 문턱, 순위 자금배분과 전체시장 후보 주입은 활성 코드에 남기지 않는다. `834be6f`의 감사·시점 제한·strict artifact replay 도구와 거래일 증거 확인을 연구 경로에 보존한다. 연구 데이터·`research-strategy-v1.json`은 실전 판단·주문 경로와 분리한다.

### 2026-09-08 1단계: 비교 기준 정리

시작 HEAD는 `669dbb7`이다. Git 이력을 되감지 않고 미커밋 실험의 연결 코드·설정·프롬프트만 분리했다. `position_management.py`와 그 전용 테스트는 아래 원본 아카이브에 보존하고 활성 트리에서 제거했다. MA20/2 ATR 진입 제한, 20% 종목 한도/1% 변동성 위험예산, 상승 추세 부분매도 보호, 전일 부분매도 복원 예외는 적용하지 않는다. Judge 목표는 정수 주식 수로 반올림하며, thesis는 문맥이지 매도 허가 조건이 아니다. 이것은 수익성이 입증된 전략이 아니라 다음 변경을 비교할 기준이다.

유지한 기술 수정:

- 로컬 `codex-runtime.yaml`의 Analyst/Judge Astra 설정, 주문 직전 체결·잔고 재조회, 감사·replay·과거 데이터 수집 도구.
- Replay `started_at`을 아카이브 decision brief 생성 완료 시각인 `source_artifacts.information_cutoff`와 맞춘다. 수집 시작은 `source_artifacts.collection_started_at`으로 보존하며 실제 cutoff 이후 입력은 계속 차단한다. manifest의 `agent_clock`과 review contract가 다른 실행을 이어 붙이지 않는다.
- 이전 거래일 체결은 개별 `filled_at`의 한국 날짜와 제공된 `order_date`까지 확인한다. 다른 날짜·시각 누락은 합계에서 제외하고 coverage를 `partial`로 표시한다. 특정 매수·재진입 예외로 사용하지 않는다.
- `CODEX_SUBAGENT_TIMEOUT_SECONDS=0`은 해당 실행의 subprocess timeout을 해제한다. 미지정 기본 1800초와 양수 제한은 유지한다.
- Review contract를 **8**로 구분해 실험 버전 6/7의 저장 wrapper를 재사용하지 않는다. 기억을 체결된 투자 단위로 연결하는 2단계는 아직 구현하지 않았다.

`agent_replay_backtest.py --frozen-targets-root <기존 replay 폴더>`는 원래 요청 목표금액과 Analyst 의견을 고정하고 현재 주문 계획·수량/현금 게이트·다음 호가 모형으로 연속 가상 계좌를 계산하는 진단 도구다. 새 모델 호출은 0회이며 **Agent 재판단이나 새 전략의 수익성 검증이 아니다**. 보관된 실험용 `position_management_context`는 새 replay 입력에서 제거한다. 정책 조정된 과거 판단은 목표금액과 함께 보존된 원래 Agent 사유도 복원하며, 원래 목표·사유를 확인할 수 없으면 재생을 거부한다. 기존 `--disable-position-management` 옵션은 해당 정책 제거와 함께 삭제했다. 과거 실험을 정확히 재현할 때는 당시 소스와 manifest를 사용한다.

보존 위치: `/home/uhug/Downloads/danta-stage1-baseline-20260908-1gir5k/`. `source-before.tar.gz`는 정리 전 추적 파일과 미추적 실험 파일, `tracked-before.patch`는 HEAD 대비 원래 변경, `experiments-before.tar.gz`는 아래 최근 결과 3개 폴더의 원본이다. 압축 무결성과 원본 일치를 확인했으며 기존 결과 폴더도 수정하지 않았다. 이 로컬 백업과 기존 `backup/master-before-stable-rebuild-20260905` 브랜치는 배포 파일이 아니다.

### 실험 판단 기록 — 과거 결과이며 현재 기준의 새 성적이 아님

- **최소 보유·교체·순위 배분·스캐너(옛 Phase 1~3):** 해당 코드는 `backup/master-before-stable-rebuild-20260905`에 보존한다. `danta-phase2-phase3-pretest-20d-20260905-004/backtest-result.json`의 2026-08-04~09-01 저장 판단 비교는 스캐너 추가 시 +6.0421% → -1.0767%, MDD 2.7054% → 4.4123%, 회전율 98.1375% → 190.6465%였다. 모델 호출 0회·기존 목표·과거 체결 가정의 진단이며 통합 Agent 검증이 아니므로 활성 전략으로 재채택하지 않는다. 재검토에는 구성요소를 분리한 동일 조건의 새 판단 검증이 필요하다.
- **MA20/ATR 진입·위험예산 + 부분매도 보호·복원:** `danta-exit-reentry-20260908-jBYFGm/REPORT.md`. 2026-08-27~09-04 저장 Astra 판단 7일에서 수정 후 -1.6819%, 무거래 -1.6860%(차이 약 +481원); 별도 08-19~26 저장 Sol 판단 6일에서는 +2.5695%, 무거래 +8.4891%였다. 두 계좌를 연결한 13일 성적이 아니며 새 Agent 판단도 아니다. 안정적 수익성 근거가 없으므로 이번에 전체 규칙을 분리했다. 각 규칙의 기여와 미사용 구간 성과를 따로 확인하기 전에는 재도입하지 않는다.
- **보유 종목 추가매수에 새 종목 고유 촉매 요구:** `danta-single-candidate-20260908-8Br0Or/pair-evaluation.json`과 `FINAL-REPORT.md`. 2026-08-11~18의 5거래일, 각 조건 별도 연속 계좌·Astra/xhigh 총 30회 성공 판단·20bp 가정 비용에서 기준 +4.1389%, 후보 +4.2759%, 무거래 +4.4447%였다. 사전 통과 기준을 충족하지 못해 후보 문구는 이미 원복했다. 해당 기준군에도 당시 position-management 실험이 포함되어 있었으므로 이번 정리된 기준의 성적으로 인용하지 않는다. 이미 사용한 짧은 구간에서의 개선만으로 재도입하지 않는다.

위 폴더는 모두 `/home/uhug/Downloads/` 아래에 있다. 무거래는 현금 100%가 아니라 시작 주식과 현금을 그대로 보유한 비교다. 실험 당시 코드·설정·모델·입력·비용 가정과 결과를 함께 확인해야 한다. 이번 단계에서는 수익률 백테스트, 운영 설정 변경, 배포 또는 실제 주문을 수행하지 않는다.

`execute_orders.py`는 주문 게이트 조회 후 기존 수집기로 당일 체결과 잔고를 다시 조회한다. 당일 매수·매도 수량, 이전 잔고 이후 체결에 따른 수량 변화, 보유량+미체결 기준 예상 수량을 대조한다. 조회 실패는 제출을 차단하고 수량 불일치는 해당 종목의 추가·정정 주문을 보류한다. 예를 들어 보유 5주·매도 미체결 1주·목표 4주에서 체결 후 보유가 4주이면 추가 매도하지 않는다. 잔고는 5주인데 미체결만 사라지면 차단한다. 정상 취소나 외부 거래로 예상 수량이 바뀐 경우도 다음 run에서 다시 판단한다. 취소 전용 실행은 기존 취소 경로를 유지한다. 판단 당시 `account-before-order.json`의 잔고·가격·조회 시각은 덮어쓰지 않고, 새 수량과 조회 시각·계좌 요약은 `execution.json.holding_refresh`에 별도 기록한다. 서로 다른 API 조회와 제출 사이의 원자성까지 보장하는 것은 아니며, 전략 수익성 검증과 별개인 실행 안전 수정이다.

`scripts/market_scanner.py collect-history`는 `a86e209`의 재개 가능한 KIS 일봉 수집 기능만 제공한다. 실전 후보 발굴·점수화·매수 로직이나 스캐너 백테스트는 제공하지 않으며, 기존 SQLite와 실험 결과는 외부 분석 폴더에 보존한다. 현재 종목 마스터에 따른 생존편향은 남는다.

현재 replay는 실전 `build_execution_plan`의 주문 순서·거래소·지정가와 `execute_orders.apply_quantity_gates`의 수량/현금 제한을 재사용한다. 모든 주문의 매수 예산을 체결 전에 확정하므로 같은 묶음의 매도대금이나 먼저 관측한 유리한 매수가로 남은 돈을 뒤 주문에 즉시 재사용하지 않는다. 매수 예산은 가상 현금에서 가정한 비용을 유보한 현금 전용 추정치이며 실제 KIS의 신용·담보·종목별 주문 가능 금액을 재현한 값은 아니다.

다음 관측의 매도호가가 매수 지정가 이하이거나 매수호가가 매도 지정가 이상일 때만 1호가 수량 한도로 가상 체결한다. 주문 차단/미제출 수량과 제출 가능했으나 체결하지 못한 수량은 별도로 기록한다. 호가 사이의 체결, 대기열 순서, 미체결 주문 이월, 예약 주문, deferred retry는 재현하지 않는다. 따라서 `ready_for_approximate_replay`도 실전 동일 검증을 뜻하지 않는다.

모델 호출 전에 `--preflight-only`로 과거 거래소 경로와 시작 시점 활성 주문의 재현 가능성을 확인할 수 있다. 거래소 정보가 없거나 예약 경로/초기 활성 주문이 필요한 기간은 일반 replay도 모델 호출 전에 차단한다. 예:

```bash
PYTHONPATH=containers/codex-exec python3 -m service.pipelines.daily_trading.scripts.agent_replay_backtest \
  --preflight-only --start 2026-08-04 --end 2026-09-04 \
  --output-root /tmp/danta-execution-coverage
```

과거 매도 우선·매도대금 즉시 재사용 실험 결과는 원래 코드·입력·비용·체결 가정에 귀속되며, 현재 모형이나 stable의 새 성적으로 재분류하지 않는다. 새 실행 모형 식별자와 모델 설정을 manifest에 기록해 기존 progress를 혼합 재개하지 않는다. 보유·매도 정책을 비교하기 전에 이 근사 범위를 명시하고 미사용 기간의 입력을 별도로 확보해야 한다.

Judge 규칙 비교 시 `--judge-prompt <보존한 judge.md>`로 기준 지침을 지정할 수 있다. 생략하면 원래 생산 `prompts/judge.md`를 사용한다. 각 조건은 별도 `--output-root`에서 같은 초기 잔고로 시작해 이전 가상 체결·판단 이력을 이어간다. 수정 조건의 `--analyst-reuse-root <기준 output-root>`는 같은 날짜의 신규 Analyst 결과만 공유한다. 실제 생산 경로에서 구성한 전체 Analyst 프롬프트(출력 전용 Markdown 경로 제외), 모델·추론 수준 및 성공 wrapper 해시가 일치해야 하며 불일치하면 Judge 호출 전에 중단한다. 보유량·이력이 달라지는 Judge는 공유하지 않는다. 공유 Analyst의 토큰을 중복 합산하지 않으며 신규 호출·동일 실행 재개·조건 간 Analyst 공유를 구분한다.

## 연구용 공시 원본 수집

`scripts/dart_history.py`는 OpenDART 접수번호별 XBRL 원본·해시·접수일·정정 이력을 보관한다. 예: `PYTHONPATH=containers/codex-exec python3 -m service.pipelines.daily_trading.scripts.dart_history --corp-code 00126380 --start 2020-01-01 --end 2025-12-31 --output-dir /tmp/danta-dart-sample`. 인증키는 `DART_API_KEY` 환경변수 또는 화면에 표시되지 않는 입력으로 전달하며 URL·키·응답 오류 원문은 로그에 쓰지 않는다. `--request-limit` 기본 200회에 도달하면 중단한다. 사용 가능한 요청 예산이 있을 때 재개하면 공시 목록을 다시 조회하고 검증된 ZIP을 재사용한다. 누적 예산 소진을 보관 ZIP만으로 우회하지 않는다. 키를 명령행 인자나 저장소 파일에 넣지 않는다.

비교 가능한 표준 연결 KRW 매출·영업이익만 읽고 접수일 다음 날부터 사용한다. 원문의 금액과 `decimals` 정밀도를 보존하며 반올림 전의 정확한 회계 금액으로 해석하지 않는다. 12월 결산의 단일 접수본에 당기·전년 동기 3개월 금액이 있으면 Q1~Q4를 직접 비교한다. 비표준 계정·불명확한 연결 구분·누락 자료는 추정하지 않는다. 현재 재무 API 수치를 과거 날짜에 붙이지 않는다. 다른 분기의 뒤늦은 정정으로 최신 분기를 대체하지 않으며, 최신 분기 자료가 없으면 이전 긍정 신호를 되살리지 않는다. 원본 해시/접수번호 메타데이터 없는 고아 ZIP과 손상 XML은 중단하고 DART의 명시적인 자료/파일 없음 응답만 unavailable로 남긴다. 충돌 수치도 기본 모드에서는 중단한다. `--research`에서는 문법·숫자 검증을 통과했으나 같은 기간·계정의 값이 다른 접수본만 원본 보존 후 `financial_data_issue`로 격리하고 모든 금액을 신호에서 제외한다. 문맥 ID만 보고 연도를 고치거나 한 값을 고르지 않는다. 다른 XML·금액·인증·해시 오류는 여전히 중단한다.

직접 Q4 금액이 없으면 `q4_reconciliation`이 같은 회사의 연간 금액에서 1~9월 누적 금액을 뺀 당기·전년 Q4 후보를 계산한다. 계산에 사용한 접수번호·해시·기간·금액·정밀도를 보관하며, 이전 보고서와 중복되는 비교 금액이 바뀌었으면 정정 조정이 필요하므로 `unavailable`로 남긴다. 숫자가 맞더라도 CFS 표기만으로 연결 대상·회계기준의 일치를 입증할 수는 없다. 따라서 다른 접수본을 조합한 결과는 `derived_unverified`이고 금액/개선 여부는 `candidate` 안에만 둔다. `comparable_quarters`에 포함하지 않고 매매 신호로 사용하지 않는다. 수집 결과의 `derived_unverified_q4`는 이 후보 수다.

개별 접수본에 저장한 계산은 해당 접수일 다음 날 기준이다. 실제 과거 판단에서는 회사별 전체 `records`와 판단일을 `latest_asof`에 전달해야 한다. 이 함수는 그날까지 공개된 최신 9개월 보고서로 Q4를 다시 계산하므로, 뒤늦은 Q3 정정은 공개 다음 날부터만 반영된다. 최신 정정 원본이 없으면 이전 원본으로 대체하지 않는다. 조회 기간 전에 제출된 필요한 보고서가 없다면 자동으로 최신 자료를 끌어오지 않고 부족한 상태를 유지한다.

이 도구는 수집·시점 검증용이며 매매 규칙, Astra 판단 입력, 실전 주문에 연결되지 않는다. Q4 회계 비교 기준·업종·당시 종목 구성·상폐·장기 시세·기업행동 검증이 남으므로 결과의 `strategy_backtest_ready`는 false다.

### 2020~2025 연구 데이터와 고정 전략 v1

`research-strategy-v1.json`은 수익성 검증 전 가설이다. 실적 개선·종목/KOSPI 상승 추세일 때만 매수하고, 추세·실적이 유지되면 수량을 유지한다. 순위가 내려가거나 5일이 지났다는 이유로 교체하지 않는다. 최대 5종목·신규 종목당 직전 NAV 20% 한도, 현금만 사용하며 ETF 매매나 거래 판단 모델 호출은 없다. 상세 수치·비용·연속 장부·개발/검증 기간을 이 파일에 고정했다. 실전 Judge나 주문 경로는 이 파일을 읽지 않는다.

이전 연구 초안과 다른 점은 두 가지다. 검증되지 않은 Q4 차액을 쓰지 않고 사업보고서의 **같은 접수본 연간 대 연간**을 비교한다. Q1~3은 같은 접수본의 전년 동일 3개월 비교다. 확보되지 않은 과거 업종 필터도 제외하며 종목·KOSPI 추세 조건은 유지한다. 연간 개선이 Q4 개선을 뜻하지 않는다. `dart_history.latest_asof(..., research=True)`와 `--research`만 이 규칙을 사용하고 기존 기본 Q4 검증 모드는 유지한다.

공시 날짜는 [공식 공시검색](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)의 `rcept_dt`를 유지하며 다음 날부터 사용한다. 실응답에서 `rcept_no` 앞 8자리와 접수일이 양방향으로 달랐으므로 접수번호로 날짜를 추정하거나 덮어쓰지 않는다. 자료 연결은 공시 목록·접수본의 회사/종목/기간/공식 날짜 일치, 접수번호에 대응하는 파일명, 저장 방식에 맞는 원본/압축본 해시를 검사한다.

수집·연결 도구(연구 전용, 주문·백테스트 실행 없음):

- `scripts/kis_history.py --output <prices.sqlite3> --calendar <calendar.json>`: 실행 중인 로컬 `kis-trade-mcp` 내부 연결을 통해 2019~2025 KOSPI/KOSDAQ 원시 일봉을 페이지별 저장한다. 날짜가 있는 `output2`만 사용하고 두 지수의 2020~2025 거래일 일치를 확인한다. `--universe <universe.sqlite3>`는 현재 마스터 대신 확보한 과거 종목 합집합의 원시 일봉을 수집한다. 부분 수집은 마지막 페이지부터 재개한다. 완료는 요청 범위 페이지 순회를 뜻하며 모든 날의 시세·기업행동 보장을 뜻하지 않는다.
- `scripts/historical_universe.py --calendar <calendar.json> --output <universe.sqlite3>`: KIND의 날짜별 ST 회사 대표 종목코드와 공식 회사 수를 대조한다. 현재 이름·업종·상장일·수출 파일의 현재 시장값은 저장하지 않는다. 당시 조회 시장을 사용하고 알파벳 포함 코드를 보존한다. 양 시장이 검증된 날짜만 완료다. 2개 동시 작업과 공유 요청 간격을 사용하며 실패/차단 시 중지한다.
- `scripts/dart_history.py ... --compact --research --budget-file <counter.json>`: 접수본의 XBRL 인스턴스만 압축 보관한다. 원본 ZIP 해시·저장 ZIP 해시·인스턴스 해시가 별도다. 링크베이스/XSD는 보관하지 않으므로 전체 원본 ZIP을 복원할 수 없다. 기본 전체 ZIP 보관은 유지한다. 누적 요청 예산은 재시작해도 초기화되지 않으며 실제 계정의 일일 잔여량을 뜻하지 않는다. `parse_corp_codes()`의 현재 코드 연결표는 과거 상장 종목 목록을 대체하지 않는다.
- `scripts/research_dataset.py --universe <universe.sqlite3> --prices <prices.sqlite3> --dart-root <archive-dir> --decision-date YYYY-MM-DD --output <coverage.json>`: 원본/인스턴스 해시를 확인하고 재파싱한 공시를 직전 거래일 종목 목록·판단일 이전 가격과 연결한다. `--dart-root`를 반복해 기존 자료를 재사용한다. 최신 접수본이 목록에만 있고 미수집 또는 금액 충돌 상태여도 이전 긍정 신호로 대체하지 않는다. 격리 여부도 원본에서 재계산하며 `financial_quarantined_receipts`로 집계한다. 첫 판단일에 직전 거래일 종목 목록이 없으면 사용 불가로 표시한다.

시세 대량 수집의 `--direct-rest`는 선택 사항이다. 같은 KIS 컨테이너 안의 기존 키와 유효한 토큰 캐시로 공식 시세 GET 두 경로만 조회한다. 매번 MCP가 예제 코드를 내려받는 비용을 줄이며, HTTP 세션을 재사용하고 요청 간격을 제한한다. 토큰이 없거나 만료되면 중단하며 새 토큰·계좌 조회·주문·설정 변경은 하지 않는다. 날짜 있는 `output2` 계약과 원시 가격 설정은 동일하고 기본 MCP 연결도 유지한다. 연구 달력은 검증된 1,473일·시작/종료 경계를 요구하며 동일 날짜 파일을 재생성해 재개용 해시를 바꾸지 않는다.

공시 전체 배치는 `scripts/dart_history.py --universe-db <universe.sqlite3> --start 2019-01-01 --end 2025-12-31 --output-dir <dataset-root> --compact --research --budget-file <counter.json> --request-limit <누적상한>`으로 실행한다. 모집단 옆의 `calendar.json`과 메타데이터, 정확한 2,946개 날짜/시장 조합, 건수·편입 해시·중복을 재검증한다. 완료 회사는 결과 파일과 원본 저장 해시를 로컬에서 확인하고 공시 목록 API를 다시 요청하지 않으며, 미완료 회사부터 이어간다. 여유 공간 3GiB 미만이나 API/요청 예산·I/O 오류에서 중단하고 `dart-full-collection-status.json`에 완료 회사·중단 위치·미매핑 코드를 남긴다. 디스크 자체가 쓰기 불가능하면 상태 저장을 보장할 수 없으므로 실행 오류도 확인해야 한다. 누적 상한은 실제 계정의 일일 잔여량이 아니며 자동 초기화나 자동 반복 실행은 없다.

공시 연결 시 sidecar의 회사·종목·접수번호·기간·공개일을 원 공시 목록과 대조하고, 접수번호 날짜와 접수일도 확인한다. ZIP 내용만 맞더라도 날짜를 앞당기거나 다른 종목으로 연결한 metadata는 거부한다.

`coverage.json`은 실제 확보한 종목·공시와 누락 수를 구분한다. 60일 시세가 있어도 액면분할·감자·권리·배당·합병·상폐 대금/수량의 검증 장부를 대신하지 못한다. 원시 가격의 관련 플래그는 확인 대상으로만 남긴다. 전체 범위 커버리지와 기업행동 처리가 끝나기 전에는 `strategy_backtest_ready=false`이며, 일부 생존 종목 표본을 전체 전략 성과로 보고하지 않는다. 이번 데이터 준비와 전략 조건 고정에는 백테스트 수익률 계산이 포함되지 않는다.

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
- `judge-review.json` (종목별 `opposing_view.increase_case`/`reduce_case`로 내부 대립 관점 검토를 감사 가능하게 기록)
- `reviews/<stage>--<agent_role>--<task_name>.md` (review agent별 사람 확인용 companion Markdown)
- `account-before-order.json`
- `order-lifecycle.json` (Judge 전 active 주문, 같은 날 이전 cash 주문의 최신 KIS 상태, 계좌 수량 일치 여부)
- `execution.json`
- `pipeline-summary.json` (`review_summary`, `report_path` 포함)

사람이 읽는 최종 포트폴리오 보고서는 run directory 밖의 `reports/YYYY-MM-DD_포트폴리오.md`에 둔다.

## 평결 입력

`decision-brief.json`은 Main agent가 수집 결과를 합쳐 만든 canonical review input이다. `analyst-review` sub-agent에는 launcher가 `decision-brief.json`에서 파생한 role-scoped `review-core` slice를 전달한다. 이 slice는 해당 execution agent가 산출해야 하는 view들의 입력 profile union만 보존한다. `judge-review`에는 `analyst-review.json` 전체가 아니라 selected-symbol analyst-review slice를 전달하고, 보유/가격 정보는 함께 전달되는 `review-core`에서 읽는다. `judge-review`의 `review-core`에는 launcher가 `account-before-order.json`의 broker 평균매입가(`pchs_avg_pric`)에서 직접 계산한 `position_cost_context`(평균매입가, 매입금액, 보유수량, 현재 판단가, 평균매입가 대비 괴리율)를 종목별로 추가한다. 이 필드는 `decision-brief.json`에는 저장되지 않고 `judge-review` stage에서만 그때그때 합성되므로, `analyst-review`의 review-core에는 구조적으로 나타나지 않는다. `judge`는 `position_cost_context`를 손익·리스크·포지션 조정의 참고 정보로 활용하고, 최종 방향과 목표 노출은 종목별 `opposing_view`(increase_case/reduce_case)로 대립 근거를 직접 비교한 뒤 thesis, 시장 근거, 포트폴리오 위험을 함께 고려해 확정한다.

`decision-brief.json`에는 종목 식별자, eligibility, `evidence_mode`, 가격, 핵심 price/chart signal, compact 기간봉/분봉·호가·체결·수급 요약, top-level `market_index_snapshot`과 `market_news_context`, financial/ETF summary, 종목별 `symbol_news_summary`, 계좌 노출, 누락/오류 사유를 압축해서 담는다. `market_news_context`는 직전 완료 거래 run 이후 현재 run까지 저장된 국내·해외·거시·지정학 기사 중 중복 제거된 최대 30건이며 종목별로 복사하지 않는다. `analyst-momentum-cycle`과 `judge`는 개별 종목과 명시적으로 연결되는 기사만 사용하고, 시장뉴스 자체를 자동 주문 신호로 취급하지 않는다. `analyst-news-flow`는 종목별 KIS `symbol_news_summary`만 사용한다.

재무/뉴스가 없더라도 식별자, 종목명, 현재가 또는 직전 거래일 가격 snapshot, 관측시각이 있으면 `eligible_for_review=true`로 유지한다.

같은 날짜 financial/`symbol_news` cache는 top-level `symbols` 키가 전체 universe를 덮으면 그대로 사용한다. 종목뉴스 coverage는 cache date와 article date가 일치하는 비어 있지 않은 기사만 센다. stale-only 종목뉴스 캐시는 full-universe cache로 보지 않고 deterministic collector를 한 번 실행한다. `market_news`는 scheduler가 15분마다 별도 SQLite 공간에 누적하며 daily-trading run은 네트워크 재수집 없이 DB를 읽는다. `news_context`는 직전 완료 거래 run 시각을 시작점으로 하되 최대 72시간으로 제한하고, URL과 정규화 제목으로 두 뉴스 범위의 중복을 제거한다.

`analyst-review`는 canonical score 관점 4개(`analyst-quality-value`, `analyst-risk-allocation`, `analyst-momentum-cycle`, `analyst-news-flow`)를 유지하되 실행 sub-agent는 2개다. `analyst-quality-risk`가 `analyst-quality-value`와 `analyst-risk-allocation` view를 서로 독립적으로 산출하고, `analyst-momentum-news`가 `analyst-momentum-cycle`과 `analyst-news-flow` view를 서로 독립적으로 산출한다. `analyst-quality-value`는 주식의 usable 재무 요약 또는 ETF/ETN의 usable ETF 요약이 전혀 없으면 `no_financial_excluded`, `analyst-news-flow`는 cache date와 article date가 일치하는 usable 뉴스/공시 요약이 없으면 `no_news_excluded` 감사용 row로 보존하되 analyst-review 평균 모수와 judge-review 입력에서 제외한다. 현재가·등락률·업종명 또는 ETF 거래량만 있는 요약은 quality/value evidence로 보지 않는다. 모든 score는 JSON 정수 `0..10`이어야 하며 비정상 값은 중립 5로 대체하지 않고 검증 실패로 처리한다. Optional 영역의 누락은 포함되는 view의 근거가 얇거나 Judge 근거가 불충분하다는 뜻으로 사용하지 않는다. Main agent가 가격, 보유, 뉴스, active 주문 상태의 안정성을 증명할 수 있는 unchanged symbol만 직전 유효 run의 review row를 병합할 수 있다. 증명할 수 없으면 재평가하고, sell/stop-loss 후보, 당일 체결, 가격 급변, 신규 뉴스, active 주문, 비보유 top-K 순위 경계 근처 종목은 반드시 재평가한다. Launcher 자동 wrapper 재사용은 같은 spec fingerprint에 한정된다.

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

## 스케줄 daily-trading 실행

각 `schedules.yaml` 호출은 전체 증거 수집, Analyst, Judge, 주문 계획을 실행한다. `daily_trading.exchange`는 `AUTO`, `KRX`, `NXT`, `SOR` 중 하나이며 기본값은 `KRX`다. `AUTO` 즉시주문은 KIS `search_stock_info`의 NXT 거래대상·KRX/NXT 거래정지 값을 사용해 NXT 가능 종목을 `SOR`, 그 외 종목을 `KRX`로 정한다. 주문일 여부는 KIS `chk_holiday`를 날짜별로 캐시한다. 즉시주문 제출 직전에 KRX는 08:20~15:30, NXT는 08:00~08:50·09:00:30~15:20·15:30~20:00, SOR는 08:00~20:00 범위인지 다시 확인한다. `demo-submit`/`real-submit`은 판단 전에 주문 lifecycle preflight를 수행하고, 명시적 `--submit-orders`와 제출 직전 계좌·현금·보유수량·활성주문 검증을 통과한 주문만 제출한다. scheduler와 Telegram 실행은 하나의 프로세스 잠금으로 직렬화한다.

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
- `execution.json`은 거래소, final holding delta, gate decision, active-order reconciliation, submitted/skipped/blocked order result, sanitized error를 기록한다.
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
