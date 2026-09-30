# 설정 참조

기준은 [README §12](../README.md)와 배포 밖에 두는 세 YAML이다. 비밀값은 사용자 요청에 따라 별도 `secrets.yaml`로 읽는다. 전체 키·기본값은 [app.yaml](../config/app.yaml),
[strategy.yaml](../config/strategy.yaml), [schedules.yaml](../config/schedules.yaml)에 있다.
현재 기본값은 `offline` / `research`이며 실제 연결·운용 승인은 포함하지 않는다.

## 로딩과 변경

```sh
danta --config-dir ./config config validate
danta --config-dir ./config config diff --against ./config.snapshot.json
```

`--against`는 이전 세 설정을 묶은 JSON snapshot이다. `config diff`는 값을 비교하며
승인이나 재시작을 수행하지 않는다. 상대 `app.state_dir`는 설정 디렉터리의 부모를 기준으로
해석한다. 환경변수로 전략 비율을 덮어쓰는 경로는 없다.

자동 운영 배포의 `model.model_id`·`model.reasoning_effort`는 다음 AI 호출부터 다시 읽는다.
Luna·Terra·Sol·Astra 전환에 재시작이 필요 없고, 진행 중인 호출과 그 기록은 이전 값을 유지한다.
새 모델은 처음 사용할 때 로컬 격리 검사를 통과해야 한다. 나머지 설정 변경과 별도 hash 승인
실행에는 이 예외를 적용하지 않으며, 기존 정책 변경 차단을 유지한다.

후보 목록은 `danta candidates add|remove|exclude|include <ticker>` 또는 승인된 Telegram
명령으로 변경한다. 현재 검증된 universe 안의 후보 범위만 변경하며 SQLite에 범위/hash,
account version과 변경 journal을 보존한다. 세 YAML의 위험 정책을 덮어쓰지 않는다.
offline 이외에서는 `candidate_control` capability가 필요하고, Telegram에는
`telegram_control`도 필요하다. 보유 수량을 바꾸거나 제외 종목을 자동 매도하지 않는다.

demo/live activation은 config hash/code ID/approval ID를 저장한다. 매 mutation에서 이
일치와 execution.enabled를 확인하므로 다른 설정/코드에 이전 activation을 재사용할 수 없다.

[config.py](../src/danta/config.py)는 누락/미지/중복/merge key, 잘못된 자료형,
unsafe YAML tag, 잘못된 비율·참조를 거부한다. 비율·금액은 YAML 문자열과 Decimal로
다루며 bool에 `"false"` 같은 문자열을 쓰지 않는다. 실행마다 canonical JSON과 설정·전략
hash를 고정한다. 파일이 바뀐 진행 실행은 기존 hash로 새 권한을 얻지 못한다.

실행에서 지원하지 않는 정책값은 로딩 시 거부한다. 추세 청산은 연속 2회 종가,
진입 시간은 연속장 시작 20분 후부터 종료 30분 전까지다. 야간·주말 보유 허용,
공식 근거 필수, 지수 필터, 미체결 주문 반영, 낙폭 시 신규 위험 중단은 고정 계약이다.
`watchlist`는 빈 목록을 유지하고 후보 변경 명령을 사용한다. 수수료·세금·운영비는
검증된 비용 자료/평가 manifest로 지정하며, 미사용 profile 비용 참조와 기본값 이외의
simulation slippage 값은 거부한다. 평가 비교군·60/30/20 표본·bootstrap·stress 조건도
현재 사전 등록 프로토콜과 다른 값을 거부한다. `max_holding_sessions`는 새 진입 가설에
그대로 저장하고, 기존 보유의 최초 체결 시각과 보호 조건은 재검토로 덮어쓰지 않는다.

## app.yaml

