# 구현·검증 상태

## 2026-09-20 Telegram 단일 엔진 설정

사용자 요청으로 Telegram route를 `trading-engine`, 봇 설정 파일을 `telegram.env`로
통합했다. 별도 peer 공유키·HMAC 서명·승인 profile과 이를 위한 고정 IP/서브넷 설정을
제거했다. Telegram bot token, 허용 sender/chat, 중복 요청 방지 및 기존 거래 제어
승인은 유지한다. 엔진 HTTP는 호스트에 공개하지 않고 내부 Docker 네트워크로 연결한다.
아래의 gateway 서명 검증 기록은 변경 전 스냅샷이며 현행 설정의 필요조건이 아니다.
Docker 전체 회귀 266개(엔진214/gateway33/도구19), 서명 없는 로컬 HTTP 접수·중복·
미허용 chat 거부, 비공개 배포 묶음의 설정 보존 및 두 Compose 구문 검사를 통과했다.
이 변경은 로컬 구현과 설정 정리이며 서버 파일 전송·이미지 push·원격 재시작을 포함하지 않는다.

## 2026-09-20 저장공간 확보 후 재개

사용자가 저장공간을 확보하고 재개를 요청했다. 아래 중단 기록은 당시 상태다.
수수료 계약을 임의로 확정하지 않고 그 정보에 의존하지 않는 구현·검증을 진행한다.

- KIS 포털의 최신 47필드 `H0STCNT0` 계약으로 날짜·장 구분·호가·잔량을 받는
  백그라운드 WebSocket 연결과 재접속을 구현했다. 수신 시각과 거래소 시각을 구분하고,
  5초 초과·역순·다른 날짜·비정규장 자료를 차단한다. 41개 구독 한도에서 보호 대상을 우선한다.
- 실제 정산비용과 정확한 최초 체결시각은 각각 보충할 수 있다. 최초 체결시각을 원장과
  thesis에 전달하고, 수량/금액 변화가 없는 증거 보충도 버전과 감사 기록에 반영한다.
- 장마감 NAV, 평가 JSON/HTML, 모델 일반 대화와 10턴 세션 기억, 대화 provenance를 구현했다.
  지연된 마감 자료의 재시도와 자정 이후 전일 마감 처리를 보완했다. NAV 저장 직후
  프로세스가 종료되면 보고 요청만 복구하며, NAV나 주문을 중복 생성하지 않는다.
- 두 이미지 배포 스크립트는 회귀 검사와 릴리스 push가 성공한 뒤 동일 이미지를 `latest`로 push한다.

최종 전체 회귀 **263개 PASS**(엔진213/gateway33/도구17), 소스 민감정보 검사30개 파일
실패0, diff 검사를 확인했다. `gpt-5.6-luna/max` 독립 검토에서 확인한 NAV 재시도·자정
처리·보고서 복구·대화 provenance·비용 상태 버전·합성 0체결 시각 문제를 보완했다.
스트림 독립 검토의 계약81개 테스트도 통과했다. 채택하지 않은 체결시각 제한 제안은
실제 거래소의 종가 연장 규칙과 대조한 판정을 `decisions.md`에 기록했다.
실제 KIS 시세 수신·계좌 정산·Codex/Telegram 왕복·운영 배포를 검증한 상태는 아니다.

최종 로컬 검증 이미지 `trading-engine:local-20260920-resume`
(`sha256:2bc68159c4415568247000b6c7654bca74d9d383c97dac090b73ab63c4e0d8f9`)도
UID10001, 네트워크 없음, 읽기 전용 루트, capability 없음, 인증정보 마운트 없음으로
검증했다. doctor·합성 실행·중복 요청 재사용·보고서·기본 서비스 무소켓 검사를 통과했다.
이미지의 code identity는 `68f7705cf54fc89044e578a69f886a9fab6acfab7c15636ed83c2dd0c85ee838`이다.
Docker Hub push, NAS 복사, 운영 서비스 재시작, 실제 주문은 수행하지 않았다.

운영 준비에 남은 항목: 계좌별 수수료 조건 확인, 기존 보유의 승인된 원장/thesis 인수,
실제 정산 자료 자동 연결, 운영 manifest 완성, 외부 연결과 보호 지연 검증.
따라서 현재 구현을 완전한 실운영 준비 완료로 표시하지 않는다. 전략 수익성 검증도 별도다.

