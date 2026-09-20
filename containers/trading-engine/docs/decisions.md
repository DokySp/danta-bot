# 결정 및 중단 기록

## 2026-09-20 — 저장공간 확보 후 독립 작업 재개

사용자가 저장공간 확보 후 재개를 요청했다. 계좌 비용 조건이 없어도 구현 가능한
실시간 시세·시각 증거·NAV·대화·보고서·배포 태그 처리를 이어간다. 이전에 비용 확인을
이유로 모든 독립 작업까지 중단했던 판단은 적용하지 않는다. 실제 계좌의 수수료율이나
보유 원장 인수 완료를 추정하지 않으며, 운영 준비와 로컬 구현 검증을 구분한다.

KIS의 2026-09-09 공지는 9월 14일부터 별도 KRX 애프터마켓(16–20시)을 도입한다.
정규장 마감을 20시로 바꾸면 현재 시장가 청산 계약과 맞지 않으므로 현재 전략은 검증된
정규장만 사용한다. 공식 포털에서 장 구분 필드가 마지막에 추가된 47필드 계약을 확인했고,
이 계약으로 스트림을 구현했다. 46필드인 이전 GitHub 샘플을 최신 규격으로 취급하지 않는다.
외부 시장의 실제 수신/지연 검증이 합성 테스트로 대체되는 것은 아니다.

