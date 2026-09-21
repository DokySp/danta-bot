# 다른 컴퓨터에서 실행하는 배포 절차

개발 PC에서 이미지를 올리고, 운영 서버에 설정을 복사한 뒤 Codex 로그인과 Compose 시작을 수행한다.
기본 설정은 사용자가 승인한 **전체 계좌 현금·보유 주식의 live 운용**이다. 서버가 계좌·주문·시장
자료와 실행 환경을 직접 확인해 준비한다. `runtime.json`, `runtime-manifest.json`, 해시 또는
`verified` 파일을 사람이 작성할 필요는 없다.

## 1. 개발 PC에서 이미지 준비

저장소 루트에서 실행한다. 두 이미지는 `latest`를 사용한다.

```sh
docker login -u dokysp
./scripts/deploy-trading-engine.sh dokysp
./scripts/deploy-telegram-gateway.sh dokysp
```

각 스크립트는 Docker 회귀 테스트·이미지 빌드·컨테이너 HTTP 검사를 통과한 뒤 해당 이미지를 push한다.
이는 실제 계좌·모델 연결·주문 체결이나 원격 서버 준비 완료를 증명하지 않는다. 스크립트는 운영 서버
파일을 복사하거나 서버 컨테이너를 재시작하지 않는다.

## 2. 운영 서버로 설정 복사

아래 파일을 복사한다. `*.example`은 처음 값을 작성할 때 참고하는 선택 파일이며 실제 값 위에 덮어쓰지 않는다.

| 저장소 파일 | 서버 파일 |
|---|---|
| containers/trading-engine/compose.yaml | /docker/trading-engine/compose.yaml |
| containers/trading-engine/config/app.yaml | /docker/trading-engine/config/app.yaml |
| containers/trading-engine/config/strategy.yaml | /docker/trading-engine/config/strategy.yaml |
| containers/trading-engine/config/schedules.yaml | /docker/trading-engine/config/schedules.yaml |
| containers/trading-engine/config/secrets.yaml | /docker/trading-engine/config/secrets.yaml |
| containers/telegram-gateway/compose.yaml | /docker/telegram-gateway/compose.yaml |
| containers/telegram-gateway/config/routes.yaml | /docker/telegram-gateway/config/routes.yaml |
| containers/telegram-gateway/config/telegram.env | /docker/telegram-gateway/config/telegram.env |

엔진 `secrets.yaml`에는 KIS·DART 인증 정보, `TELEGRAM_GATEWAY_URL`, `DANTA_CODEX_AUTH_HOME`,
`TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ALLOWED_SENDER_IDS`를 넣는다. chat은 gateway의 `telegram.env`와
맞춘다. 개인 채팅의 chat과 sender는 같은 사용자 ID다. 그룹의 음수 chat ID를 sender로 사용하지 않는다.
비밀 파일 권한은 `0600`으로 둔다.

서비스마다 Compose는 하나다. NAS 관리 화면을 쓰면 각 `compose.yaml` 전체를 넣고 작업 폴더를 해당 서비스
폴더로 지정한다. 별도 `.env`·override·init 서비스는 없다. `var/`, `locks/`, gateway의 `memory/`는 자동 생성된다.
엔진 내부에서 권한을 준비한 뒤 UID 10001로 실행하며 설정은 읽기 전용이다. `var`는 서버 로컬 디스크에 둔다.
NAS로 SMB 전송하는 것과 활성 DB를 SMB/NFS 마운트에서 실행하는 것은 다르다. 후자는 지원하지 않는다.

선택적으로 설정 묶음을 만들려면 개발 PC에서 아래 명령을 쓴다. 이미 만든 엔진 이미지의 Python을 사용한다.
기존 출력 폴더는 덮어쓰지 않는다.

```sh
mkdir -p deployment
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD":/work:ro -v "$PWD/deployment":/out:rw -w /work \
  --entrypoint python trading-engine:latest \
  scripts/prepare-trading-deployment.py --output /out/release --include-secrets
```

그룹 채팅이면 마지막 명령에 `--sender-id 허용사용자ID`를 추가한다. 여러 사용자는 옵션을 반복한다.
개인 chat ID만 양수로 지정돼 있으면 같은 sender ID를 추론한다. 실제 비밀값은 `--include-secrets`일 때만 복사한다.
결과 `PREPARED`는 `deployment/release`에 전송할 파일을 만들었다는 뜻이며 원격 검증·주문 성공 상태가 아니다.
기본 namespace/tag는 `dokysp/latest`다.

## 3. 운영 서버에서 로그인하고 시작

기존에 같은 계좌를 운용하는 엔진은 정상 종료한 뒤 새 엔진을 시작한다. `danta-bot-net`은 없으면 Compose가
생성한다. 엔진 포트를 외부에 공개하지 않고 신뢰하는 컨테이너만 이 네트워크에 연결한다.