## 과거 기록 — 2026-09-20 계좌 수수료 확인 대기

현재 상태는 `WAITING_FOR_HUMAN`이다. 사용자는 기존 KIS MCP를 유지하고 새 엔진 작업을
재개하도록 요청했다. 주식·현금 전체 배정, 기존 보유 인수, 전략 정책 판단 위임은 확인됐으므로
다시 질문하지 않는다. 아래 과거 기록의 정책 미승인 표시는 당시 상태다. 현재 계좌별 실제
수수료율·우대 조건·적용기간은 확인되지 않았으며 운영 설정/승인 파일을 완성하지 않았다.

- 중단 전 작성한 일일 NAV 확정, 평가 JSON/HTML 보존, 일반 대화와 세션 분리,
  릴리스 push 성공 후 `latest` 갱신 변경을 포함해 전체 **237개 PASS**
  (엔진187/gateway33/도구17)를 확인했다. 실제 네트워크/계좌/모델 호출 검증은 아니다.
- 공식 KIS 취소확인수량 필드는 `cnc_cfrm_qty`다. 배포 manifest 예제와 계약 테스트에 있던
  `cncl_cfrm_qty` 오타를 수정했다. 공식 필드만 제공하는 합성 응답에서 기존 매핑의 대사 실패를
  재현하고, 매핑 수정 후 해당 테스트 1개 PASS를 확인했다. 전체237개 검사는 이 수정 전 결과다.
- 정확한 최초 체결 초시각 부재만으로 전체 작업을 중단해야 한다는 앞선 판단을 정정한다.
  명세는 시각 품질을 구분하도록 하며 검증된 체결 세션과 `FIRST_OBSERVED`를 허용한다.
  비용 보충 자료와 정확 시각의 결합을 풀고, 제공된 실제 시각을 원장까지 전달하는 수정은 남았다.
- 기존 KIS `H0STCNT0`의 날짜·시각·bid/ask 필드로 호가 시각 문제를 해결할 기술 경로를
  확인했다. 스트림 연결, 재접속, 신선도 및 장중 제공 품질 검증은 아직 구현·실행하지 않았다.
- gpt-5.6-luna/max 읽기 전용 검토는 사용자 정보가 필요한 중단 조건 확인 후 중단했다.
  최종 검토 완료로 표시하지 않는다. 새 커밋·이미지 빌드/push·NAS 작업·운영 배포는 하지 않았다.

재개에 필요한 것은 해당 계좌의 **국내주식 API 주문 적용 수수료 안내**(수수료율,
우대/유관기관비용 조건, 적용기간)다. 공개 기본 요율이나 일부 과거 거래의 비율을 해당
계좌의 확인된 수수료 계약으로 대체하지 않는다. 수수료 정보만 받으면 배포 완료라는 뜻은
아니며 실제 정산 연결, 운영 manifest, 독립 검토와 운영 연결 검증도 남아 있다.
근거와 재개 조건은 [decisions.md](decisions.md)의 같은 날짜 기록을 따른다.

현재 프로젝트 이름은 `trading-engine`이다. 아래 날짜별 검증 기록의 이전 이미지·경로·hash는
당시 실제 증적이므로 유지했다. 현재 실행 경로와 명령은 [runbook](runbook.md)과
[배포 안내](../deployment/README.md)를 따른다. 사용자 요청으로 명세의 프로젝트 이름만
변경하고 HTML을 다시 생성했다. 투자 전략·승인·Codex CLI 계약은 바꾸지 않았다.

Fact: README P1~P5의 **오프라인 구현과 로컬 엔진 검증을 완료**했다.
2026-09-15 사용자 재실행 후 Docker 연결을 복구했다.
최신 운영 준비 결과와 실제 연결에 필요한 항목은 아래 날짜별 검증 기록을 따른다.
당시 중단·재개와 리뷰 판정은 [decisions.md](decisions.md)에 보존했다.

## 구현 범위

README P1~P5의 Python/SQLite workflow, 전략·위험·보호·주문·대사·복구,
KIS/DART/Codex/Telegram 어댑터, 독립 paper 비교 평가, CLI/service,
Docker/Compose와 보고서·운영 문서를 구현했다. 기본은 offline이며 모델/외부 통신·
실주문 권한·스케줄·Telegram ingress는 활성화하지 않았다.

