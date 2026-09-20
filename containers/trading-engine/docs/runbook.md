# 실행·복구 안내

현재 상태는 `STRATEGY_UNPROVEN`, `EXTERNAL_INTEGRATION_UNVERIFIED`,
`LIVE_NOT_AUTHORIZED`다. 코드·합성 검증 완료와 실제 연결·거래·배포 승인은 다르다.
기본 설정은 offline, 실행/스케줄/보호 감시/Telegram ingress 비활성이다.

## 로컬 검증

Python 3.12 환경에서 `requirements.lock`을 설치하고 `pip install --no-deps
--no-build-isolation -e .`로 CLI를 연결한다.

```sh
danta doctor
danta config validate
danta run --kind full_review
danta status
danta replay --manifest tests/fixtures/evaluation-synthetic.json
danta evaluate --manifest tests/fixtures/evaluation-synthetic.json
python -m unittest discover -s tests/unit
python -m unittest discover -s tests/integration
```

기본 `run`은 제공 합성 snapshot 한 번을 처리한다. 실제 시장·계좌·모델 결과가 아니다.
`serve`는 `cli.make_application`으로 동일 application을 구성한다. offline 합성 snapshot을
반복 스케줄하여 현재 시장 관측처럼 쓰는 설정은 차단한다. 기본 ingress가 false이면 소켓을
열지 않는다. 승인된 실제/향후 데이터 어댑터와 설정을 갖춘 경우에만 `danta serve`를 명시해
서비스를 시작한다. 서비스 실행 자체는 외부 권한이나 live 활성화를 부여하지 않는다.

## 프로세스와 저장소

하나의 Application과 Store를 사용한다. HTTP는 허용 사용자·권한 검증·영속 접수 후 202 응답을 보내고
모델 호출을 기다리지 않는다. 느린 심사, 빠른 제어, 알림 전송은 별도 worker이며, 보호·대사는
`Application.start_monitor`가 별도 실행한다. `/pause`, `/stop`, `/schedule_off`는 보호와
대사를 끄지 않는다. 단일 writer는 같은 실제 계좌·모드의 공유 파일 잠금으로 보장한다.

보호 감시는 마지막으로 검증한 종목·일봉·달력·공시를 재사용하며 보유/미체결 종목의 계좌와
호가만 갱신한다. 전체 공시 수집이 지연돼도 이 경로는 기다리지 않는다. 공시 조회 실패는
PARTIAL로 기록하고, 계좌·호가·거래 상태가 불명확하면 보호를 성공으로 표시하지 않는다.
확정 거절/취소된 보호 매도는 대사·현재 조건 검사 후 새 revision으로 재시도하지만 UNKNOWN이나
CANCEL_REQUESTED 주문은 재전송하지 않는다. 주문 요약은 원장의 실제 상태를 표시한다.
control/review worker 실패는 정리 후 종료 코드 1로 끝나며 Compose의 `on-failure:3`
재시작 대상이 된다. 정상 stop은 성공 종료다.

SQLite 파일은 검증된 로컬 파일시스템에 둔다. CIFS/NFS에 활성 DB를 두지 않는다. 백업은
SQLite backup API로 생성·integrity_check하고, 복원은 존재하지 않는 별도 경로에만 한다.
WAL 파일만 복사하거나 활성 DB 파일을 덮어쓰지 않는다. 재시작 시 불명 주문은 대사하고,
서비스에서 실행 중이던 거래 요청을 새 주문으로 재실행하지 않는다. 기존 workflow 결과가
있으면 복구해 표시하고 없으면 `INTERRUPTED_RECONCILE_REQUIRED`로 남긴다.

접수 중 프로세스가 죽어 durable queue 연결이 불명한 Telegram 요청은 새 update로 다시
제출해야 한다. 이미 확정된 동일 update/본문은 중복 실행하지 않고 동일 request ID를 반환한다.
같은 ID의 다른 본문은 거절한다. 재시작 시 기한 지난 재량 요청은 실행하지 않는다.

