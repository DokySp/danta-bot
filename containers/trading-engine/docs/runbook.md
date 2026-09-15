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

하나의 Application과 Store를 사용한다. HTTP는 인증·검증·영속 접수 후 202 응답을 보내고
모델 호출을 기다리지 않는다. 느린 심사, 빠른 제어, 알림 전송은 별도 worker이며, 보호·대사는
`Application.start_monitor`가 별도 실행한다. `/pause`, `/stop`, `/schedule_off`는 보호와
대사를 끄지 않는다. 단일 writer는 같은 실제 계좌·모드의 공유 파일 잠금으로 보장한다.

보호 감시는 마지막으로 검증한 종목·일봉·달력·공시를 재사용하며 보유/미체결 종목의 계좌와
호가만 갱신한다. 전체 공시 수집이 지연돼도 이 경로는 기다리지 않는다. 공시 조회 실패는
PARTIAL로 기록하고, 계좌·호가·거래 상태가 불명확하면 보호를 성공으로 표시하지 않는다.
확정 거절/취소된 보호 매도는 대사·현재 조건 검사 후 새 revision으로 재시도하지만 UNKNOWN이나
CANCEL_REQUESTED 주문은 재전송하지 않는다. 주문 요약은 원장의 실제 상태를 표시한다.
control/review worker 실패는 정리 후 종료 코드 1로 끝나며 runtime Compose의 `on-failure:3`
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
JSON의 sender/route/peer 선언만으로 인증하지 않는다. `trusted_peer_profile`은 root 소유,
app이 변경 불가한 경로의 별도 JSON이다. gateway 서명 구현, 로컬 HTTP와 별도 internal
Docker network의 이미지 간 수신·재시작 후 중복 방지를 검증했다. root 소유 profile의
비root 읽기도 확인했다. 실제 운영 Docker/NAS 연결·Telegram 송수신·배포는 아직 검증하지 않았다.

gateway의 `config/codex-peer.secret.example`을 `config/codex-peer.secret`으로 복사하고,
placeholder 대신 새 64자리 16진 문자열 한 줄을 넣는다. 예를 들어 로컬 Python의
`secrets.token_hex(32)`로 생성할 수 있다. 파일은 0400/0600인 일반 파일이어야 하며,
symlink·잘못된 형식·128 bytes 초과 파일은 거부한다. 실제 키는 Git·이미지에 넣지 않는다.
같은 문자열을 엔진 private `config/secrets.yaml`의 `DANTA_TELEGRAM_PEER_SECRET`에 넣는다.
16진 문자열을 binary로 변환하지 않고 ASCII bytes 그대로 HMAC 키로 사용한다.

gateway Compose는 `DANTA_TELEGRAM_PEER_SECRET_FILE=/app/config/codex-peer.secret`을 항상 설정하고
읽기 전용 config mount에서 파일을 읽는다. 파일을 읽을 수 없으면 시작이 실패한다.
서명 경로를 생략하는 별도 client 사용은 기존 코드 호환용이며 이 배포 Compose는 사용하지 않는다.
키는 프로세스 시작 때만 읽으므로 교체할 때 양쪽 서비스에 같은 키를 반영하고 재생성한다.
signed route URL은 credentials/query/fragment 없는 HTTP(S)의 정확한 `/telegram` 경로여야 한다.
서명된 요청은 리다이렉트와 환경 proxy를 사용하지 않는다.

기본 Compose의 엔진은 `isolated` 네트워크와 HTTP listen `127.0.0.1`을 유지한다.
runtime override는 gateway와 같은 외부 네트워크 `DANTA_GATEWAY_NETWORK`에 추가로 연결한다.
배포 생성기의 shadow example은 listen `0.0.0.0`, 고정 gateway IP 및 profile 경로를 준비한다.
운영 전환 시 공통 네트워크와 컨테이너 수신 주소, 수신기가 보는 실제 gateway IP를 대조해
아래 profile 및 sender/chat allowlist에 반영해야 한다. root 소유 profile·승인 경로와
설정 hash/유효기간 검증도 충족해야 한다. 로컬 합성 검증 결과를 운영 승인 파일로 사용하지 않는다.