- IMPLEMENTED: 소스·설정3개·schema·migration·합성 fixture·문서.
- ENGINE_VALIDATED: 아래 합성/격리 검증 범위. 독립 검토 지적을 판정·반영하고 최종 회귀를 완료했다.
- EXTERNAL_INTEGRATION_UNVERIFIED: DART 조회, Codex 로그인·단독 모델 호출, KIS 인증·잔고 조회를 아래 범위에서 검증했다. 전체 운영 runtime과 gateway 연결은 미검증이다.
- STRATEGY_UNPROVEN: 향후60+20세션 결과 없음. 합성 실험은 INCONCLUSIVE.
- LIVE_NOT_AUTHORIZED: 실제 자금/계좌·보유 인수·밤 보유·손실·실행 정책 미승인.

## 2026-09-13 초기 구현 검증

| 검사 | 결과·범위 |
|---|---|
| 전체 unittest | **133개 PASS**, 2026-09-13, 실제 네트워크 금지 합성/계약 테스트 |
| source/배포 fixture credential 검사 | 36파일 PASS; Docker context는 Dockerfile/.dockerignore 포함38파일 PASS |
| 자격증명 주입 negative build | 합성 credential을 넣은 별도 context가 COPY/의존성 설치 전에 거부됨, 값 출력 없음 |
| 최종 Docker build | PASS, `danta-codex-exec:spec-20260913-final` |
| 컨테이너 실행 | UID10001/read-only/network none/capabilities 없음/no-new-privileges |
| 컨테이너 기능 | doctor VALID, 합성 FIXTURE_FILLED, 동일 요청 재사용, holding1, 보고서, 기본 serve 소켓0 |
| Compose config | 비root/read-only/호스트 포트·Docker socket·host network 없음/internal network, secret 파일 부재 기본 허용 |
| 설치 CLI 계약 probe | Codex CLI0.153.4 + loopback mock provider. 제한 도구·shell/patch 거부·임시 auth canary 미노출 |
| README/report | 원문과 byte 동일, 92개 절, SHA256 일치, 외부 script/stylesheet0 |
| 화면 | 데스크톱·390×844 모바일·목차 확장/절 이동 PASS, 문서 가로 넘침 없음 |
| 새 파일 whitespace 검사 | 74파일, 지적0 |
| 독립 검토 | gpt-5.6-luna/max 3회, 채택한 문제 수정. 마지막 국소 수정은 메인 회귀+전체133개+이미지 재검증 |

재실행 명령(새 프로젝트 디렉터리에서 Python3.12 + requirements.lock 환경):

```sh
PYTHONPATH=src python -m unittest discover -s tests -t . -p 'test_*.py' -q
PYTHONPATH=src python -m danta.safety src schemas migrations prompts README.md scripts/render_report.py tests/fixtures/offline-e2e.json pyproject.toml requirements.lock
docker build -t danta-codex-exec:local .
PYTHONPATH=src python scripts/verify_offline_image.py --image danta-codex-exec:local
```

컨테이너 검증 완료: `2026-09-13T02:52:19.246243+00:00`.
이미지 ID: `sha256:8c12f677a5ba1fe5692f523abdea3744ae20e5d33c144b638120933a7080f22c`.
호스트/이미지 공통 code ID: `4cf9800a634cb78258feb511a91ce9a03b6a5a3d8946b7732a1f669d655e5e68`.
원문 SHA256: `26d74f328784dcd461dc3aedf0f58d7e60855e41ee9179b0cc6fe8ec51be7006`.

상세 증적: [container-verification.json](container-verification.json),
[report-verification.json](report-verification.json),
[CLI probe](../tests/fixtures/adapters_cli_probe_result.json),
[S/O/E/I 추적표](acceptance.md).

## 실행하지 않은 범위

아래에 기록한 단독 연결 검사 외 전체 운영 runtime·배포 gateway peer 검증·실계좌 주문·Telegram 전송,
이미지 push·NAS sync·운영 컨테이너 변경·Git push는 하지 않았다.
초기 구현은 2026-09-14 사용자 요청으로 `35db0d2`에 커밋했으며, 이후 운영 준비 변경은 작업 트리에 남아 있다.
기존 legacy 소스/설정/원장/인증 파일도 삭제하거나 변경하지 않았다.

