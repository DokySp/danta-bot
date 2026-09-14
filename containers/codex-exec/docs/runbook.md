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
app이 변경 불가한 경로의 별도 JSON이다. 현재 gateway가 아래 선택적 인증 헤더를 만든다는
검증은 수행하지 않았다. 기존 gateway/proxy 변경·배포는 별도 승인 대상이다.

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
timestamp + `\nPOST\n/telegram\n` + 원본 요청 bytes이다. 비밀값은 최소 32자이며 image,
설정 snapshot, 로그, 응답에 쓰지 않는다. `X-Forwarded-For`나 본문의 peer는 신뢰하지 않는다.

일반 대화는 별도 세션 ID와 안내 응답만 만들고 모델·주문·설정 변경을 호출하지 않는다.
`/status`, `/report`, `/usage`, `/version`, `/session`, `/show_touch_point`는 기록을 읽는다.
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

`containers/codex-exec` 디렉터리에서 위 Python 의존성을 설치한 환경으로 실행한다.
Docker daemon은 로컬에서 실행 중이어야 한다.

```sh
docker build -t danta-codex-exec:local .
PYTHONPATH=src python scripts/verify_offline_image.py --image danta-codex-exec:local
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

image는 Python 3.12, 고정 Python 의존성, 새 application 코드·schema·migration·prompt와
명시된 합성 snapshot만 포함한다. 기본 명령은 `doctor`이며 자동 재시작하지 않는다.
실제 configuration/state/secret/approval을 image에 복사하지 않는다.
README와 렌더러도 포함한다. 읽기 전용 컨테이너에서는
`python scripts/render_report.py --output /app/var/design-report.html`처럼 결과를 상태 mount에
쓴다. `danta report --design`의 기본 /app/report.html 출력은 호스트 개발 환경용이다.

Codex 실행 파일·인증은 기본 image에 없다. 실제 모델 실행 시 운영자가 **컨테이너 안에서
실행됨을 검증한 Codex CLI 0.153.4 실행 파일과 필요한 런타임 디렉터리**를 별도 읽기 전용
mount로 제공하고, `model.executable`에 그 경로를 설정해야 한다. 인증 디렉터리와 격리
도구도 승인된 별도 mount/구성으로 검증한다. 임의 다른 CLI 버전을 자동 다운로드하거나
현재 image에 실제 모델 실행 환경이 완성됐다고 표시하지 않는다.

compose 사용 전에 다음 **외부 위치 참조**를 준비한다. 실제 경로 생성/권한 변경과 운영
서비스 시작은 운영자가 승인한 전환 절차에 포함되어야 한다.

| 환경 참조 | 조건 |
|---|---|
| DANTA_CONFIG_DIR | app.yaml/strategy.yaml/schedules.yaml이 있는 읽기 전용 디렉터리 |
| DANTA_SECRETS_FILE | 실제 연결에만 필요한 Git/image 밖 환경 파일. 기본 offline에는 생략 가능 |
| DANTA_STATE_DIR | 로컬 파일시스템, UID/GID 10001이 쓸 수 있는 상태 디렉터리 |
| DANTA_LOCK_DIR | 같은 계좌를 구동할 모든 컨테이너가 공유하는 UID 10001 소유 mode 0700 디렉터리 |

기본 state_dir 상대경로는 `/app/var` bind mount 아래에 보존된다. 컨테이너는 UID/GID 10001,
읽기 전용 root filesystem, 권한 제거, no-new-privileges로 실행한다. host port·host network·
Docker socket은 연결하지 않는다. 기본 internal network는 외부 API egress를 허용하지 않는다.
실제 계좌·모델·gateway 연결에 필요한 네트워크·인증 volume·trusted approval mount는
검증과 별도 운영 승인을 거쳐야 하며, 이 compose가 이미 live 운용을 제공한다고 해석하지 않는다.