```sh
cd /docker/trading-engine
docker compose pull
docker compose run --rm trading-engine codex -c 'cli_auth_credentials_store="file"' login --device-auth
```

표시되는 URL과 일회용 코드를 브라우저에서 완료한다. 로그인과 엔진은 `trading-engine-auth` Docker volume을
공유한다. 이미 이 서버에서 로그인했다면 로그인은 생략할 수 있다. `--entrypoint codex`는 사용하지 않는다.

NAS 관리 화면의 컨테이너 터미널은 root로 열릴 수 있다. 이때는 `codex`를 바로 실행하지 말고
`python -m danta.container_init codex -c 'cli_auth_credentials_store="file"' login --device-auth`를
사용한다. 시작 과정에서 기존 Codex DB·캐시·세션의 소유권을 UID 10001로 복구하며 내용과 파일 권한은
보존한다. root로 남은 파일은 로그인 확인이 성공해도 실제 AI 실행과 사용량 조회를 막을 수 있다.

```sh
docker compose run --rm trading-engine codex -c 'cli_auth_credentials_store="file"' login status
docker compose up -d --force-recreate --remove-orphans

cd /docker/telegram-gateway
docker compose pull
docker compose up -d --force-recreate --remove-orphans
```

관리 HTTP가 먼저 열리고 로그와 Telegram `/status`에 실제 준비 상태가 표시된다. 설정·로그인·외부 조회·기존
주문·데이터가 부족하면 이유를 표시한다. `/healthz`는 HTTP 생존 상태, `/readyz`의 HTTP 200과 `ready: true`는
거래 작업 접수 준비 상태다. `/version`은 실제 이미지 버전을 보여준다. `READY`도 체결 성공이나 수익성의 증거는 아니다.

최초 실행은 관측한 계좌 현금과 보유 수량을 따로 인수한다. 과거 매수가·매수 시점·체결 이력은 만들지 않는다.
인수 평가액을 기준으로 보호 규칙과 보유 기한을 적용하며, 재시작은 기존 장부를 보존하고 대사한다.
Codex 로그인과 로컬 격리 검사를 수행하고, 공시 원문 수집은 보유 보호가 시작된 뒤 review 작업에서 처리한다.
공시 자료가 수집되지 않은 종목은 신규 진입 조건을 통과하지 못한다. 기존 활성·예약
주문을 확인 없이 취소하거나 숨기지 않는다. 운용 권한은 승인 정책에서 읽고 관측 결과·해시·승인 참조는 `var`의 DB에
기록한다. 별도 `activate` 명령이나 수동 해시 작성은 필요 없다.

주문 전 계산에는 매수·매도 각각 **0.5% 수수료 추정치**, 매도 **0.2% 세금 추정치**와 방향별 10bp 슬리피지를 쓴다.
보수적인 수량 계산용이며 실제 계좌 약정 수수료가 아니다. 실제 현금·일별 수수료·세금은 KIS 계좌 조회로 따로 대사한다.
추정치를 실제 비용으로 기록하지 않는다. 원인을 확인하지 못한 현금 변동은 실제 잔액에 반영하되, 수수료나 입출금이라고 추정하지 않고 정확한 수익률 확정을 보류한다.

거래 창은 KRX **09:00–15:20**에 한정하고 실시간 메시지의 정규시장 구분도 확인한다. 휴장일과 과거 거래일은
조회하지만 특별 거래시간을 확인했다고 표시하지 않는다. 지연 개장일에는 정규시장 시세를 받기 전 주문하지 않으며
늦은 종료까지 거래 창을 늘리지 않는다. 당일 일봉은 다음 날부터 사용하고 일일 마감 보고는 다음 날 **00:10 한국시간**에
처리한다. NXT/SOR·시간외·애프터마켓 주문은 추가하지 않는다. 현재 수집한 수정주가를 과거 시점에 확보한 자료로
표시하거나 프로그램 준비 성공을 전략 수익성 검증으로 표시하지 않는다.

## 4. 이후 업데이트

개발 PC에서 1번 명령으로 새 `latest`를 올리고 변경한 설정만 서버에 복사한 뒤 실행한다. 기존 `var`, `locks`, 인증
volume은 보존한다. `docker compose down -v`는 사용하지 않는다.

```sh
cd /docker/trading-engine
docker compose pull
docker compose up -d --force-recreate --remove-orphans

cd /docker/telegram-gateway
docker compose pull
docker compose up -d --force-recreate --remove-orphans
```

원장이 있는 상태에서 계좌·정책 변경으로 대사가 필요하면 `/status`의 사유를 먼저 해결한다. 데이터 삭제로 검사를
통과시키지 않는다. 수동 manifest 방식은 별도 고급 운용 경로로 남아 있지만 기본 automatic 배포에는 필요 없다.
기존 인증 volume 이름이 다르면 엔진 Compose의 `volumes.codex-auth.name`을 기존 이름으로 맞춘다.