P6은 승인된 데이터/모델·인프라에서 실제 관측 기간을 쌓아야 하며,
P7은 별도 사용자 정책과 운영 인수·보호 성능 검증이 필요하다.
이 단계들을 자동 배경 실행하거나 완료한 것으로 표시하지 않는다.
실제 공시 서식 전체 지원, 현재 운영 호가표/달력, 비용·호출량·보호 지연도 미검증이다.

검증 중 만든 합성 원장만 `/tmp/danta-preserved-synthetic-validation-iy8c3gnm/research`에
보존했다. 기본 offline 상태는 다음 실행 시 새로 생성된다. 임시 화면 검증 서버/탭은 정리했다.

## 2026-09-14 운영 준비 후속 작업

- 완료: 레거시 제외 초기 커밋 `35db0d2`; 별도 secrets.yaml 로딩·example·Git/image 제외.
- 완료: 기존 env에서 KIS 앱 키/시크릿/계좌·gateway 주소 이전, 값 일치/0600 확인. 원본 legacy 보존.
- 완료: KIS 자동 발급·만료 60초 전 갱신·재시작 캐시 재사용·동시 발급 방지. POST는 캐시만 대기 없이 사용.
- 완료: 우선순위 대기 후 실제 전송 직전 결정·호가·세션 시한 재검사; NOT_SENT 제출/취소 원장 구분.
- 완료: 기존 deploy 명령의 테스트 경로·build context·버전 metadata 수정, experimental 호환 wrapper.
- 완료: 고정 Codex CLI 0.153.4 native image, 별도 runtime Compose와 auth/state/approval mount 준비.
- 검증: 전체 **193 PASS**(엔진150/gateway28/scripts15), Docker build/격리 엔진/컨테이너 CLI probe PASS.
- 검증: gpt-5.6-luna/max 읽기 전용 검토의 주문 만료 지적을 채택·수정하고 메인에서 경계 회귀와 전체/image를 재검증.

최신 증적: [runtime-preparation-verification.json](runtime-preparation-verification.json).
인증 없이 loopback fixture만 호출한 probe이며 실제 모델·계좌 연결 성공을 뜻하지 않는다.

남은 작업은 실제 KIS 데이터·비용·달력·호가·호출량·보호 지연 검증, DART의 다른 원문 양식 검증,
승인 manifest, Telegram gateway 인증·호환 연결, 운영 원장/잔고/미체결
전환과 레거시 정리, P6 실제 관측 평가 및 P7 사용자 실거래 정책 확정이다.

## 2026-09-14 OpenDART 실제 조회

사용자가 제공한 키를 `config/secrets.yaml`에 저장하고 실제 `list.json`의 `000` 응답과
공시 1건으로 인증을 확인했다. 이어 기업 코드 3,990건, 2026-09-11 공시 683건/7페이지,
계약 공시 원문 1건을 기존 어댑터로 조회했다. 키는 출력·증적·Git·이미지에 포함하지 않았다.

실제 원문 `20260911800002`의 3열 병합 셀을 지원하도록 계약 파서를 보완했다. 금액·최근
매출·기간·계약금/지급조건을 추출하며, 본사 계약분과 금액/기간 변동 등 원문 주석도
보존한다. 불명확한 통화·필드·중복·누락 조건은 추정하지 않는다. 다른 실적/전망/정정
양식과 전체 운영 런타임 검증을 의미하지 않으며, 과거 공시 조회는 장중 공개시각의 증거가 아니다.

증적: [dart-read-verification.json](dart-read-verification.json). 실제 조회는 사용자가
허용한 DART 진단에 한정하며 기본 offline 모드와 운영 승인 상태는 변경하지 않았다.

당시 기존 로컬 Docker volume에 `auth.json`이 없어 사용자 입력을 요구했으나, 기존처럼
`codex login`으로 생성할 수 있으므로 **구현 중단 사유로 삼은 판단을 정정한다**.
아래 로그인 경로로 직접 인증을 생성할 수 있다. Telegram 서명 헤더와 컨테이너 연결
보완도 남아 있다.