| 영역 | 기본값 / 의미 | 외부 실행에서 필요한 확인 |
|---|---|---|
| app | `offline`, `Asia/Seoul`, 계좌 별칭 null, `127.0.0.1:8080` | 실제 모드/계좌와 bind/수신 경계 |
| model | `codex_cli`, `gpt-6-astra`/`xhigh`/`chatgpt`, timeout 180초 | 다음 호출에서 모델·추론 설정 적용. 실제 로그인·모델 접근·격리 증거·모델 사용 승인 검증 |
| model 재시도 | transient 1회/5초, schema 교정 1회, fallback false | quota reset 미확인은 운영자 확인, 다른 모델 자동 교체 없음 |
| broker | `kis`, 환경/manifest/rate-limit null | 모의/실전 endpoint, 계좌 귀속, 제공자 필드·한도 검증 |
| broker 비밀 참조 | `KIS_ACCOUNT_REF`, `KIS_APP_KEY`, `KIS_APP_SECRET` 이름 | 값은 private secrets.yaml에서만 읽으며 정책 YAML·image에는 넣지 않음 |
| market | OpenDART, 공식 IR 목록 빈 값, calendar/corporate action null | DART 권한, 승인 도메인, 실제 세션·기업행위 출처 |
| execution | enabled false, single writer true | 실행 활성화와 승인 capability가 함께 필요 |
| monitoring | enabled false, 호가/활성주문 5초, idle계좌 60초 | 최신성/호출량/보호 실행 가능성 검증 |
| telegram | enabled/ingress false, sender/chat 빈 목록, route `trading-engine`, `default_chat_id: null` | 같은 Docker 네트워크·송신/수신/제어 승인 및 허용 sender/chat. 송신이 활성화되고 허용 chat이 여러 개면 자동 알림·리포트 수신지를 `default_chat_id`로 지정 |
| storage | SQLite, 로컬 filesystem 필수, raw 보존 기간 null | writable 로컬 상태 디렉터리·백업/복원, 보존 정책 |
| observability | redaction/attempt usage/structured events true | 실제 운영 검증을 합성 성공으로 표시하지 않음 |

`null`은 0·무제한·자동 승인을 뜻하지 않는다. 기본 offline fixture에는 실제 모델·인증·계좌·
수수료 공급 설정이 필요 없다. `CodexAdapter`의 명시된 fixture runner와 DART fixture
transport도 이 경우에만 내부 `FIXTURE_ONLY` 표지를 사용한다. 외부 분기에는 적용하지 않는다.
`telegram.default_chat_id`는 허용 chat 목록 안의 문자열이어야 한다. 생략한 기존 설정은
`null`로 읽으며, 허용 chat이 하나면 그곳으로 자동 알림·리포트를 보낸다. 명령 응답은 요청한 chat으로 보낸다.

실제 adapter 구성과 manifest 필드는 [runtime-contract.md](runtime-contract.md), gateway
연결과 mount 절차는 [runbook.md](runbook.md)를 따른다. image에는 고정 Codex CLI가 포함되며, 실제 인증과 외부 egress는 별도 운영 설정이다.

## strategy.yaml

`strategy.id=catalyst_trend_swing`, `active_profile=research`, 연구 상태는
`unproven_hypothesis`다. 아래 값은 연구 가정이며 개인 자금·손실 승인이 아니다.

| 연구 영역 | 정확한 기본값 |
|---|---|
| 자본/모집단 | 10,000,000원, KOSPI/KOSDAQ 보통주, KRX 정규 연속장, 레버리지/공매도 false |
| 이력/유동성 | 완료 일봉 최소120, 최근20 거래대금 **중앙값** 5,000,000,000원 이상, spread 최대30bps |
| 사건 | earnings_quality/official_guidance/material_contract, 최대5세션, 원문 필수 |
| 특징 | SMA20/60, SMA20 기울기5세션, RS20 > 0, ATR14 단순 평균 true range, 지수 종가≥SMA60 |
| 추격/기회기간 | 전일종가+0.75ATR, SMA20+2ATR 상한, 3~20세션 |
| 진입 위험 | 건별 NAV의0.25%, 합계 계획위험1%, 최대5기업 |
| 진입 비중 | 종목20%, 업종40%, 총80% |
| 축소 발동 | 종목25%, 업종50%, 총90% 초과, 유효관측2회·최소5초 간격 |
| 주문 참여/갭 | 계획/ADTV 최대0.0005, gap buffer 0.50ATR |
| 초기 보호 | low5/2ATR 상한, 최소 stop 거리0.75ATR |
| 추적/추세 | MFE 1R부터 2ATR trailing, stop 비하향, 연속2완성 종가<SMA20 청산 |
| 기한/재진입 | 첫 체결 세션 포함20세션, 동일사건 재진입 전 완료1세션·이전 진입가 회복 |
| 낙폭 | 현금흐름 보정10% 도달 시 신규위험 중단·매수잔량 취소·기존보호 유지, 자동 재개 false |
| 주문 | 검증된 ask 지정가 매수, 유효세션 시장가 매도, 주문/결정 유효120초, 호가 최대5초 |
| 비용 | 수수료/세금/운영비 출처 null, 합성 슬리피지 방향별10bps, 왕복마찰/초기R 최대0.15 |
| 실험 | 주 비교군 technical_only, 추가 cash/event_flag_no_ai, discovery60세션/종료30thesis, confirmation20세션 |
| 통계 | 비순환5세션 moving block, 2,000회 paired bootstrap, 신뢰수준0.95, 슬리피지2배 스트레스 |

