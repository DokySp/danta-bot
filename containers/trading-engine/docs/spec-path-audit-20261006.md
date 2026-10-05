# 명세와 실제 호출 경로 재감사 — 2026-10-06

기준 소스는 `48d3f1c77339c38e492b036608cc9deadd068dd3`이다. 요구사항은 README와
그 이후 사용자가 명시적으로 승인한 운영 변경을 함께 적용했다. 승인된 live 설정을 과거
offline 기본값으로 되돌리거나 모델 timeout 1200초를 연구 기본 180초로 되돌리지 않았다.

## 판정 방식

- **로컬 검증**: 합성 외부 응답을 사용하는 실제 생산 코드 경로와 회귀 assertion이 통과했다.
- **부분 구현**: 코드가 처리하는 범위가 요구사항 전체보다 좁다. 표에 빠진 범위를 명시한다.
- **운영 미검증**: 실제 공급자·NAS·계좌에서 확인하지 않았다. 이미지 게시와 별개다.
- **연구 미검증**: 미래 관측·경제적 성과·운영비·독립 표본이 충족되지 않았다.

함수 존재나 테스트 수만으로 전체 기능을 완료 처리하지 않는다. 입력 → 어댑터 → Application
→ 주문/원장 → CLI·Telegram·스케줄 보고서와 재시작 경로를 구분한다. 아래 표의 로컬 검증은
실거래 허가·실제 수익성 또는 모든 외부 형식을 지원한다는 뜻이 아니다.

## 반복 누락의 원인과 조치

1. 생산자가 만드는 입력과 검증기가 받는 계약을 따로 검사했다. `6996fd3`의 새 심사 필드가
   실제 모델 입력 검증에서 거절되었고 `16aa7e4`에서 수정됐다. 앞으로 계약 회귀는
   `freeze_input()` 등 실제 생산자 결과를 그대로 넘긴다.
2. 같은 기능의 진입점을 함께 닫지 않았다. `fc316a0`의 서비스 보고서는 보호 주문을 포함했지만
   CLI는 심사 결과만 모았다. 이번에는 공통 수집기를 사용하고 두 진입점 결과를 비교한다.
3. 단위 assertion을 외부 기능 완료의 근거로 확대했다. IR 어댑터 메서드, 기업행위 계산,
   단일 writer 잠금이 존재해도 수집 호출자·운영 CLI·공급자 데이터 경로는 별도 검증이 필요했다.
4. 운영 변경 이후 문서의 기본값과 완료 기록이 따라가지 않았다. 현행 설정·연구 fixture·
   과거 검증 기록을 분리하고, 이 문서에서 현재 상태를 다시 판정했다.

Ponytail 사용과 지침은 이전 대화 감사에서 확인했지만, 이 결함들이 플러그인 하나 때문에
발생했다는 인과관계는 확인되지 않았다. 코드에서 확인된 원인은 호출 경로 누락, 계약 불일치,
형제 진입점의 부분 수정과 검증 범위의 과장이므로 그 지점을 수정했다.

## 확인된 9개 항목의 수정과 검증