검증: 전체 **195 PASS**(엔진152/gateway28/scripts15), Docker build/격리 엔진 PASS.
gpt-5.6-luna/max 읽기 전용 검토에서 지급조건의 미정/미공개/공시유보 처리를 채택해
원시 필드 단계에서 거부하도록 수정하고 관련 회귀 및 전체 검증을 다시 수행했다.

## 2026-09-14 Codex 로그인 경로

`compose.auth.yaml`에서 `codex login --device-auth`를 실행하고, 생성된 인증을 runtime과
같은 `codex-exec-auth` volume에 보존한다. 새 volume은 UID 10001·0700으로 초기화한다.
거래 설정·승인·DB는 로그인 컨테이너에 필요하지 않으며 기존 인증 파일은 필수가 아니다.
기존 모델 `gpt-5.6-sol`/`xhigh`와 사용자가 지정한 ChatGPT 로그인 방식을 설정에 반영했다.
로그인과 판단 subprocess는 같은 `/app/auth` 및 file credential store를 사용한다.

원 명세 §10.1/10.4/12.1은 CLI 사용·인증 경계·외부 실행 설정 확인을 요구하며, 인증
파일의 수동 작성을 요구하지 않는다. 브라우저 로그인 완료와 실제 모델 호출 검증은 별도
실행 단계다. 기본 offline·거래 비활성·운영 승인 검증은 유지한다.

검증: 전체 **196 PASS**(엔진153/gateway28/scripts15), Docker build/격리 엔진/제한 CLI
probe PASS. 네트워크 없는 임시 volume에서 CLI가 가짜 인증을 0600으로 저장하고 새
컨테이너에서 재사용·쓰기 가능한지 확인했다. 실제 로그인이나 모델 성공 증거로 해석하지
않는다. [로그인 준비 증적](codex-login-verification.json)

gpt-5.6-luna/max의 로그인 변경 범위 읽기 전용 검토에서 중요 결함은 발견되지 않았다.

## 2026-09-14 로그인 완료 후 실제 연결

- ChatGPT 로그인: 공유 volume `codex-exec-auth`에서 native `codex login status` 성공.
- Codex: `gpt-5.6-sol`/`xhigh` 실제 응답, frozen 읽기 도구 1회, 엄격한 JSON/schema/값 검증 **SUCCESS**.
- 최초 모델 요청은 Code Mode host 비활성화로 `PROCESS_FAILED`였다. 실제 모델 정보를 사용하는
  로컬 probe로 원인을 재현하고 bundled host·6개 읽기 도구 제한·파일 권한 profile을 반영했다.
  실패를 성공으로 무시하거나 다른 모델로 전환하지 않았다.
- 최종 회귀 **196 PASS**. 새 Docker 이미지와 외부 네트워크 없는 CLI 격리 probe PASS.
  auth canary는 노출·변경되지 않았다. 숨겨진 파일 읽기 handler는 이번 Docker의 sandbox 시작
  차단으로도 거부되었으며, 운영 환경의 같은 경로가 검증됐다는 뜻은 아니다.
- gpt-5.6-luna/max 최종 읽기 전용 검토: 이번 변경 범위의 중요 결함 없음.
- KIS: 실전 origin에서 토큰 발급 1회·잔고 GET 1회, `COMPLETE`. 새 캐시 인스턴스에서
  네트워크 호출 없이 토큰 재사용, token 파일 0600 확인. 검사는 호스트 Python 3.12에서
  수행했으며 Docker 운영 연결 검증으로 표시하지 않는다. 종목별 주문 가능 자원은 `NOT_QUERIED`.

증적: [Codex 로그인·호출](codex-login-verification.json), [KIS 인증·잔고](kis-read-verification.json).
최종 이미지: `danta-codex-exec:login-20260914-verified`,
ID `sha256:62392bdb8af3e893f9b0b493470fe48ddbd2f8a8056f758bcf1e8df96e83a7ee`.

현재 모드는 계속 offline이다. 상시 외부 운영의 모드·승인 유효기간은 아직 정하지 않았다.
실계좌 잔고 조회를 기존 보유의 새 전략 인수나 거래 승인으로 사용하지 않는다.

## 2026-09-14 gateway 요청 서명과 로컬 수신 검증