## Telegram의 추가 운영 조건

설정의 `enabled`, `ingress_enabled`, allowed sender/chat, route 외에도 trusted approval의
`telegram_ingress`, `telegram_send`, 제어에는 `telegram_control` capability가 필요하다.
Telegram은 `trading-engine` route 하나와 gateway의 `config/telegram.env` 하나를 사용한다.
엔진의 `telegram.route`와 gateway route 이름은 `trading-engine`으로 맞춘다. 별도 peer 키,
HMAC 서명, peer profile, 고정 gateway IP 설정은 사용하지 않는다. gateway가 Telegram에서
받은 발신자·채팅 정보를 전달하고 엔진은 허용 sender/chat과 runtime 권한을 검사한다.

엔진과 gateway는 각자의 `compose.yaml`에서 공통 `danta-catalyst-net` 네트워크에 연결한다.
기본 app.yaml의 HTTP listen은 `127.0.0.1`이며 ingress가 비활성이다.
배포 생성기의 shadow example은 listen `0.0.0.0`을 준비한다. 엔진 HTTP 포트를 호스트에
공개하지 않으며 공통 네트워크에는 신뢰하는 컨테이너만 연결한다. 이 네트워크의 다른
클라이언트 요청을 별도 서명으로 구별하지 않으므로 sender/chat 목록만으로 전송 출처가
인증되는 것은 아니다. 실제 운영 Docker/NAS 연결·Telegram 송수신·배포는 별도로 검증한다.

기존 설정을 이전할 때는 사용할 봇의 token/chat 값을 `config/telegram.env`로 옮기고
routes를 단일 `trading-engine`으로 바꾼다. 엔진 `trusted_peer_profile` 설정과 Compose의
peer 키 환경변수·고정 IP 설정은 제거한다. config hash가 바뀌므로 runtime 승인 기록은
변경된 설정에 맞춰 갱신한다. 제어·거래 승인과 허용 sender/chat 목록은 계속 필요하다.

일반 대화는 route·chat·user별 별도 세션에서 승인된 모델을 호출한다. 최근 10회 대화를
문맥으로 사용하고 `/new`로 초기화한다. 일반 대화에는 매매 도구나 계좌 자료를 제공하지
않으며 주문·설정 변경을 실행하지 않는다. 모델 호출량·오류도 기존 사용 기록에 남긴다.
`/status`, `/report`, `/usage`, `/version`, `/session`, `/show_touch_point`는 기록을 읽는다.
`/report`는 요청한 채팅에 일일 `daily-<날짜>.html` 파일을 첨부한다. 승인된 스케줄의
`finalize_and_report`도 일일 HTML을 보내며, 각 심사 실행은 `summary-<날짜>-<실행 ID>.html`을 보낸다.
예약·실행 알림은 기존 규칙대로 설정된 route와 단 하나의 허용 chat을 사용한다. 허용 chat이
여러 개라 목적지가 불명확하면 자동으로 대상을 추정하지 않는다. 로컬 CLI `report`는 파일만 생성한다.

첨부 내용은 생성 시 SQLite outbox에 보관하므로 원본 daily.html이 갱신되거나 서비스가 재시작돼도
같은 내용을 재시도한다. 텍스트와 파일은 별도 작업이며 실패한 파일 때문에 심사·주문을 다시 실행하지 않는다.
송신 때마다 `telegram_send` 승인, 허용 chat, 민감정보 형식과 현재 secrets.yaml의 알려진 비밀값을 검사한다.
문서 형식·민감정보·수신 대상 검증에 실패한 첨부는 `BLOCKED`로 보관하고 후속 알림을 처리한다.
해당 outbox 행의 `outbox_last_outcome:<id>`에 값 노출 없는 거부 사유를 기록한다. 원인을 수정한 뒤
새 `/report`로 다시 요청한다. 일시적인 통신 실패는 `PENDING`으로 남겨 같은 첨부를 재시도한다.
Telegram 송신 성공 후 응답이 유실되면 재시도 첨부가 중복될 수 있다. 기본 offline·스케줄 비활성 설정은 유지한다.