| 항목 | 이전 동작 | 실제 경로와 변경 | 검증과 경계 |
|---|---|---|---|
| 첫 체결 전 매수 무효화 | 보유가 0이면 position 심사·보호에서 빠짐 | `Application.review → freeze_input`에 pending 포함, `protect → cancel → reconcile` 후 실제 체결분 보호 | `test_engine` pending-only 및 취소 중 2주 체결 회귀. 수정 전 실패 재현, 수정 후 통과 |
| 장중 낙폭 평가 | 마감 회복 시 장중 손실이 최종 평가에서 사라짐 | `_risk_observation → PaperLedger.record_nav → performance/evaluate_metrics` | `test_evaluation_risk_replay` 장중 낙폭 회복 후에도 실패/중단 유지 |
| 비동기 시세 집중 한도 | 여러 종목의 서로 다른 관측 시각 때문에 유효 관측을 구성하지 못함 | 공통 평가 시각과 종목별 원 시각 분리, stale 거부·동일 관측 중복 집계 방지 | 서로 다른 시각의 2종목 초과 확인·축소 체결, stale/같은시각/무관시세 회귀 |
| 운영 문서의 offline 안내 | live 설정에 합성 실행 명령을 안내 | runbook에 `--config-dir tests/fixtures/config` 명시, 설정표·README·HTML 동기화 | 실제 config와 README YAML 블록 일치. 운영 설정 파일 변경 없음 |
| Telegram 첨부 소유·재전송 | 같은 채팅의 다른 사용자가 대기 파일을 소비할 수 있음 | route/chat/sender 소유 확인, 최초 wire 요청·첨부 선택 영속, 같은 update 동일 재전송 | 실제 Gateway→캐시→TelegramAdapter/SQLite; 다른 sender, `/new`, caption, 응답 유실·재시작·캐시 실패 회귀 |
| 공시 수집 지연 | 느린 심사와 같은 worker에 줄 서서 수집 지연 | `Service.start`에 독립 disclosures worker | 실제 worker 시작 후 모델 review를 막은 동안 공시 수집 완료 |
| 운영 CLI 두 번째 writer | 실행 중 서비스가 있을 때 CLI가 새 Application을 생성 | private Unix socket으로 기존 Application 호출; 생성 전부터 종료 후까지 owner lease | 실제 CLI→UDS→App/SQLite와 `serve` 수명주기, 시작/종료 경합·timeout·UID·권한 회귀 |
| CLI 일일 보고 누락 | 심사 밖 보호 매도/체결이 daily 보고에서 빠짐 | `daily_report.collect_daily_report`를 CLI·서비스에서 공유 | 실제 보호 SELL 체결의 CLI JSON 포함 및 원격/단독 보고 결과 비교 |
| 공식 IR 수집 호출자 | `read_official_ir`만 있고 실제 수집 경로가 없음 | 확인된 DART 회사 원문의 직접 링크→허용 HTTPS 도메인→원문 cache→`freeze_input`→model tools | 실제 어댑터/수집/SQLite/모델입력 validator/도구 9개 회귀. 빈 도메인 설정은 여전히 비활성 |

독립 `gpt-6-luna/max` 검토에서 모델 설정 재로딩 후 CLI hash 거부와 원문 입력 고정 누락을
추가로 확인했다. CLI는 기존 설정 검증이 허용하는 모델 ID·effort·timeout 변경만 받아들인다.
원문은 body를 제외한 manifest와 원본/도구 텍스트 hash를 입력에 고정하고, 모델 도구에 나중
자료를 끼워 넣거나 캐시 내용·메타데이터를 바꾸면 거부한다. 사후 심사와 **주문 직전 및
예약 후 최종 전송 검사**에서 종목별 원문 manifest를 다시 대조한다. 마지막 전송 경계의
추가·삭제·변조 누락은 실제 실패 4개를 재현한 뒤 브로커 제출 0건으로 검증했다.

주요 회귀 파일:
[엔진](../tests/integration/test_engine.py), [서비스](../tests/integration/test_service.py),
[CLI](../tests/integration/test_operator_cli.py), [평가](../tests/unit/test_evaluation_risk_replay.py),
[IR](../tests/contract/test_official_ir_runtime.py), [원문 고정·최종 주문 검사](../tests/contract/test_frozen_documents.py),
[gateway](../../telegram-gateway/tests/test_telegram_gateway.py).

## README 전체 절의 경로 점검