사용자 승인 범위로 기존 gateway에 private 파일 기반 HMAC 서명을 연결했다.
gateway `config/codex-peer.secret`과 엔진 `config/secrets.yaml`의 peer 키는 같은 값이며,
두 파일은 0600이고 Git·이미지에서 제외된다. example과 Compose 파일 경로 참조를 제공한다.

- 전체 **200개 PASS**: 엔진153, gateway32, 배포 스크립트15.
- 실제 loopback HTTP 정상/중복/SQLite·수신기 재시작 후 중복은 **202/202/202**,
  동일 request ID, 수신1건·큐1건·intent0건이다.
- 서명 누락/오류, 31초 지난 시각, 본문 변조, 서명된 잘못된 JSON, 미허용 실제 소켓 IP,
  만료 peer profile은 모두 **403**이다. root 비소유 경로도 거부한다.
- 별도 gateway 테스트에서 비밀 파일 권한/형식, 리다이렉트 차단, 환경 proxy 무시,
  미설정 시 기존 전송 호환을 확인했다. Compose 구문 검사와 diff 공백 검사도 통과했다.
- gpt-5.6-luna/max 읽기 전용 독립 검토에서 현재 범위의 중요 결함을 발견하지 못했다.
  검토자는 gateway32개 테스트와 실제 loopback 검증을 별도로 재실행했다.