`/new`는 채팅 세션만 바꾼다. `/review`는 기존 권한 범위의 동일 심사 workflow만 요청한다.
네 종목 목록 명령은 ticker 인자 한 개와 `telegram_control` 승인이 필요하다. 현재 universe에
있는 종목의 후보 포함·제외만 변경하며, 보유 수량·전략 진입 기준·보호 청산을 우회하지 않는다.
offline 외 모드에서는 공통 Application이 후보 범위 변경용 `candidate_control` 권한도 검사한다.
`/resume`는 인자를 받지 않고 공통 Application의 현재 계좌·중단 사유·trusted approval
검사를 실행하며, `resume` 권한이 필요하다. 낙폭 중단에는 별도 `resume_drawdown` 권한도 필요하다.

`/reasoning_effort`는 현재 값을 조회한다. 값 한 개를 붙인 요청은 `telegram_control` 승인 후
SQLite와 journal에 `APPROVAL_REQUIRED`로 보존한다. 실제 모델의 지원 여부와 새로운 trusted
설정·승인은 운영 workflow에서 확인해야 하며, 이 요청은 즉시 정책을 변경하거나 모델을 호출하지 않는다.

당일 공시의 `EXACT` 또는 `FIRST_COLLECTED` available_at은 공식 원문 수집이 완전하고
현재 세션에서 이미 관측된 경우에만 event review를 예약한다. `UNCERTAIN`이나 미래 수집 시각은
트리거로 쓰지 않는다.

송신은 저장된 outbox를 gateway `/sendMessage`로 보내 `ok:true`를 확인한다. 실패/timeout은
동일 outbox만 다시 보내며 거래를 다시 실행하지 않는다. timeout 뒤 실제 전달 여부는 불명할
수 있어 알림 중복 가능성을 구분한다. 외부 gateway 전달 성공은 별도 실제 증적이 필요하다.

## Docker 경계

`containers/trading-engine` 디렉터리에서 위 Python 의존성을 설치한 환경으로 실행한다.
Docker daemon은 로컬에서 실행 중이어야 한다.

```sh
docker build -t danta-trading-engine:local .
PYTHONPATH=src python scripts/verify_offline_image.py --image danta-trading-engine:local
```

검증 스크립트는 자신의 위치에서 저장소 루트를 찾는다. 기본 offline 설정 3개를 민감정보
검사 후 stdin으로 전달하며, host 디렉터리 공유나 실제 credential은 필요 없다. 새 임시
컨테이너를 UID 10001, 읽기 전용 root filesystem, `--network none`, 권한 제거,
`no-new-privileges`, 임시 상태·잠금 디렉터리로 실행하고 종료 후 제거한다.
doctor, 합성 심사·체결, 요청 중복 방지, 기본 service의 소켓 미개방, 일일 보고서와
README 렌더링을 확인한다. 실제 API·모델·Telegram 송신은 실행하지 않는다.

출력 JSON의 `evidence` 디렉터리에 `validation.json`, `report.html`, `daily.html`,
`daily.json`을 보존한다. `validation.json`의 검증 시각, image ID, image 안의 code ID가
검증 대상을 식별한다. 이후 소스가 변경되면 새로 빌드한 이미지에 같은 명령을 실행한다.
이 스크립트는 호스트 검증용이며 image에는 포함하지 않는다.