| 명세 | 생산 경로·산출물 | 현재 판정 |
|---|---|---|
| §1 목표·완료 의미 | `doctor/status`, 평가 결과, approval/activation | 로컬 검증·운용 승인·성과를 구분. 실운영과 수익성은 미검증 |
| §2 해결할 문제·설계 요구 | 전략/위험/실행/성과 모듈, 실제 입출력 계약 회귀 | 설명과 거래·원장·성과 결과를 분리. 누락 경로는 위 9개 수정 및 아래 한계에 반영 |
| §3 후보·사건·특징·진입 | `ExternalRuntime → MarketBundle → assess_entry → Application.review` | 합성 경로 검증. 실제 모집단·공시 완전성과 달력 공급은 운영 미검증 |
| §4 비용·수량 | `size_entry → Executor.reserve/submit`, 계좌 비용 evidence | 로컬 검증. 실제 정산·비용 공급 품질은 별도 |
| §5 보유·청산·재진입 | `start_monitor → protect → evaluate_exit`, thesis·재진입 검사 | 미체결 누락 수정. 실서버 보호 지연/거래소 결과 미검증 |
| §6 세션·심사·지연 | `SchedulePlanner → Service`의 control/review/disclosures worker | 모델 대기 중 공시수집 회귀 추가. 실제 공급자 지연/부하는 미검증 |
| §7 구조·책임 | adapters → runtime → application → executor/store/report | 같은 App을 CLI와 서비스가 공유. 기존 모듈 수리로 해당 결함 해소 |
| §8 데이터·모델 계약 | `freeze_input → CodexAdapter/MarketTools → validate_decision` | pending 대상·IR 메타데이터를 실제 생산자 입력으로 검증 |
| §9 주문·복구 | `Executor`, SQLite reservations/journal, Store account writer | 취소·체결 경합/중복·UNKNOWN 로컬 검증. 실제 전원 차단/브로커 복구 미검증 |
| §10 Codex 실행 | `CodexAdapter`, attempt 결과, timeout/retry/quota, sandbox | mock process·계약 회귀. 실제 모델 품질/요금·현재 NAS 실행 증적 아님 |
| §11 외부 연결·Telegram | KIS/DART/IR runtime, Gateway→Service, outbox | 송수신 fixture 경로 검증. IR은 허용 도메인 직접 링크 텍스트에 한정 |
| §12 설정·모드 | `load_config`, 승인된 live mandate, deployment effective config | 현행 값 동기화. 운영 config·승인 범위 변경 없음 |
| §13 원장·성과 | `reconcile → record_nav → accounting.performance`, replay ledger | 기업행위/현물 이관의 운영 자동 처리는 부분 구현. 아래 한계 참조 |
| §14 보고 | common daily collector, `reporting`, frozen outbox document | 보호 매도 포함·원문 hash 회귀. 과거 날짜 보유는 현재 현황 |
| §15 전략 실험 | `replay_manifest/evaluate_manifest`, 독립 비교군·stress·bootstrap | 장중 낙폭/집중도 회귀 추가. 실제 과거 데이터·forward 표본·경제성 미검증 |
| §16 인수 항목 | 아래 70개 ID와 [acceptance](acceptance.md)의 개별 assertion | 로컬 범위만 판정. 모든 ID가 외부 운영 완료라는 뜻이 아님 |
| §17 권한·중단 | config/approval/activation, provider 불명 시 차단 | 기승인 항목 재질문 없음. 미확인 기업행위 비율/귀속은 추정하지 않음 |
| §18 CLI·산출물 | cli/operator, docs, source, tests, README report | 실제 진입점·보고 통합, tracked 산출물 갱신 |
| §19 전환·복구 | deployment scripts, startup requirements, readiness, account adoption | 릴리스는 이미지 게시까지. NAS 활성화는 사용자 작업이며 이번 검증 아님 |
| §20 외부 규격 | 기존 endpoint/contract adapters와 fixture | 이번 작업에서 공식 규격 전체 재인증·실제 공급자 호출하지 않음 |
| §21 시작 지시 | 현재 승인 범위·실제 검증 결과 | 이번의 과거 대화/코드 감사 요청이 최초 구 소스 금지 지시보다 우선 |

## 남은 부분 구현과 미검증 범위

- **기업행위·현물 이관:** replay는 확인된 정수비 분할·배당·거래정지를 처리하고 미지원
  합병/상폐 등을 품질 오류로 남긴다. live `corporate_action_source`는 null이며 실제 수량 변화를
  분할·이관으로 확정하여 원장/thesis/보호 기준에 반영하는 자동 경로는 없다. 수량 불일치는
  `OWNERSHIP_RECONCILIATION_REQUIRED`로 신규 위험과 정확 성과를 막는다. 이 차단은 완료된
  기업행위 처리나 자동 복구를 뜻하지 않는다. 검증 가능한 공급자 원기록과 수량/현금 귀속이
  있어야 해당 기능을 완성할 수 있다. 명세 §13.2/E02/E07은 운영 전체 기준으로 부분 구현이다.
- **공식 IR:** 현재 도메인 목록은 비어 있다. 확인된 DART 원문의 직접 링크 중 허용 도메인만
  지원하며 PDF·추가 페이지 탐색은 지원하지 않는다. 원문 공개시각/재무 사실을 임의 확정하지
  않는다. IR은 읽기 도구의 보조 원문이며 proposal의 구조화된 fact/event 근거 ID로 자동
  승격하지 않는다. 실제 기업별 IR 응답과 운영 도메인 선택은 미검증이다.
