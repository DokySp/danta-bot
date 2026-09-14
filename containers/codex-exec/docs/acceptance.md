# 명세 수용 추적표

기준은 [README §16](../README.md)이다. 아래 경로와 테스트 이름은 구현의 합성 회귀
증적을 가리킨다. 테스트 통과는 실제 KIS/DART/모델/gateway 연결·운영 보호 성능이나
전략 수익 검증을 뜻하지 않는다. 최종 전체 실행/컨테이너/화면 검증 결과는
[status.md](status.md)에 기록하며 이 표에 변하는 전체 테스트 수를 중복 고정하지 않는다.

```sh
PYTHONPATH=src python -m unittest discover -s tests -t . -p 'test_*.py'
PYTHONPATH=src python -m danta.safety src schemas migrations prompts README.md scripts/render_report.py tests/fixtures/offline-e2e.json pyproject.toml requirements.lock
```

`합성`은 해당 fixture 조건에서 재현되는 assertion이다. `사람/운영`은
실제 승인·환경·향후 관측이 필요하다. 외부 검증을 건너뛰고 모드를 올리는 우회는 없다.

## 전략 S01~S24

각 행은 [test_strategy.py](../tests/unit/test_strategy.py)의 `test_sNN_…`에 직접 대응한다.

| ID | assertion 대상 | 증적 |
|---|---|---|
| S01 | 장기 목표가만으로 신규 진입 없음 | 합성: long_term_target_without_event |
| S02 | 반복 기사/원사건 dedup, 정정 증거 보존 | 합성: repeated_story + official_correction |
| S03 | 수집 실패와 검색 완료 무사건 구분 | 합성: coverage_failure_is_not_no_event |
| S04 | 20구간 수익에21종가 필요 | 합성: twenty_returns_need_twenty_one_closes |
| S05 | 미완성 봉/혼합 수정 기준 거부 | 합성: in_progress_or_mixed_adjustments |
| S06 | 결정론적 추세/RS/지수 게이트 | 합성: deterministic_gates_override_accept |
| S07 | 추격 상한 초과 대기, 자동 재호가 없음 | 합성: chase_cap_waits_without_repricing |
| S08 | 초기 stop/ATR/호가단위 경계 | 합성: invalid_initial_stop_and_tick_boundary |
| S09 | 위험수량50과 다른 상한32 | 합성: risk_budget_and_other_cap |
| S10 | 0주·현금부족 강제주문 없음 | 합성: no_forced_one_share_and_exact_nonlinear_costs |
| S11 | 미확인/과다 비용은 진입 차단, 보호 별도 | 합성: unknown_or_excessive_costs |
| S12 | 작은 가격 소음으로 목표수량 변경 없음 | 합성: price_noise_keeps_fixed_quantity |
| S13 | 모델과 독립된 보호·오래된 체결가 거부 | 합성: protection_precedes_model |
| S14 | MFE1R 활성화·stop 비하향 | 합성: trailing_requires_mfe |
| S15 | 각 SMA20 아래 완성 종가2개 | 합성: two_completed_closes |
| S16 | 첫체결 포함20세션·재시작 overdue | 합성: first_fill_included_time_exit |
| S17 | 손절 당일 재진입 차단 | 합성: stop_same_session_reentry |
| S18 | 완료회복세션 뒤 전체 조건 재검사 | 합성: stop_reentry_after_completed_recovery_session |
| S19 | 무효근거의 별도 공식 해소 사건 필요 | 합성: invalidated_contract_requires_distinct_official_resolution |
| S20 | 기존 부분계획 완료·물타기 금지 | 합성: partial_plan_completion_only |
| S21 | 가격상승22%에서 재조정 없음 | 합성: price_appreciation_to_twenty_two_percent |
| S22 | 두 관측·단일축소계획·교차한도 이중매도 없음 | 합성: two_valid_observations + overlapping_concentration_groups |
| S23 | 외부흐름 보정낙폭·매수취소·보호유지 | 합성: drawdown_pauses_entries |
| S24 | 새 부정사건/가격으로 결정 stale | 합성: negative_event_or_price_change |

추가로 실제 수신이 미래인 quote, 예약위험/슬롯, strict bool/NaN/time 경계, 완료 원문의
증거 갱신을 검사한다. [test_disclosure_parser.py](../tests/unit/test_disclosure_parser.py)는
합성 공식 표의 명시된 단위/기간/비교기준, guidance의 예측 표시, 계약조건, 정정 부모를
검사한다. 실제 공시 서식 전체 지원은 인증하지 않았다.

## 운영·보안 O01~O28