image는 Python 3.12, 고정 Python 의존성, Codex CLI 0.153.4와 새 application 코드·schema·migration·prompt와
명시된 합성 snapshot만 포함한다. 이미지 단독 기본 명령은 `doctor`다.
배포 Compose는 `serve`로 실행하며 실패 시 최대 3회 재시작한다.
실제 configuration/state/secret/approval을 image에 복사하지 않는다.
README와 렌더러도 포함한다. 읽기 전용 컨테이너에서는
`python scripts/render_report.py --output /app/var/design-report.html`처럼 결과를 상태 mount에
쓴다. `danta report --design`의 기본 /app/report.html 출력은 호스트 개발 환경용이다.

Codex CLI는 image에 포함하며 기본 `model.executable: codex`를 사용한다. 기존처럼
`codex login`으로 인증을 생성한다. `auth.json`을 직접 작성하거나 미리 가져올 필요는 없다.
로그인과 runtime은 같은 Docker volume `trading-engine-auth`를 `/app/auth`에 mount한다.
새 volume의 디렉터리는 image에서 UID 10001·0700으로 초기화하며, Codex가 인증을
저장·갱신한다. `secrets.yaml`의 `DANTA_CODEX_AUTH_HOME`도 `/app/auth`로 둔다.
인증은 image 및 브로커 비밀값·정책·승인 디렉터리와 분리한다.
[공식 인증 저장·갱신 설명](https://learn.chatgpt.com/docs/auth).

설치·로그인·업데이트 명령은 [배포 절차](../deployment/README.md)에 모았다.
서비스별 `compose.yaml` 하나를 사용하며 별도 `.env`나 Compose override는 없다.
엔진의 `init` 서비스는 network 없이 실행되어 config·var·locks·auth의 소유권과 권한만 준비한
뒤 종료한다. 설정 내용과 기존 DB·인증 파일 내용은 변경하지 않는다. 실제 엔진은 UID 10001,
읽기 전용 root/config, cap_drop ALL, no-new-privileges로 실행하며 Docker socket은 연결하지 않는다.
로그인도 이 Compose와 같은 인증 volume을 사용한다. `docker compose down -v`는 인증을 지우므로
사용하지 않는다. 기본 model/effort는 `gpt-5.6-sol`/`xhigh`, 인증 방식은 `chatgpt`다.

비밀값은 `config/secrets.yaml`, 승인과 운영 정보는 `config/runtime.json`과
`config/runtime-manifest.json`에 둔다. `danta`는 외부 모드에서 config의 runtime.json을 자동으로
찾아 검증한다. `--approval-file`을 명시하면 해당 파일을 우선 사용한다. offline 실행은 기본
승인을 읽지 않으며, `approvals show`는 모드와 관계없이 저장된 승인을 검증해 보여준다.
승인 파일이 없거나 만료·설정 hash 불일치이면 외부 실행을 허용하지 않는다.

비밀값 작성 형식은 [secrets.yaml.example](../config/secrets.yaml.example)을 참고한다.
기존 env는 런타임이 자동 로딩하지 않는다. 레거시의 계좌번호가 8자리이면 기존 상품코드
`KIS_PROD_TYPE`, 미설정 시 `01`을 적용하여 `KIS_ACCOUNT_REF`로 이전한다. 발급 토큰 캐시는
옮기지 않으며 새 cache가 만료시각을 관리한다. 실제 값을 출력하거나 Git에 올리지 않는다.
비밀값·정책 변경은 다음 프로세스 시작에 적용되며 config hash가 바뀌면 승인도 갱신해야 한다.

컨테이너 안 실행 파일의 SHA-256과 제한 도구 probe 결과를 runtime-manifest.json에 연결한다.
호스트의 npm launcher hash를 컨테이너 native binary 증거로 사용하지 않는다. 2026-09-14의
[단독 연결 검증](codex-login-verification.json)은 현재 운영 승인이나 배포 완료의 대체 증거가 아니다.
이미지 빌드·푸시는 서버 파일 복사, 로그인, 기존 계좌 인수, 운영 컨테이너 재시작을 실행하지 않는다.