- **성과 평가:** 현금/현물 외부흐름 계산 단위검증을 replay manifest의 모든 외부흐름 생성 경로
  검증으로 확대하지 않는다. 실제 과거 시점 자료·독립 60세션/30thesis·운영비가 확보되지 않아
  `STRATEGY_UNPROVEN`을 유지한다.
- **배포:** 로컬 테스트·격리 이미지 검사·registry 게시·NAS 설정·실행 버전·실제 거래를 각각
  구분한다. 이번에는 실주문, NAS 재시작, 실제 Telegram 발송 또는 사후 운영 검증을 하지 않는다.

## 유지할 완료 기준

새 기능/수정은 해당 명세 ID, 실제 입력·진입점, 변경된 주문/원장/보고 결과, 실패 회귀,
검증 범위와 운영 미검증 항목을 함께 기록한다. 전체 재작성은 현재 결함을 수정할 수 없거나
원장이 명세 상태를 표현할 수 없다는 증거가 생겼을 때 다시 판단한다. 이번 9개 결함은 기존
모듈을 수정하고 공유 경로를 연결하는 방식으로 회귀 검증할 수 있었다.

## S/O/E/I 70개 인수 항목 연결

각 ID의 상세 assertion/test 이름은 [acceptance.md](acceptance.md)를 유지한다. 아래의 기본
`로컬 회귀`는 전체 543개 검사에 포함된 합성/단위 검증이며, 외부 완료 판정이 아니다.