기본 application 검사는 [test_engine.py](../tests/integration/test_engine.py), wire/model
경계는 [test_adapters.py](../tests/contract/test_adapters.py), 지속 실행/수신은
[test_service.py](../tests/integration/test_service.py), 실제 경로를 연결한 injected runtime은
[test_runtime.py](../tests/contract/test_runtime.py)에 있다.

| ID | 구현/검사 경로 | 증적 범위 |
|---|---|---|
| O01 | engine `test_O01_I01`; runtime default_offline; adapter defaults | 합성: 기본 모델/외부호출0 |
| O02 | engine `test_O02` | 합성: 중복/미지/string bool/비율 거부 |
| O03 | engine `test_O03_I04`, execution_disabled_and_changed_activation, authorization_change_during_preflight, cancel_rechecks_authority; runtime unapproved_manifest | 합성: mode 한 줄/비활성 실행/이전 activation/전송 전 권한 변경으로 mutation 불가 |
| O04 | engine `test_O04`; frozen Config | 합성: 정책 변경 감지·현재 실행 권한 고정 |
| O05 | engine `test_O05`; service durable receipt/scheduler identity | 합성: 재시작 가능한 요청/update 중복 제거 |
| O06 | engine `test_O06` | 합성: 동일 계좌 두 writer 차단 |
| O07 | engine `test_O07_O08` | 합성: 보유10+pending3+목표15→추가2 |
| O08 | engine `test_O07_O08` | 합성: 새 run의 같은 목표 재예약/과다주문 없음 |
| O09 | engine `test_O09_O22`; service slow_model | 합성: 모델대기 중 보호·pause 및 오래된 결정 폐기 |
| O10 | engine `test_O10_O11`; service restart_recovers_result | 합성: UNKNOWN/reservation 복구; 실제 전원중단 시험은 운영 증적 |
| O11 | engine `test_O10_O11`; adapter post_no_retry | 합성: 접수불명 자동 재제출0 |
| O12 | engine `test_O12_O13`; paper latency/cancel-race | 합성: 부분체결/취소 후 누적량 유지 |
| O13 | engine `test_O12_O13`; runtime known_fill_unknown_fees; contract `test_terminal_corrections.py` | 합성: 중복/역순/평균가·비용 보정, terminal 이후 최신 정정/늦은 누적 체결 반영 |
| O14 | engine `test_O14_O15`; adapter pagination; runtime partial_page | 합성: 불완전 계좌를 COMPLETE로 승격하지 않음 |
| O15 | engine `test_O14_O15`; runtime unknown_ownership | 합성: 전략 외 보유·귀속 불명 신규위험 차단 |
| O16 | adapter scheduler_holiday_short_session; S16; service expired_review/first_collected | 합성: 전달된 실제형 세션시각/만료·최초 수집 사건 적용; 실제 달력 공급은 운영 |
| O17 | engine `test_O17`; paper expiry | 합성: 만료잔량 대사·확정 취소 후 예약 해제 |
| O18 | adapter `test_O18_multiline_quota_without_final_opens_circuit_without_repair` | 합성: 여러줄quota+final없음→QUOTA_EXHAUSTED, 교정/재호출0 |
| O19 | adapter process_event_schema_semantic_layers | 합성: valid final이 있어도 비정상 exit/turn.failed 거부 |
| O20 | adapter `test_O20_timeout_kills_process_group_and_late_final_cannot_be_reused` | 로컬 mockprocess SIGKILL·지연파일 미생성, timeout 이후 주입한 이전final 재사용0 |
| O21 | adapter `test_O21_transient_retries_once_with_attempt_results_and_usage`; runtime decide_records_every_retry_and_failure_for_usage | 합성: transient 최대1회/5초 요청, 연속실패도2attempt까지, 개별결과/명시usage 또는null journal·실제 `/usage` 조회 |
| O22 | engine `test_O09_O22`; 독립 monitor | 합성: 모델 응답 없이 보호·대사 경로 지속 |
| O23 | engine stale_and_missing_holding_quote/future_received_quote; S13 | 합성: stale NAV 인증/신규 dispatch 차단; 실제 rate-limit 지속 부하는 운영 |
| O24 | adapter snapshot_scope/credential_fields; local CLI probe | 합성: 최초 prompt/도구 전 scope/credential 차단, 명령 무권한 |
| O25 | adapter Telegram dedup/conflicts; service body_sender_and_peer/portfolio/resume/effort; engine candidate_controls/resume_and_candidate_changes | 합성: body 자기선언만으로 제어 불가, 목록 변경·resume 권한 검사와 보유 불변 |
| O26 | engine `test_O26`; service outbox_retry | 합성: 알림 재시도에서 거래 재실행0 |
| O27 | engine `test_O27`; service restart recovery | 합성: backup/restore·예약/UNKNOWN 보존; 실운영 복구는 운영 |
| O28 | adapter `test_O28_source_fixture_html_and_shared_document_refuse_credentials`; safety CLI; Docker scanner | 합성: source/fixture/HTML/report/전송 직전 거부, 값 로그노출0 |