독립 검토의 비용 미확정 전환 시 상태 버전 누락과 합성 브로커의 0수량 최초체결 기록은
재현 후 수정했다. 반면 실제 최초 체결시각을 전략의 연속매매 허용 구간으로 제한하자는
지적은 채택하지 않았다. `day_order_fill_session_verified`는 영업일 귀속 계약이다.
주문 허용 시각과 이미 일어난 체결의 증거는 구분해야 하며, KRX는 종가 단일가 중 VI가
발동하면 단일가 시간을 연장한다. 같은 검증된 영업일의 실제 체결을 달력의 고정 종료
시각 이후라는 이유만으로 버리면 대사에서 실제 보유를 누락할 수 있다. 날짜 불일치와
미래 체결시각 검증은 유지한다.
근거: [KRX VI 처리 방식](https://global.krx.co.kr/contents/GLB/06/0602/0602020204/GLB0602020204T7.jsp).

## 과거 기록 — 2026-09-20 기존 MCP 유지 및 실제 수수료 확인 대기

**Fact:** 사용자는 기존 MCP를 유지하고 기존 새 엔진 작업을 계속하도록 요청했다.
전체 주식·현금 배정, 기존 보유 인수, 전략 정책 판단 위임은 이미 받은 답이며 재질문하지 않는다.
전체237개 합성/계약 테스트 통과 후 공식 KIS 필드명 오타를 재현·수정하고 관련1개 테스트를
통과했다. 이전 미커밋 변경은 보존했다. Docker 연결은 확인했지만 새 이미지는 만들지 않았다.

**판단 정정:** 정확한 최초 체결 초시각이 없다는 이유만으로 전부 중단할 필요는 없다.
README §8.1과 현재 runtime 계약은 체결시각 품질을 구분한다. 검증된 거래 세션을 유지하고
`FIRST_OBSERVED`를 표시할 수 있다. 실제 비용 보충 자료와 정확 초시각을 묶는 제약은
구현에서 분리할 수 있다. supplement의 `first_fill_at`을 Executor가 Store에 전달하지 않아
관측시각을 EXACT 최초체결처럼 기록하는 오류도 확인했으며 아직 수정하지 않았다.

**미결정/정보 부족:** 실제 계좌의 국내주식 API 적용 수수료율, 우대·유관기관비용 조건,
적용기간. 공식 기간별 손익 API에는 실제 비용 집계 필드가 있지만 주문번호가 없으며,
현재 조사만으로 모든 주문의 정산 금액/범위를 정확히 연결할 수는 없다. 주문별 배분을
임의로 만들거나 과거 한 거래 비율을 계좌 수수료 계약으로 일반화하지 않는다.

**영향:** 비용 조건과 실제 정산 연결을 확정할 수 없어 운영 manifest와 실운영 준비 완료
판정을 보류한다. 사용자 중단 지시와 README §3 비용 조건/§17.2에 따라 구현·위임을
멈췄다. 진행하던 gpt-5.6-luna/max 검토도 중단했으므로 검토 완료나 최종 커밋으로 표시하지 않는다.

**추천/재개 조건:** 해당 계좌에 적용되는 국내주식 API 수수료 안내(수수료율·우대 조건·
유관기관비용·적용기간)를 사용자에게 요청한다. 계좌번호/인증값은 필요하지 않다.
확인 뒤 실제 정산과 시각 품질 분리, 기존 실시간 스트림 연결, 운영 설정과 검증을 이어간다.
운영 배포 및 이미지 push는 수행하지 않았다.

공식 근거:
- [주문 응답의 cnc_cfrm_qty](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_llm/domestic_stock/inquire_daily_ccld/chk_inquire_daily_ccld.py#L39)
- [기존 KRX 실시간 체결 스트림 필드](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_llm/domestic_stock/ccnl_krx/ccnl_krx.py#L65)
- [기간별 종목 비용 집계 필드](https://github.com/koreainvestment/open-trading-api/blob/b4e6249714418aa57833d1cbbbced39cbcc5b125/examples_llm/domestic_stock/inquire_period_trade_profit/chk_inquire_period_trade_profit.py#L19)

2026-09-15 사용자 요청에 따라 프로젝트를 `trading-engine`으로 이름 변경한다.
기존 검증 이미지·로그·비공개 백업은 과거 기록으로 유지하며 Docker 자원을 자동 전환하지 않는다.
이후 HTML 텔레그램 첨부 작업은 별도 변경과 커밋으로 처리한다.

## 2026-09-13T01:59:02.413549+00:00 — Docker 검증 중단

**Fact:** 현재 설정된 Docker endpoint에 연결하지 못했다. 오류는 `Cannot connect to the Docker daemon at unix:///home/uhug/.docker/desktop/docker.sock. Is the docker daemon running?`이다. 따라서 이미지 빌드/실행 검증을 완료할 수 없다. 사용자 요청과 README §17.2에 따라 구현·위임을 중단했으며 이 중단 기록만 작성했다.

**미결정:** 현재 Docker Desktop을 사용할지, 사용자가 지정하는 다른 실행 가능한 Docker context를 사용할지 확인이 필요하다.

**영향:** 컨테이너 검증 및 전체 완료 판정이 보류된다. 이미 생성한 소스와 합성 테스트 결과는 보존되지만 최종 결과로 승인되지 않았다. 현재 로컬 개발 작업 중단은 기존 운영 서버나 계좌 상태 변경을 뜻하지 않는다.

**추천:** 현재 Docker Desktop/daemon을 시작한 뒤 재개한다. 대체 context가 의도된 환경이면 정확한 context를 지정한다. 추천은 아직 적용하지 않았다.

**재개 조건:** Docker 사용 가능 상태에 대한 사용자 응답과 재개 요청. 이후 실제 연결 상태를 확인한다. 실운용 승인·자격 증명 설정·외부 서비스 호출은 별도 범위로 남는다.

## 독립 검토에서 반환된 미판정 지적

**Fact:** 읽기 전용 검토 에이전트가 아래 8개 항목을 보고했다. 메인은 중단 시점까지 이를 채택하거나 기각하지 않았다. 검토 에이전트의 61개 테스트 통과 보고는 지정된 동결 파일 범위의 결과다.

**Guess:** 아래 문제와 우선순위는 검토 에이전트의 가설이다. 재개 후 메인이 실제 호출 경로·명세·공식 가격 규칙을 확인하여 실재 여부, 수정 필요성, 승인 범위를 각각 판정해야 한다. 특히 경계 가격의 이전 유효 호가와 외부 흐름 전후 NAV의 시간 관계는 재현 예만으로 결함이라고 단정하지 않는다.

1. `adapters/codex_cli.py`, `adapters/disclosures.py`: offline fixture adapter 경로가 외부 모델 설정 또는 DART key를 요구한다는 지적. 실제 offline application 호출 여부까지 판정 필요.
2. `adapters/market_tools.py`: ID 기반 조회가 `tool_scope.instrument_ids`를 적용하지 않는다는 지적.
3. `adapters/market_tools.py`: snapshot secret 검사가 일부 정확한 키 이름만 차단하고 별칭/값을 놓친다는 지적.
4. `market.py`: 같은 normalized key, 새 official ID인 정정 공시의 변경 facts가 갱신되지 않는다는 지적.
5. `strategy.py`, `models.py`: 미래의 received_at이 있는 quote가 fresh로 승인된다는 지적.
6. `accounting.py`: 외부 흐름의 before NAV와 직전 관측 NAV의 연결이 불충분한데 EXACT TWR로 판정한다는 지적.
7. `market.py`: 변수 호가단위 previous()의 최소 tick 처리에 문제가 있다는 지적. 검토 예 `(0,1),(2000,5), previous(2000)=1999`는 실제 이전 유효 호가 규칙과 대조해야 한다.
8. `evaluation.py`, `risk.py`: quote 이벤트가 없는 보유 기한 세션에서 청산/기한 초과 판정이 생략된다는 지적.

이 목록은 수정을 완료했다는 뜻이 아니다. 재개 전에는 코드 변경·추가 검증을 진행하지 않는다.


## 2026-09-13 재개 및 1차 검토 판정

Fact: 사용자가 Docker Desktop 실행을 알렸고, 메인이 Docker CLI 29.8.0 / Server 29.7.2 연결을 확인했다. 로컬 검증을 재개했다. 임시 경로 bind가 Desktop daemon에서 보이지 않아 합성 설정을 stdin으로 전달하는 동등한 검증 방법을 사용했다. 다른 daemon/host 또는 기존 컨테이너를 변경하지 않았다.

- 지적 1: 기본 offline workflow는 fixture_decision을 직접 사용하여 전체 offline 불가 주장은 오탐. 개별 fixture 전용 adapter에서도 외부 인증 없이 동작하도록 보완 채택.
- 지적 2, 3: 실제 모델 최초 입력과 ID 도구의 범위·credential 경계를 보완. 모든 임의 문자열의 비밀값 판별을 보장하지 않는다.
- 지적 4: 새 official ID인 정정은 원본을 보존한 별도 증거로 등록하고 correction_of로 연결한다. 동일 원문 반복은 심사를 재발생시키지 않는다.
- 지적 5: 미래 received_at quote를 진입·보호·dispatch에서 거부한다. 원시 체결시각도 수신 이후일 수 없다.
- 지적 6 기각: 서로 다른 시점인 직전 NAV와 외부흐름 직전 NAV는 투자손익으로 달라질 수 있다. README §13.2 식에 따라 100→50, 입금100→150, 이후220의 TWR은 (50/100)*(220/150)-1 = -26.6667%다. before+flow=after와 같은 시각 snapshot 일치 검사는 유지한다. 두 값의 무조건적 동등성 검사는 정상 손익을 거부하므로 추가하지 않는다.
- 지적 7 기각: 가격대 (0,1),(2000,5)에서 2000보다 작은 가장 가까운 유효호가는1999다. 2000의 현재 tick5를 단순 차감한1995보다 가까운 유효가격이 존재한다. 현재 previous()가 반환하는1999를 결함으로 수정하지 않는다. 제공된 합성 band 계약과 기존 경계 테스트로 대조했다. 이번 재개 시 공개 KRX 페이지 직접 조회는 404여서 현재 공식 호가표의 외부 연결 검증으로 주장하지 않는다. 실운용은 별도로 검증된 현재 tick manifest가 필요하다.
- 지적 8: 관측된 timeline 범위 안의 독립 deadline/close timer를 추가. 호가가 없으면 MONITOR_DEGRADED/EXIT_OVERDUE이며 새로운 가격이나 가짜 체결은 만들지 않는다. 후속 실제 호가와 전송 지연 뒤에만 청산 가능하다.

통합 보완: valuation 시각과 원시 가격 관측시각을 구분하고, stale/missing 시 정확 NAV와 신규 위험을 차단한다. 계좌 수량이 알려진 보유의 감시·기한 판단은 계속한다. 집중 축소 목표와 관측상태를 SQLite에 보존하고, 주문 제출 직전 현재 수량 한도·매도가능 수량을 다시 검사한다.

현재 중간 aggregate: 109 tests PASS. 이후 변경·최종 검토 결과는 acceptance.md/status.md에 따로 기록한다.

## 2026-09-13 통합 검토 판정 및 보완

Fact: gpt-5.6-luna/max의 읽기 전용 통합 검토가 7개 연결 문제를 보고했다. 메인은 실제 application/service/runtime/execution 경로와 README를 대조하여 아래처럼 판정했다.

- 결정 시각: README §6.3은 **모델 완료 후120초**다. 모델 실행180초만으로 stale이라는 해석은 기각했다. 완료 후 자료 재조회/계획/dispatch 지연을 포함하지 못한 구현 문제는 채택했다. Application에서 완료시각을 별도 보존하고 이후 현재시각과 비교하며, 주문 만료에도 이 deadline을 적용한다. 180초 모델+즉시 후처리 성공, 완료 후121초 지연 차단, dispatch 중 refresh 후 만료 차단을 검사했다.
- 실행 권한: demo/live의 모든 mutation에서 execution.enabled를 재확인한다. 영속 activation은 단순 bool 대신 config/code/approval ID와 결합하므로 오래된 activation으로 현재 설정의 권한을 얻지 못한다.
- 완료 주문 보정: 실제 snapshot에 다시 나타난 terminal 주문도 누적 체결/평균가/비용 revision을 반영한다. 제한된 과거 조회에서 보이지 않는 terminal 주문을 새 UNKNOWN으로 바꾸지 않는다.
- 공시 trigger: README §6.1의 실제 최초 수집시각을 가진 FIRST_COLLECTED를 허용한다. UNCERTAIN, 미완성 원문, 비공식, 미래시각은 여전히 제외한다.
- 사용량: 실제 model attempt 결과와 최종 outcome을 동일 Store journal에 연결한다. 실패/재시도와 미제공 usage=null을 보존하고 최종 usage가 마지막 attempt의 중복 요약임을 표시한다.
- shadow: 판단/수량 계획 artifact는 정상 완료하며 주문 의도, 신규 thesis, 가상 체결을 만들지 않는다. 보호 판단도 broker mutation으로 보내지 않는다.
- Telegram/CLI: 네 목록 명령은 같은 Application 후보 범위 변경을 호출한다. 기존 universe 안에서만 후보 포함/제외를 바꾸며 실제 보유·전략 게이트는 바꾸지 않는다. SQLite 범위/hash와 계좌 version/journal에 남기고 외부 모드는 candidate_control 권한을 요구한다. Telegram은 추가로 telegram_control을 검사한다. resume도 같은 계좌/귀속/승인/낙폭 검사를 사용한다. reasoning_effort 변경은 실제 설정을 바꾸지 않고 승인 필요 요청으로 보존한다.

외부 API·모델·메신저·실계좌를 호출하거나 승인 파일을 생성한 것은 아니다. 위 경로는 주입된 어댑터와 합성 원장으로 검사한다. 최종 변경 후 독립 검토와 전체/컨테이너 검증 결과는 status.md에 기록한다.

## 최종 재검토 및 종료 판정

Fact: 같은 gpt-5.6-luna/max의 새 읽기 전용 재검토에서 위7개 보완을 한정하여 검사했다. 나머지5개에서 중요한 미해결 문제는 발견하지 않았고, 권한 재확인과 프로세스 시작 실패 기록의 두 경계를 추가 보고했다. 메인도 실제 Application+합성 demo broker에서 refresh 중 execution.enabled 변경 후 전송되는 문제를 재현하여 채택했다.

- execution의 submit/cancel은 영속 상태 저장 후 broker 전송 직전 권한을 다시 검사한다. 제출 거부는 아직 전송하지 않았으므로 INVALIDATED와 예약 해제로 기록한다. 취소 거부는 기존 주문 상태와 예약을 복원한다. 이를 접수불명 UNKNOWN 또는 취소확정으로 오인하지 않는다.
- Codex runner의 OSError는 오류 원문을 보존하지 않고 PROCESS_FAILED로 변환한다. 공통 attempt result와 MODEL_ATTEMPT/MODEL_OUTCOME journal에 usage=null로 남기며 자동 재시도하지 않는다.

추가 회귀: 실제 application 임시 정책 변경 후 broker0/예약0/INVALIDATED, 취소 두 번째 권한검사 실패 시 cancel0/기존 예약 유지, 주입 OSError 시 호출1/시도결과1/journal2/원문 미노출. 최종 전체 **133개 테스트 PASS**다. 독립 검토는 총3회(초기 계약, 통합 경로, 채택 보완의 재검토)이며, 마지막 두 국소 수정은 메인이 직접 회귀와 전체 검증으로 확인했다. 새 전략 정책이나 외부 연결을 추가하지 않았으므로 추가 전역 검토는 수행하지 않는다. 최종 image 재검증 증거는 container-verification.json에 기록한다.


## 2026-09-14 비밀 설정·토큰·배포 연결

사용자 요청에 따라 레거시를 제외한 기존 구현 74파일을 `35db0d2`로 먼저 커밋했다.
비밀값은 `config/secrets.yaml`, 빈 예시는 `config/secrets.yaml.example`로 분리했다.
레거시 env의 주석을 제외해 읽고 기존 계좌 분리 규칙(8자리 + 상품코드 기본01)을 확인한
뒤 KIS 정보와 gateway 주소를 이전했다. 값 일치·0600·Git 제외만 보고하고 원문은 출력하지 않았다.
원본 legacy는 변경하지 않았고 실제 토큰 발급/계좌 연결은 실행하지 않았다.

KIS 캐시를 런타임에 연결하고 구식 deploy/test 경로를 수정했다. image에 고정 native
Codex CLI를 포함하며 runtime Compose에 전용 auth/state/approval mount를 준비했다.
이 변경은 실제 계좌/운영 승인이나 모델 인증을 생성하지 않는다.

읽기 전용 gpt-5.6-luna/max 검토의 주문 만료 지적을 채택했다. 토큰 발급 중 시각 전진으로
preflight 뒤 만료 주문을 보낼 수 있었다. POST는 cache-only/nonblocking으로 바꾸고,
실제 transport 직전(우선순위 대기 후) 결정·호가·세션 시한을 검사한다. 전송되지 않은
제출은 INVALIDATED/예약 해제, 취소는 이전 상태/예약 유지로 구분했다. 메인 회귀에서
캐시 부재·60초 경계·잠금 경합·세 가지 시한 경과·원장 복구와 실제 timeout의 UNKNOWN
유지를 검사했고 전체 193테스트를 통과했다. 이 국소 수정에는 추가 전역 검토를 반복하지 않았다.

원본 README의 env example 트리 지적은 원문 보존 사유로 변경하지 않았다. 해당 파일과
report.html은 전달받은 원 명세의 사본이다. 사용자 후속 요구로 달라진 실행 방법과 파일
구조는 루트 README, config-reference, runbook, runtime-contract에 반영했다.

실제 OpenDART 키/모델 인증·운영 manifest·gateway 검증은 남아 있다. 기존 env에는
OpenDART 키 항목/참조가 없어 secrets.yaml에서 빈 값으로 유지했다. 미확인 값을 만들거나
승인된 것처럼 외부 호출을 실행하지 않는다.

## 2026-09-14 DART 키 제공 후 재개

사용자가 DART 키를 제공하고 검증·진행을 요청했다. 비밀 설정에 0600으로 저장하고
공식 목록 API의 정상 응답으로 유효성을 확인했다. 기업 코드·전체 7페이지 목록·계약
원문 조회까지 수행했다. 앞 절의 DART 키 입력 대기는 해소되었다.

실제 원문은 기존 2열 합성 양식과 달리 3열 병합 셀이다. 명시적 필드 경로만 매핑하고
계약금/지급조건, 본사 계약분·변동 가능성 등의 추가 주석을 함께 보존하도록 보완했다.
다른 양식·실거래 승인·과거 공시의 장중 공개시각으로 일반화하지 않는다.

gpt-5.6-luna/max의 국소 읽기 전용 검토에서 두 지급조건을 합친 문자열은 원시 값의
미정/미공개/공시유보 검사를 우회한다는 지적을 재현·채택했다. 합치기 전에 각 필드를
검사하도록 수정하고 세 경우를 회귀에 추가했다. 다른 중요 지적은 없었다.

다음 모델 연결에 앞서 기존 `/codex-home`의 저장 방식을 확인했다. 예전 base Compose는
named volume을 사용하며 현재 로컬의 `containers_codex-home-stock-v1`에는 `auth.json`이
없다. 인증 부재를 임의의 계정/방식으로 채우지 않고 실제 인증 위치 또는 새 로그인 입력을 기다린다.

## 2026-09-14 로그인 방식 정정

사용자가 기존처럼 `codex login`으로 진행하도록 요청했다. 원 명세는 CLI와 인증 분리를
요구하며 사용자가 auth.json을 직접 준비하도록 요구하지 않는다. 인증 파일 부재를
구현 중단 사유로 삼은 이전 판단을 정정한다. 로그인 전용 Compose와 runtime이 같은
지속 volume을 사용하고 양쪽에서 file credential store를 명시한다. 새 volume의 권한은
image에서 준비하며 기존 인증/볼륨은 변경하지 않는다. 기존 모델/effort와 ChatGPT
로그인 방식을 반영했다. 실제 브라우저 로그인은 운영자가 완료하는 실행 단계로 둔다.

## 2026-09-14 로그인 완료 후 연결 검증과 다음 변경 범위

- 사용자 로그인 후 공유 volume의 ChatGPT 인증, 실제 `gpt-5.6-sol`/`xhigh` 응답과
  frozen 읽기 도구·JSON schema·값 검증을 확인했다. 기존 host 비활성화로 실패한
  첫 요청도 증적에 남겼다. 설치 모델 정보를 반영한 probe와 권한 profile을 적용하고
  전체 196개 테스트 및 Docker CLI 검증, gpt-5.6-luna/max 검토를 통과했다.
- KIS 실전 origin에서 token 발급과 잔고 조회를 각각 1회 확인하고, 0600 캐시를
  새 인스턴스에서 외부 호출 없이 재사용했다. 주문·Telegram 송신·운영 배포는 하지 않았다.
- 다음 Telegram 연결을 조사할 당시 외부 gateway의
  `CodexExecClient.post_message()`는 Content-Type만 보내며 새 수신기의
  `X-Danta-Timestamp`/`X-Danta-Signature`를 만들지 않는다.
- 원 명세 §11.4는 새 codex-exec와 필요한 루트 연결만 변경하도록 정했다.
  gateway 내부 수정은 승인 요청 전에는 적용하지 않았다. 제안 범위는 `telegram-gateway.py`의
  정확한 요청 body 서명·별도 비밀 파일 로딩, `compose.yaml`의 비밀 파일 경로 참조,
  `config/codex-peer.secret.example`, 기존 gateway 테스트와 README의 설정 안내다.
  적용하더라도 메시지 전송·이미지 push·운영 재시작은 별도 실행이다.

## 2026-09-14 gateway 서명 변경 승인 후

- 사용자의 “계속 진행해줘”를 위 gateway 파일 변경 범위 승인으로 반영했다.
  기존 sender/chat/route 처리 위에 정확한 요청 bytes의 HMAC 서명과 별도 private 파일 로딩을
  연결했다. 비밀 파일 경로가 없으면 기존 전송을 유지하고, 지정한 파일이 잘못되면 시작을 거부한다.
- gateway private 파일과 엔진 `secrets.yaml`의 peer 키를 같은 값으로 준비했다.
  두 파일은 0600이며 실제 값은 Git·이미지·증적에 포함하지 않았다. 다른 인증 항목은 보존했다.
- 실제 loopback HTTP의 접수/중복/SQLite 재개방 후 중복 202와 7종 거부 403을 확인했다.
  전체 200개 테스트를 통과했다. 서비스 worker·외부 Telegram 호출·주문은 실행하지 않았다.
- gpt-5.6-luna/max 독립 검토에서도 중요 결함이 없었으며 gateway 테스트와 loopback 결과를
  재확인했다. 메인은 현재 범위에서 추가 코드 변경이 필요하지 않다고 판정했다.
- 이번 세션에는 Docker Desktop 소켓이 없고 서비스가 inactive여서 새 gateway 이미지 및
  컨테이너 간 연결은 검증하지 못했다. 이전 이미지 검증을 이번 gateway 검증으로 대체하지 않는다.

## 2026-09-15 Docker Desktop 재실행 후 검증 완료

사용자의 Docker 재실행 통보 후 새 gateway 이미지를 빌드하고 이미지 내부 32개 테스트,
기존 엔진 이미지와 현재 코드 일치 및 offline smoke, 별도 internal network의 이미지 간
서명 수신과 SQLite 재시작 중복 방지를 확인했다. root 소유 profile을 비root 수신기가
읽고 변경할 수 없는 조건도 실제 파일·mount로 검증했다.

첫 시도는 Docker Desktop에서 `/tmp` 검증 파일을 bind할 수 없어 시작되지 않았다.
해당 시도의 임시 자원을 정리하고 합성 파일만 workspace의 Git 제외 `var/`로 옮겨 통과했다.
제품 코드와 실제 비밀값·운영 설정은 수정하지 않았다. 합성 fixture는 운영 승인으로
사용하지 않으며, 실제 Telegram 송수신·운영 배포·실주문은 남은 별도 단계다.

## 2026-09-15 원격 배포 준비와 커밋 요청

사용자는 배포 스크립트가 이미 이미지 빌드/push를 담당한다는 점을 확인한 뒤,
다른 컴퓨터에서 사용할 gateway 메뉴·Compose·운영 설정 세트·설치/업데이트 절차와
그 검증(1~4번), context-to-git-title을 사용한 커밋을 요청했다.

범위에 맞춰 실제 메뉴와 example을 정리하고, 고정 gateway IP/외부 네트워크 연결,
소스가 필요 없는 새 전달 폴더 생성기와 별도 운영 PC 로그인·권한·백업 절차를 구현했다.
실제 비용·달력·인수·권한 검증이 필요한 example은 미확인 상태로 두었다. 운영자의
정책을 추정하거나 테스트 fixture를 실운영 승인으로 사용하지 않았다.

현재 단계는 로컬 준비·검증 및 Git 커밋이다. 이미지 push/설정 전송/운영 서버 변경은
다음 단계이며 수행하지 않았다. 레거시·실제 비밀값·인증·DB는 커밋에서 제외한다.