프로파일 계약:

```json
{
  "schema_version": 1,
  "identity": "approved-gateway-identity",
  "allowed_source_ips": ["127.0.0.1"],
  "secret_env": "DANTA_TELEGRAM_PEER_SECRET",
  "verified": true,
  "evidence_id": "operator-provided-transport-verification",
  "expires_at": "2030-01-01T00:00:00+00:00",
  "config_hash": "exact-approved-configuration-hash"
}
```

이 예시는 유효한 승인 파일이 아니다. 운영자가 실제 증거·유효기간·hash를 넣어야 한다.
요청의 실제 TCP peer IP와 profile을 검사하고, `X-Danta-Timestamp`의 ±30초 시각과
`X-Danta-Signature`의 HMAC-SHA256 16진 문자열을 확인한다. 서명 입력은 정확한 UTF-8
timestamp + `\nPOST\n/telegram\n` + 원본 요청 bytes이다. 수신기는 최소 32자를 요구하며
현재 gateway 파일 형식은 64자리 16진 문자열이다. 비밀값은 image,
설정 snapshot, 로그, 응답에 쓰지 않는다. `X-Forwarded-For`나 본문의 peer는 신뢰하지 않는다.

일반 대화는 별도 세션 ID와 안내 응답만 만들고 모델·주문·설정 변경을 호출하지 않는다.
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
명시된 합성 snapshot만 포함한다. 기본 명령은 `doctor`이며 자동 재시작하지 않는다.
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

엔진 이미지를 준비한 뒤 아래 명령을 실행한다. 로그인 전용 Compose는 거래 설정·DB·
승인 파일 없이 실행되며, 표시된 URL과 일회용 코드를 사용자가 브라우저에서 완료한다.
Docker/NAS에서 localhost callback을 연결할 필요가 없는 기기 코드 로그인이다.

```sh
cd containers/trading-engine  # 저장소 루트에서 실행할 때
export DANTA_IMAGE=danta-trading-engine:local  # 배포한 이미지 이름으로 맞춘다
docker compose -f compose.auth.yaml run --rm codex-login

# 저장된 로그인 상태만 확인
docker compose -f compose.auth.yaml run --rm codex-login login status
```

기본 model/effort는 기존 env의 `gpt-5.6-sol`/`xhigh`, 인증 방식은 `chatgpt`이다.
로그인과 판단 subprocess는 모두 file 저장 방식을 사용한다. 실제 모델 호출은 운영
설정·격리 증거·승인 검증을 계속 거친다. 로그인 성공이 거래 권한을 부여하지 않는다.

2026-09-14에는 `danta-codex-exec:login-20260914-verified`에서 실제 ChatGPT 로그인,
읽기 도구 호출과 JSON 결과 검증을 완료했다. 이 이미지에는 실제 모델이 요구하는
Code Mode host와 market 도구 제한, auth 읽기를 차단하는 권한 profile이 반영되어 있다.
[단독 연결 검증 결과](codex-login-verification.json)를 운영 승인으로 대신 사용하지 않는다.

여러 운영 프로필은 `DANTA_AUTH_VOLUME`을 다르게 지정한다. 로그인과 runtime 명령에
같은 값을 사용해야 한다. 기존 호스트 인증 디렉터리를 쓰려면 양쪽에 같은 `DANTA_AUTH_DIR`
절대 경로를 지정할 수 있다. 이 경우 UID 10001의 읽기/쓰기 권한을 운영자가 준비한다.
기존 volume/디렉터리의 권한과 내용은 자동으로 변경하지 않는다. 인증 보존을 위해
`docker compose down -v`로 이 volume을 삭제하지 않는다.