O24의 실제 설치 CLI probe는 `tests/fixtures/adapters_cli_probe.py --run`과
`adapters_cli_probe_result.json`에 있다. loopback mock provider/임시 canary만 사용하여
shell/patch 호출 거부, auth canary 미노출, 제한 도구 목록을 확인한다. 실제 모델 사용·
운영 auth mount·외부 통신 검증은 아니다. O28은 명백한 credential 형식 검사이며 임의
비밀값·모든 인코딩을 완전히 식별한다고 주장하지 않는다.

추가 application 회귀는 shadow가 양의 수량 계획을 기록하면서 intents/theses/체결을 만들지
않는지 확인한다. 결정 시각은 README §6.3의 완료 후120초 기준이다. 모델180초 뒤 즉시
처리는 허용하고, 완료 후 refresh121초 지연과 dispatch 중 만료는 차단한다.
프로세스 시작 OSError도 PROCESS_FAILED 시도 결과와 사용량 journal에 남으며, 원문 오류의
합성 비밀 canary는 결과/응답에 노출되지 않고 자동 재시도하지 않는다.

## 성과·실험 E01~E12

각 행은 [test_evaluation.py](../tests/unit/test_evaluation.py)의 `test_eNN_…`에 직접 대응한다.

| ID | assertion 대상 | 증적 |
|---|---|---|
| E01 | 연속100→90→91의 전체 -9% | 합성: overnight_loss |
| E02 | 현금/현물 외부흐름과 배당 내부손익 | 합성: cash_asset_flows |
| E03 | 수동·외국 자산의 전략손익 제외 | 합성: managed_quantity_excludes_manual |
| E04 | 체결가격/현금과 비용 이중차감 방지 | 합성: fill_prices_and_cash |
| E05 | 완료 세션만 선택·누락0수익 금지 | 합성: only_completed_calendar_sessions |
| E06 | 흐름 전후 평가누락의 정확TWR 거부 | 합성: missing_flow_valuations |
| E07 | split/배당/정지/상폐의 가짜 체결 방지 | 합성: split_dividend_halt_and_delisting |
| E08 | AI 통과후 모집단 재사용 거부 | 합성: comparison_pool_must_precede_ai_acceptance |
| E09 | 미래정정/생존편향 가시화 | 합성: future_revisions_and_survivor_universe |
| E10 | 일봉의 장중 선후관계 인증 불가 | 합성: daily_bars_cannot_certify_intraday_order |
| E11 | 세션/종료thesis 독립 표본·부족 보류 | 합성: sample_counts |
| E12 | 음의 결과/미정운영비/선택편향·자동live 금지 | 합성: loss_unknown_operating_cost_selection_bias |

추가 검사는 지연 이후 호가체결/부분체결/최소수수료, 호가 없는 기한/보호의 무체결 상태,
full strategy 전용 의미무효화, 비순환 paired bootstrap 재현성을 다룬다.
메인의 `evaluate` smoke 결과는 `FIXTURE_ONLY`, `INCONCLUSIVE`, discovery1세션/
종료1thesis, cash/technical_only/event_flag_no_ai/full_strategy 네 독립 arm,
`automatic_live_promotion=false`다. 향후60+20세션 성과 자료는 아직 없다.

## 명세 독립성·사람 판단 I01~I06

| ID | 증적 | 남은 구분 |
|---|---|---|
| I01 | engine `test_O01_I01`; image의 명시적 새 source/fixture allowlist | clean Docker build/run 결과는 status에 별도 기록 |
| I02 | Docker 데몬 불가 당시 status/decisions 중단 기록 | 이후 핵심 명세/진행 불가도 사용자에게 즉시 보고하는 작업 규칙 |
| I03 | null live_mandate·trusted approval gate | 실제 자금/인수/밤보유/손실/비용 승인은 사람 판단이며 미승인 |
| I04 | engine `test_O03_I04`; validate_activation 완전 live policy 검사 | 연구값 자동 상속 없음 |
| I05 | evaluation `test_i05_report_preserves_readme_hash_tables_code_and_escapes_raw_html` | 원문 hash/표/코드·native 접힘목차; 최종 화면 QA는 status |
| I06 | evaluation E11/E12, report FIXTURE_ONLY, doctor 상태 | 엔진검증·STRATEGY_UNPROVEN·LIVE_NOT_AUTHORIZED 분리 |

외부 연결, 운영 인수·보호 성능과 경제적 성과에는 아직 사람/운영 증거가 필요하다.
수용표의 합성 assertion 수가 그 승인을 대신하지 않는다.
