# 원장·독립 paper 평가·보고

구현과 합성 검증은 실제 경제적 성과를 증명하지 않는다. `ENGINE_VALIDATED`는 테스트가
통과한 엔진에 대한 상태이며, `replay` 자체는 `ENGINE_EXECUTED`를 보고한다. 제공 fixture는
`FIXTURE_ONLY`, 전략은 `STRATEGY_UNPROVEN`이다. 실제 계좌·모델·외부 시세를 호출하지 않는다.

## 실행

```sh
danta replay --manifest tests/fixtures/evaluation-synthetic.json
danta evaluate --manifest tests/fixtures/evaluation-synthetic.json
python scripts/render_report.py
python -m unittest discover -s tests/unit -p test_evaluation.py -v
```

`evaluation.replay_manifest(path)`와 `evaluate_manifest(path)`는 JSON 직렬화 가능한 객체를
반환한다. `reporting.write_report(data, json_path, html_path)`가 같은 객체로 양쪽 보고서를
만든다. 수익 수치는 LLM이 작성하거나 재계산하지 않는다. 이 파일은 사용 설명이며 전략의
원본은 README이다. `scripts/render_report.py --source README.md --output report.html`은
원본 전체, SHA-256, 생성 시각, 모든 절 바로가기를 외부 CDN 없이 렌더링한다. raw HTML은
이스케이프하고 표·코드·모바일·인쇄를 지원한다.

## 원장 계산 계약

`accounting.strategy_nav`에는 전략 귀속 현금과 **관리 수량만** 전달한다. 예약 현금도 NAV에
포함된다. 체결 이후 현금에 반영된 수수료·세금·슬리피지를 다시 차감하지 않는다.
`NavPoint`는 연속 평가 시각·NAV를, `ExternalFlow`는 현금/현물 이관의 직전·직후 평가액을
담는다. `performance`는 밤사이 손익과 외부 흐름을 반영한 TWR·낙폭·기간 손익을 계산한다.
배당·이자는 내부 손익이다. 귀속 불명·누락 평가·흐름 불일치에는 정확 TWR과 신규 위험을
허용하지 않는다. `completed_session_returns`에는 공식 캘린더로 확인한 완료 세션 목록을
전달한다. 주말 조회를 샘플로 만들거나 누락을 0으로 채우지 않는다.

금액은 Decimal, JSON 금액은 문자열이다. 수량은 bool을 제외한 정수이다. 실제 영속 원장은
application/store 책임이며 평가의 paper 원장은 실제 계좌와 분리된 재생 상태이다.

## 고정 manifest

`EvaluationManifest`는 실험 ID, code/config/strategy/prompt/tools/data/universe hash,
모델 ID/effort, 사전 고정 시각, 초기 자본, 실제 이용 가능 시각, 비용·지연, 업종 분류 출처,
캘린더·호가단위, 연구 설정, 시도 수와 선택 편향을 기록한다. `data_hash`는 정규화된
`TimelineEvent.model_dump(mode="json")` 목록의 정렬 JSON SHA-256이다. 데이터 변경은 hash
불일치로 거절한다. 외부 데이터에 fixture 비용을 사용하거나 미확인 비용을 0으로 치환할 수
없다. 하나의 manifest 안에서 모델/정책을 바꾸는 review는 거절한다.

입력 형식의 완전한 예시는 `tests/fixtures/evaluation-synthetic.json`이다. 이 파일의 가격,
평일 캘린더, 수수료·세율은 모두 합성이다. 실제 시장/계좌 요율이라고 인용하지 않는다.
프로그램은 fixture를 좋은 성과로 바꾸거나 미래 기간을 만들어 완료하지 않는다.

## 독립 비교군과 체결

각 군은 별도 `PaperLedger`의 현금·보유·thesis·pending·취소·체결 기록을 유지한다.

| 군 | 입력과 판단 |
|---|---|
| cash | 같은 초기 현금, 확인된 이자 정보가 없으므로 무이자 기준 |
| technical_only | AI 이전 모집단, 사건/AI 조건 없이 공통 기술·위험·집행 규칙 |
| event_flag_no_ai | AI 이전 풀의 공식 사건 존재·시각·원문과 공통 기술 조건, 의미 해석 제외 |
| full_strategy | 공통 진입 게이트에 AI 의미·순위·근거 무효화 판단을 포함 |