컨테이너 안에서 사용하는 실행 파일의 SHA-256과 제한 도구 probe 결과를 manifest에
연결한다. 호스트의 npm launcher hash를 컨테이너 native binary 증거로 사용하지 않는다.

compose 사용 전에 다음 **외부 위치 참조**를 준비한다. 실제 경로 생성/권한 변경과 운영
서비스 시작은 운영자가 승인한 전환 절차에 포함되어야 한다.

| 환경 참조 | 조건 |
|---|---|
| DANTA_CONFIG_DIR | app.yaml/strategy.yaml/schedules.yaml이 있는 읽기 전용 디렉터리 |
| config/secrets.yaml | config 디렉터리 안의 실제 비밀 파일. UID 10001이 읽을 수 있는 0400/0600; Git/image 제외 |
| DANTA_IMAGE | 빌드/푸시한 정확한 이미지 이름·태그 |
| DANTA_AUTH_VOLUME | 선택 사항. 기본 `trading-engine-auth`; 로그인/runtime이 공유하며 프로필별로 구분 |
| DANTA_AUTH_DIR | 선택 사항. named volume 대신 기존 전용 디렉터리를 쓸 때 지정, UID 10001 소유 0700 |
| DANTA_APPROVAL_DIR | root 소유 읽기 전용 승인 디렉터리, runtime.json 포함 |
| DANTA_STATE_DIR | 로컬 파일시스템, UID/GID 10001이 쓸 수 있는 상태 디렉터리 |
| DANTA_LOCK_DIR | 같은 계좌를 구동할 모든 컨테이너가 공유하는 UID 10001 소유 mode 0700 디렉터리 |

기본 state_dir 상대경로는 `/app/var` bind mount 아래에 보존된다. 컨테이너는 UID/GID 10001,
읽기 전용 root filesystem, 권한 제거, no-new-privileges로 실행한다. host port·host network·
Docker socket은 연결하지 않는다. 기본 internal network는 외부 API egress를 허용하지 않는다.
실제 계좌·모델·gateway 연결에 필요한 네트워크·인증 volume·trusted approval mount는
검증과 별도 운영 승인을 거쳐야 하며, 이 compose가 이미 live 운용을 제공한다고 해석하지 않는다.

## 비밀값 이전과 runtime Compose

`config/secrets.yaml.example`을 복사하여 실제 비밀값을 채운다. 기존 env는 런타임이
자동 로딩하지 않는다. 레거시의 계좌번호가 8자리이면 기존 코드의 상품코드 규칙
`KIS_PROD_TYPE`, 미설정 시 `01`을 적용하여 `KIS_ACCOUNT_REF`로 이전한다. 이전 결과를
출력하거나 Git에 올리지 않는다. 예전 cache는 복사하지 않으며 새 cache가 만료시각을 관리한다.
비밀값 변경은 다음 프로세스 시작에 적용된다.

아래 명령은 설정·인증·운영 증거·신뢰 승인을 준비한 운영 호스트에서만 실행한다.
설정의 `state_dir`는 `/app/var/<mode>/<account-alias>`처럼 모드와 계좌별로 분리한다.

```sh
# 1회 검증: 외부 egress 차단, doctor
docker compose -f compose.yaml run --rm trading-engine
# 지속 실행: 명시적으로 egress를 허용하고 승인 파일을 읽어 serve 시작
docker compose -f compose.yaml -f compose.runtime.yaml up -d
```

운영 override는 `on-failure:3`, 전용 auth mount, 읽기 전용 approval mount를 추가한다.
기본 ingress는 꺼져 있으며 host port는 열지 않는다. 기존 gateway에서 접근하려면
검증한 공용 Docker network와 listen_host/route/peer 서명을 준비해야 한다. 운영 증거
없이 모드를 live로 바꾸거나 빈 승인을 생성하지 않는다. 이미지 빌드·푸시 스크립트는
NAS 파일 복사, 로그인, 기존 계좌 인수, 운영 컨테이너 재시작을 실행하지 않는다.