합성 키와 주입된 테스트 승인으로 검증했고 worker·외부 API·Telegram 송신·주문을 실행하지 않았다.
당시 root 소유 profile의 허용 검사, 새 gateway 이미지 빌드와 컨테이너 간 연결은 남아 있었다.
Docker Desktop 서비스가 inactive였고 소켓이 없어 다음 사용자 재실행까지 보류했다.
[검증 증적](gateway-signing-verification.json), [설정·연결 조건](runbook.md#telegram의-추가-운영-조건).

## 2026-09-15 Docker 재개 후 gateway 컨테이너 검증

- 새 이미지 `danta-telegram-gateway:verify-20260915` 빌드 완료.
  ID `sha256:60de34642b685b8c2b560078e5f378e08e1e0da766c8b3baf5848edcb9c5cb8e`.
  이미지 소스와 작업 파일 hash 일치, config 디렉터리는 비어 있다.
- 이미지의 기본 UID999, read-only, network none 조건에서 gateway **32개 PASS**.
  엔진 이미지는 현재 code ID와 일치하며 UID10001·네트워크 차단 오프라인 실행도 다시 통과했다.
- 별도 internal network의 두 이미지에 포함된 코드를 직접 사용했다. 정상/중복/수신기 재시작 후
  중복은 **202**, 서명 누락/변조/미허용 실제 컨테이너 IP는 **403**이다.
  재시작 전후 동일 request ID, 접수1건·큐1건·intent0건을 확인했다.
- root 소유 0444 profile과 모든 상위 경로를 실제 `PeerAuthenticator.from_file()`로 읽었다.
  receiver UID10001에서 profile 쓰기 불가, ext4 원장 디렉터리는 UID10001·0700이다.
  sender는 기존 gateway Compose와 같은 UID0이며 root 소유 합성 키 파일 0600을 읽었다.
- 호스트 포트 없음, 읽기 전용 root filesystem, cap drop ALL, no-new-privileges를 확인했다.
  합성 승인 fixture를 주입했으며 model/adapter/outbound socket/subprocess 호출과 worker는 모두 0이다.
  테스트 컨테이너·네트워크·볼륨을 제거했고 기존 서비스는 변경하지 않았다.

Docker Desktop의 `/tmp` bind mount가 불가능해 합성 검증 스크립트를 Git 제외 `var/` 경로로
옮겨 재실행했다. 제품 코드 수정은 없었다. 실제 Telegram 송수신, 운영 peer/approval 설정,
NAS 배포와 실거래는 이번 검사 범위에 포함되지 않는다. 기본 offline 설정은 유지한다.
상세 결과는 [gateway 검증 증적의 docker_validation](gateway-signing-verification.json)에 기록했다.

## 2026-09-15 다른 컴퓨터용 배포 설정 준비

사용자가 승인한 1~4번 범위(메뉴·Compose·운영 설정 세트·설치/업데이트 절차)를 구현했다.
기존 이미지 빌드/push 스크립트의 역할은 유지하며 원격 배포·서비스 시작은 하지 않았다.

- 실제 gateway v1 메뉴와 example을 수신기의 18개 명령에 맞췄다. 실제 파일의 URL/env 참조는
  보존했으며 `routes.yaml`은 비공개 백업 후 수정하고 Git에서 계속 제외한다.
- gateway는 peer secret 파일로 서명하며, 양쪽 runtime Compose는 같은 외부 네트워크 이름을
  사용한다. gateway 고정 IP를 peer example에 함께 기록한다. 기본 offline 네트워크는 유지한다.
- `scripts/prepare-codex-deployment.py`가 새 비공개 전달 폴더를 만든다. 전달본 Compose에는
  build 경로가 없으며 개발 PC 소스·로그인 상태를 요구하지 않는다. 비밀값은 명시 옵션일 때만
  0600으로 복사하고 Codex auth·원장·레거시는 제외한다. 기존 출력 폴더는 덮어쓰지 않는다.
- 운영 PC의 상대 경로, UID 권한, 동일 auth volume 로그인/업데이트, 네트워크 생성, 승인 준비,
  최초 실행과 SQLite backup 절차를 [배포 안내](../deployment/README.md)에 제공한다.
- 전체 **203개 PASS**(엔진153, gateway33, 도구17), 새 전달본 gateway/runtime/auth Compose 검사,
  소스 checkout 없는 전달본의 이미지 `doctor` 검사 PASS. 원문 README hash도 유지했다.
- gpt-5.6-luna/max 독립 검토 후 사용자 지정 대역의 전달본 README 반영과 루트 안내의
  출력 부모 폴더 생성 누락을 수정했다. 대역 불일치 회귀 검사 실패를 재현하고 수정 후 통과했다.
  계좌 별칭/환경은 미승인 shadow example의 예시이며 실제 계좌 귀속이라는 지적은 채택하지
  않았다. 실제 app의 null/offline, 귀속 미검증, 빈 권한을 확인하고 별칭의 의미를 안내에 명시했다.

전달본의 실제 `app.yaml`은 offline이며 `app.shadow.yaml.example`은 검증 전 준비본이다.
실제 수수료·달력·계좌 귀속·자금·발신자 ID·승인 유효기간은 확정하지 않았다. JSON example은
검증 false/권한 빈 목록/null을 유지하며 다음 실제 운영 연결 단계에서 근거와 함께 채워야 한다.
이 준비 완료는 실제 주문·Telegram 송신·운영 runtime의 시작 승인을 의미하지 않는다.

## 2026-09-15 HTML 보고서 Telegram 첨부

- `/report`는 요청 채팅에 일일 HTML을, 승인된 장 마감 작업은 기본 허용 채팅에 일일 HTML을
  첨부한다. 각 심사 실행의 summary HTML도 기존 실행 알림과 별도 outbox 항목으로 보관한다.
- 대기열에 파일 내용을 저장하므로 재시도·재시작·daily.html 갱신에도 최초 첨부를 유지한다.
  성공한 텍스트는 파일 실패 때문에 재발송하지 않으며 심사·주문을 재실행하지 않는다.
- 기존 승인·수신자 검사와 민감정보 형식 검사, 현재 설정의 알려진 비밀값 검사를 통과해야 송신한다.
- 전체 206개 PASS(엔진156, gateway33, 도구17). 새 이름의 이미지 빌드와 격리된 오프라인
  실행·중복 요청·HTML 생성 검증 PASS. 운영 Telegram 송수신이나 원격 배포는 수행하지 않았다.
- gpt-5.6-luna/max 독립 검토와 메인 재현에서 거부된 문서의 반복 시도가 후속 알림을 막는 문제를
  확인했다. 문서 검증 실패만 `BLOCKED`로 보관하고 통신 실패는 재시도하도록 수정했다.
  거부 문서 뒤 정상 알림 진행과 재시작 후 차단 유지 회귀 검사를 추가했다.

기본 offline·스케줄 비활성은 유지한다. 실제 운영 연결 및 승인 후 위 송신 경로가 동작한다.
Telegram 측 성공 뒤 응답 유실 시 파일 첨부가 중복될 수 있으므로 exactly-once 전달을 보장하지 않는다.