review는 `pool_stage=UNIVERSE_PRE_AI`, 원래 `pre_ai_candidate_ids`, 전체 Candidate 목록,
당시 Quote/EventRecord, 완료 시각, 전체 모델 판단을 가진다. 모델 판단의 ACCEPT는 경제적
경로·기간 논리·가격 반영 가능성·반증·무효화 조건을 필요로 한다. 풀에서 AI 기각 종목을
제거하면 비교 오염으로 거절한다. 기술 비교군에 사건 기반 재진입이나 AI 의미 청산을
추가하지 않는다. 실제 공개시각 이후 원문과 당시 모집단이 없으면 품질 한계를 표시한다.

비AI 군은 입력 시각, full 군은 심사 완료 시각에 계획하며 각자 전송 지연을 적용한다.
주문이 실행 가능해진 뒤의 실제 관측 호가만 사용한다. 매수 ask/매도 bid, 방향별 슬리피지,
호가단위, 지정가 상한, 상대 호가 수량, 시장 세션을 확인한다. 동일 호가 이벤트를 다시
처리해 유동성을 복제하지 않는다. 정해진 수량의 부분체결·120초 만료와 취소 요청 후
확인 이전 추가 체결을 처리하며, 취소가 완료되어야 반대 방향 보호 주문을 만든다.
최소 수수료는 분할 체결마다 중복 부과하지 않고 주문 누적 금액에서 증가분만 반영한다.

각 군에서 공통 `assess_entry`, `size_entry`, `reentry_eligibility`, `evaluate_exit`,
`update_trailing_stop`, `ConcentrationMonitor`, `DrawdownCircuit`를 호출한다. 새 완성 봉은
review의 `completed_bars`로 전달한다. 보유 중 호가 이벤트가 보호·집중·낙폭을 관측한다.
분할은 관리 수량/가격/보호 기준을 함께 조정한다. 미해결 단주·진행 주문·합병·상장폐지는
품질 문제로 남기고 정상 가격 또는 0원 가짜 청산을 만들지 않는다. 배당의 실제 확인 금액만
현금에 더한다. 일봉만 있는 재생에는 장중 선후관계 미확인과 정밀 성과 인증 불가를 표시한다.

## 판정과 결과의 한계

실험 시작 뒤 첫 완료 **60세션·종료 thesis 30개**를 discovery로 사용하고, 바로 다음
20세션을 정책 변경 없는 confirmation으로 사용한다. 호출 수는 독립 표본이 아니다.
같은 완료 세션의 full−technical 일별 TWR 차이에 5세션 **비순환** moving block을 paired
복원추출한다. 2,000개 원래 길이 표본 평균의 보간 2.5/97.5 백분위와 seed·세션·결측 정책을
기록한다. 누적 초과수익은 일별 평균 차이와 별도로 기록한다. 회사·사건·시장일 상관과
현재 모델의 과거 결과 학습 가능성을 한계로 보고한다.

양의 거래비용 후 수익·주 비교군 초과·운영비 후 수익·bootstrap 하한·10% 낙폭 경계·
자료/집행 coverage·다음 20세션 수익을 모두 검사한다. 운영비 미정, 표본 미달, 선택 편향,
결측, confirmation 미관측은 보류 사유이다. 표본 미달의 최상위 판정은 `INCONCLUSIVE`이며
이미 관측된 음의 경제적 항목은 `failures`에 함께 남긴다. 충분한 표본의 실패는 `FAIL`이다.
확인 구간을 나중에 유리한 날짜로 이동하지 않는다.

스트레스는 동일한 전략 계획 규칙·수수료·세금을 유지하고 **체결 슬리피지**를 2배로
재생한다. 군별 현금·수량·미체결을 다시 운용하므로 체결 경로 차이가 결과에 반영된다.
악화는 승격 보류 사유다. 총시도 수와 최고 결과 선택 여부를 출력한다. 모든 실제 기준을
통과한 향후 paper 결과만 `PASS_REVIEW_ELIGIBLE`이 될 수 있으며, 이것도 사용자 실거래
검토 자격일 뿐 자동 live 승격이 아니다. fixture/과거 재생은 이 자격을 부여하지 않는다.

보고서는 게이트별 사유, AI 결과, 계획/제출/부분체결/만료, 실현·미실현 손익, 비용,
보유 세션·MFE/MAE·청산 사유, 현금/NAV, 독립 비교군, coverage와 운영 상태를 포함한다.
MFE/MAE는 제공된 관측 호가/체결 범위이며 누락된 장중 극값까지 안다고 주장하지 않는다.