물타기·pyramiding·가격변동만의 비중 재조정·자동 재호가·자동 live 승격은 false다.
미확인 비용을 0으로 채우지 않는다. 명시된 synthetic fixture의 가상 비용과 실제 비용을
구분한다. 전략 산식/경계의 기준은 [README §3~6](../README.md)이며 표는 이를 대체하지 않는다.

`live_mandate`는 pending_user 상태이며 자본/필요시점/계좌소유/기존보유·주문인수/
accepted_strategy_hash/완전한 risk policy/밤보유/주문종류/비용배분/신뢰승인 ID가 모두 null이다.
live에서 research 값을 자동 상속하지 않는다. 실제 승인에는 완전한 정책, 일치하는 코드·모델·
prompt·설정 hash, 계좌·소유·단일writer·저장소·복원·격리·비용·달력·호가·보호 증거가 필요하다.

## schedules.yaml

스케줄은 기본 disabled이며 `app.market.calendar_manifest`를 참조한다.
휴일/지연 개장/단축장은 고정 평일 시간이 아닌 검증된 해당 세션 개장·종료시각을 사용한다.

| job | 트리거 |
|---|---|
| finalize_day | 연속장 종료+30분 |
| portfolio_review | 연속장 개장+20분 |
| disclosures | 세션 중180초 |
| material_event | 검증사건, 기업/사건 중복 최대30초 합치기 |
| risk_monitor | 시장사건 + app의 polling fallback |
| reconcile | 주문사건 + app의 polling fallback |
| holding_deadline | 연속장 종료10분 전 |

신규 재량 진입은 개장20분 뒤부터 종료30분 전까지다. 보호/대사 polling 간격은
app.yaml에만 둔다. 재시작 시 기한 지난 매수를 catch-up하지 않고 대사·보호를 우선한다.
pause/schedule_off는 새 재량 심사를 멈추며, 공시 수집·일일 NAV 확정과 재시도·리포트 및
이미 승인된 보호/대사는 계속한다. 전역 scheduler.enabled=false는 보호/대사 외 스케줄을 비활성화한다.
자정 뒤 일일 리포트도 NAV를 확정한 거래 세션의 날짜와 거래 기록을 사용한다.

## 비밀값 검사

[safety.py](../src/danta/safety.py)의 공통 검사는 명백한 credential 필드·literal 패턴을
모델 전달, report 작성, 문서 공유, image source 검사에 적용한다. 이름만 있는 환경 참조와
빈 값/null 예시는 통과한다. 모델 snapshot의 별도 schema/종목 범위 제한은 보고서에
그대로 적용하지 않는다. 오류 출력은 분류와 검사 경로만 남긴다.

```sh
PYTHONPATH=src python -m danta.safety src schemas migrations prompts README.md scripts/render_report.py tests/fixtures/offline-e2e.json pyproject.toml requirements.lock
```

검사는 임의 비밀 문자열·압축/암호화된 모든 값을 알아내는 도구가 아니다. image의 명시적
파일 allowlist, 설정/인증의 image 밖 보관, 모델의 권한 격리를 함께 유지한다.

## secrets.yaml

[secrets.yaml.example](../config/secrets.yaml.example)을 같은 디렉터리의 `secrets.yaml`로
복사하고 문자열 값을 채운다. 권한은 0600 또는 읽기 전용 0400이어야 한다. 환경변수 자동
fallback은 없다. 기존 `*_env` 필드는 이 파일의 키 이름을 가리킨다.

- `KIS_ACCOUNT_REF`: 계좌번호 8자리와 상품코드 2자리를 하이픈으로 연결한 문자열.
- `KIS_APP_KEY`, `KIS_APP_SECRET`: 승인된 KIS 환경의 앱 인증 정보.
- `DART_API_KEY`: OpenDART 인증 정보.
- `TELEGRAM_GATEWAY_URL`: gateway 연결 주소. Docker 배포에서는 `http://telegram-gateway:8080`.
- `DANTA_CODEX_AUTH_HOME`: 전용 Codex 인증 디렉터리 경로. 동일 Compose의 로그인과 엔진이 `/app/auth`를 공유한다.
  `codex login`이 `trading-engine-auth` Docker volume에 인증을 생성·갱신한다.

공백 값은 미설정이다. 파일 누락/형식 오류 메시지에는 입력값을 넣지 않는다. offline
명령은 이 파일을 읽지 않는다. 실제 값은 Git/Docker context/정책 snapshot/보고서에서 제외한다.
파일을 변경하면 서비스를 재시작하여 반영한다. 발급 access token은 수동 설정하지 않으며
`app.state_dir/kis-token.json`에 만료시각과 함께 0600으로 저장된다. 조회 단계에서 갱신하고,
주문/취소 단계에는 준비된 캐시만 대기 없이 읽는다. 갱신 필요·잠금 경합이면 전송 전에 차단한다.