| ID | 명세의 기대 결과 | 실제 호출·검증 경로 | 결과의 범위 |
|---|---|---|---|
| S01 | 신규 진입 불가 | `Application.review → strategy.assess_entry` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S02 | 사건 1개, 새 진입/재심사 근거 중복 없음 | `ExternalRuntime._events → EventRegistry/parse_official_event` | 공시 원문 합성 회귀; 실제 원문 형식 전체 미검증 |
| S03 | 서로 다른 coverage/대기 사유 | `ExternalRuntime._events → coverage → assess_entry` | 수집 실패 분리 회귀; IR 도메인 미설정 |
| S04 | 이력 부족. 21개 종가와 산식 검증 | `MarketBundle → market.calculate_features` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S05 | 특징 확정 금지·품질 오류 | `MarketBundle → market.calculate_features` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S06 | ACCEPT 출력이어도 신규 진입 차단 | `Application.review → assess_entry` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S07 | WAIT_PRICE, 자동 추격 정정 없음 | `assess_entry → Application._preflight` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S08 | 진입 거부 | `initial_stop → size_entry` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S09 | q_risk 50, 다른 한도 32면 최대 32 | `Application.review → portfolio.size_entry` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S10 | 거래 강제 없음 | `size_entry → Executor.reserve` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S11 | 신규 진입 보류/차단, 보호 매도는 별도 | `portfolio.validate_costs → size_entry / Application.protect` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S12 | KEEP, 목표비중 재조정 없음 | `Application.review → thesis/entry_plan_completion_allowed` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S13 | 모델 없이 보호 의도 생성 | `start_monitor → protect → evaluate_exit → Executor` | 합성 보호 회귀; 실제 감시 지연 미검증 |
| S14 | stop 비하향, 완성 봉 기반 계산 | `protect → update_trailing_stop` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S15 | 다음 허용 세션 추세 청산 | `protect → evaluate_exit → completed bars` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S16 | 기한 청산. 기간 연장/시계 리셋 없음 | `SessionCalendar → evaluate_exit` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S17 | 재진입 거부. 보호/대사는 제한받지 않음 | `Application.review → reentry_eligibility` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S18 | 다른 모든 조건 통과 시 새 thesis 가능 | `Application.review → reentry_eligibility` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S19 | 새 공식 해소 사건 없으면 재진입 금지 | `validate_decision → reentry_eligibility` | 근거 연결 회귀; 실제 모델 판단 품질 미검증 |
| S20 | 기존 계획 완료만 가능, 물타기 차단 | `entry_plan_completion_allowed → Executor` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S21 | 20%에 맞추는 새 매매 없음 | `protect → ConcentrationMonitor.observe` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| S22 | 20% 목표 축소 1개 계획, 중복/되사기 없음 | `protect / replay → ConcentrationMonitor.observe` | 비동기 시세 replay/축소 체결 회귀 추가 |
| S23 | 신규 확대 중단·매수 잔량 취소·보호 유지·자동 재개 없음 | `record_nav → performance → DrawdownCircuit / replay` | 장중 낙폭 회복 후에도 중단·평가 실패 유지 |
| S24 | 오래된 매수 결정 폐기, 위험 경로 우선 | `refresh_decision → decision_fresh → _preflight` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O01 | offline, 외부 모델/브로커 호출 0 | `fixture config → CLI.make_application → offline Application` | fixture offline 한정; production은 승인된 live |
| O02 | 설정 오류, 조용한 fallback 없음 | `CLI/serve → load_config` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O03 | submit/cancel/replace 0 | `activate/_authorize → Executor.submit/cancel` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O04 | frozen hash 유지, 새 거래 의미는 재검증/승인 | `Config.assert_current → Application.review/_authorize` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O05 | 영속 중복 제거 | `Gateway frozen request → TelegramAdapter.receive → Store receipt` | gateway 재시작/응답 유실 회귀 추가; TTL 이후 엔진 충돌 검사 |
| O06 | 두 번째 writer 차단 | `CLI/serve → OperatorLease → Application → Store writer lock` | 실제 CLI→UDS→단일 App, 시작/종료 owner 경합 검증 |
| O07 | 새 필요 수량2, 예약 중복 없음 | `Application.review → portfolio snapshot → Executor.reserve` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O08 | 새 주문 과다 생성 없음 | `Application.review request/thesis → Executor.reserve` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O09 | 응답 대기 중 감시 지속, stale 매수 무효화 | `Service independent workers + Application.start_monitor` | 모델 대기 중 독립 공시수집/보호 회귀 |
| O10 | 상태에 맞는 대사, 모호하면 UNKNOWN | `Service._recover → Application.reconcile → Executor.reconcile` | 로컬 복구 회귀; 실제 전원 차단은 미검증 |
| O11 | 자동 재전송 0 | `Executor UNKNOWN → reconcile; transport POST no retry` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O12 | 누적 체결 유지, 실제 잔량만 취소 | `Executor.cancel/reconcile → cumulative fill ledger` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O13 | 이중 반영 없음, 보정 이벤트 | `Executor.reconcile → Store cumulative/correction ledger` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O14 | 완전성 false, 신규 쓰기 차단 | `KIS pagination → account_snapshot → Executor.reconcile` | 페이지 누락 합성 회귀; 실제 계좌 미검증 |
| O15 | 전략 수량만 사용, 불명 상태 질문 | `Runtime ownership mapping → Executor.reconcile` | 귀속 불명 차단; 기업행위 후 자동 수량 복구 미구현 |
| O16 | 실제 세션 적용, 오래된 매수 catch-up 없음, 기한 보호 우선 | `SessionCalendar/SchedulePlanner → Service.queue_tick/protect` | 전달된 특수 세션 회귀; 실제 자동 공급의 특별장 미검증 |
| O17 | 잔량 취소/대사, 체결분 보호, 자동 재주문 없음 | `Executor.expire/cancel → reconcile → protect` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O18 | QUOTA_EXHAUSTED, JSON 오류 재시도 0 | `CodexAdapter process events → quota circuit` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O19 | 주문 연결 거부 | `CodexAdapter process/event/final/semantic validation` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O20 | 새 정상 결정으로 재사용하지 않음 | `CodexAdapter timeout → process-group termination → attempt paths` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O21 | 제한된 횟수, attempt별 usage/결과 보존 | `CodexAdapter bounded attempts → runtime usage journal` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O22 | 보호·기한 청산·대사 독립 동작 | `Application.start_monitor/protect independent of model` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| O23 | MONITOR_DEGRADED, 새 위험 차단·알림 | `refresh_protection/quote_fresh → monitor diagnostics` | stale/실패 회귀; 실서버 지속 부하 미검증 |
| O24 | 실제 권한에서 차단, 모델 출력만 믿지 않음 | `freeze_input/MarketTools scope + model process sandbox` | 로컬 sandbox/도구 회귀; 실제 모델 실행 환경 별도 |
| O25 | 허용 목록·단일 route·기존 제어 승인 검사로 거부 | `Gateway sender cache → TelegramAdapter/Service authorization` | 다른 sender의 첨부 소비 거부 회귀 추가 |
| O26 | outbox만 재처리, 거래 재실행 0 | `Service.outbox_once → gateway send; frozen report body` | fixture outbox; 실제 Telegram 전달 미검증 |
| O27 | 원장·예약·승인·중복 방지 복구 후 대사 | `Store backup/restore + Service._recover/Executor.reconcile` | 로컬 DB/queue 복구; 실제 NAS 복구 미검증 |
| O28 | 검출·배포/공유 차단, 값은 로그에 재노출하지 않음 | `safety scan + report/outbox credential guard` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| E01 | 전체 -9%, 장중 +1.11%와 구분 | `Application.record_nav / replay → accounting.performance` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| E02 | 외부 흐름/내부 손익 분리, TWR/낙폭 왜곡 없음 | `accounting.ExternalFlow/performance; cash reconciliation` | 계산 단위검증; live 현물 이관/모든 replay 외부흐름 경로는 부분 구현 |
| E03 | 해당 전략 수익에 미귀속 | `accounting.strategy_nav / runtime ownership` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| E04 | 비용 이중 차감 없음 | `Store cash/fill fees → accounting/performance` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| E05 | 완료 세션20만 선택, 누락을 0수익으로 채우지 않음 | `finalize_nav/completed_session_returns / replay completed marks` | 누락 세션을 다음 일간 수익에 합치지 않는 회귀 추가 |
| E06 | 정확 TWR 인증 금지, 품질 표시 | `accounting.performance flow coverage` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| E07 | 가짜 수익/유리한 가상체결 없음 | `PaperLedger.corporate_action; live reconcile fail-closed` | 합성 corporate-action 처리; live 자동 공급/보정은 부분 구현 |
| E08 | 후보 오염 검출, 비교 무효 | `EvaluationManifest → validate_candidate_pool` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| E09 | 시각/모집단 오류 또는 평가 한계 명시 | `EvaluationManifest.frozen_contract → timeline/source times` | 타임라인 오류 검사; 실제 역사적 자료는 미검증 |
| E10 | 정밀 성과 인증 거부, 보수 가정 표시 | `EvaluationManifest data-frequency validation → _run` | 로컬 회귀 통과; 실제 외부 동작 증거 아님 |
| E11 | INCONCLUSIVE, 독립 표본 부풀리기 없음 | `evaluate_metrics → session/thesis count + bootstrap` | 부족 시 INCONCLUSIVE 검증; 실제 독립 표본 미충족 |
| E12 | FAIL/보류/선택편향 표시, 자동 live 승격 없음 | `evaluate_metrics → costs/failures/hold reasons` | FAIL/보류 판정 검증; 실제 수익성과 운영비 미검증 |
| I01 | 이 README+공식 규격+새 코드만으로 clean build/offline end-to-end 가능 | `curated Docker context → offline Application` | 이번 전체 Docker 회귀; clean 제품 이미지 검사는 릴리스 스크립트 별도 |
| I02 | SPEC_GAP으로 질문·중단, 구 코드 복원 금지 | `config validation + user clarification workflow` | 작업 규칙과 설정 오류 검사; 모든 명세 모순 자동 탐지 의미 아님 |
| I03 | 사용자 질문 후 구현/위임 중단, 미정값 추정 금지 | `live_mandate + trusted approval/activation` | 미승인 거부 회귀; 현재 전체계좌 운용은 명시 승인됨 |
| I04 | 차단, 명시적 승인된 유효 정책 필요 | `validate_activation → Application.activate` | 설정 프로필은 명시 승인으로 선택; 자동 수익기반 승격 없음 |
| I05 | 모든 절/표/값 일치, 원본 hash 일치, 숨겨진 별도 전략 없음 | `reporting.render_readme → report.html` | 새 README hash/93개 제목 포함 HTML 재생성 |
| I06 | ENGINE_VALIDATED와 STRATEGY_UNPROVEN 분리, live 허가 아님 | `doctor/status/evaluate report status fields` | 상태 구분 회귀; 실운영/수익 검증으로 확대하지 않음 |
