# 다른 컴퓨터에서 실행하는 배포 절차

개발 PC에서는 이미지를 Docker Hub에 올리고, 운영 서버에서는 그 이미지를 받아 실행한다.
서버에 직접 옮기는 것은 각 서비스의 `config/`다. **Compose는 서비스 폴더마다 하나**이며
NAS 관리 화면에는 해당 `compose.yaml` 내용 전체를 넣으면 된다. 별도 `.env`는 사용하지 않는다.

## 1. 개발 PC에서 이미지 올리기

저장소 루트에서 실행한다. 테스트·빌드·push가 모두 성공한 뒤 다음 단계로 간다.
두 이미지 모두 `latest`를 사용하며 테스트 의존성은 Docker에서 준비한다.

```sh
docker login -u dokysp
./scripts/deploy-trading-engine.sh dokysp
./scripts/deploy-telegram-gateway.sh dokysp
```

## 2. 서버로 옮길 파일

| 개발 PC의 파일 | 서버에서 둘 위치 | 용도 |
|---|---|---|
| containers/trading-engine/config/app.yaml | trading-engine/config/app.yaml | 엔진 실행 설정 |
| containers/trading-engine/config/strategy.yaml | trading-engine/config/strategy.yaml | 매매 정책 |
| containers/trading-engine/config/schedules.yaml | trading-engine/config/schedules.yaml | 실행 일정 |
| containers/trading-engine/config/secrets.yaml | trading-engine/config/secrets.yaml | KIS·DART 인증 정보, 연결 주소 |
| 확정된 runtime.json | trading-engine/config/runtime.json | 외부 연결·운용 승인 |
| 확정된 runtime-manifest.json | trading-engine/config/runtime-manifest.json | 검증된 운영 정보 |
| containers/telegram-gateway/config/routes.yaml | telegram-gateway/config/routes.yaml | 엔진 연결과 메뉴 |
| containers/telegram-gateway/config/telegram.env | telegram-gateway/config/telegram.env | 봇 토큰·허용 채팅 |

`*.example`은 작성 형식 참고용이다. 실제 값이 있는 파일을 예제로 덮어쓰지 않는다.
`runtime.json.example`과 `runtime-manifest.json.example`도 엔진 `config/`에 있다.
이 둘은 미승인 예시이므로 이름만 바꾼다고 외부 연결·거래가 가능해지지 않는다.
기본 `app.yaml`은 offline이다. 설정 정리나 Compose 실행이 운영 승인을 대신하지 않는다.

서버 구조는 아래와 같다. Compose를 파일로 관리한다면 각 서비스의 `compose.yaml`을 하나씩
복사한다. NAS 관리 화면으로 관리한다면 파일 복사 대신 같은 내용을 화면에 붙여 넣는다.
그 프로젝트의 작업 폴더를 해당 서비스 폴더로 지정한다.

```text
서버의 Docker 폴더/
├── trading-engine/
│   ├── compose.yaml
│   └── config/              ← 위의 엔진 설정·비밀값·승인 파일
└── telegram-gateway/
    ├── compose.yaml
    └── config/              ← routes.yaml, telegram.env
```

`var/`, `locks/`, `memory/`는 옮기거나 미리 만들 필요가 없다. Docker가 생성하고
`trading-engine` 컨테이너 내부에서 권한을 준비한 뒤 일반 사용자로 엔진을 실행한다.
별도 init 컨테이너는 없으며, 실행 중인 엔진은 config를 읽기 전용으로 사용한다.
기존 설치를 갱신할 때는 기존 데이터 폴더를 그대로 사용한다. `var`는 서버의 로컬 디스크에
두며 SMB/NFS 마운트 위에서 활성 DB를 실행하지 않는다. 다른 PC에서 SMB로 NAS에 파일을
전달하는 것은 괜찮지만, Docker에서 사용하는 저장소 자체는 NAS의 로컬 디스크여야 한다.

설정 묶음을 만드는 `prepare-trading-deployment.py`는 선택 도구이며 필수 배포 단계가 아니다.
기존 설정을 직접 복사하면 된다. 도구를 사용할 경우 `--namespace`와 `--version`의 기본값은
`dokysp`, `latest`이며 `--include-secrets`를 지정해야 실제 비밀값을 복사한다.

## 3. 운영 서버에서 최초 실행

아래는 서버 폴더가 `/docker/trading-engine`, `/docker/telegram-gateway`인 경우다.
공유 네트워크는 기존 `danta-bot-net`을 사용한다. `external` 설정은 없으며 네트워크가
없으면 Compose가 자동 생성한다. 네트워크에는 신뢰하는 컨테이너만 연결한다.

```sh
cd /docker/trading-engine
docker compose pull
docker compose run --rm trading-engine codex -c 'cli_auth_credentials_store="file"' login --device-auth
```

로그인 명령에 표시되는 URL과 일회용 코드를 브라우저에서 완료한다. 이미 해당 서버에서
로그인했다면 다시 할 필요 없다. 로그인 상태는 다음 명령으로 확인한다.

```sh
docker compose run --rm trading-engine codex -c 'cli_auth_credentials_store="file"' login status
```

로그인은 서버의 `trading-engine-auth` Docker volume에 저장된다. 개발 PC의 인증 파일을
복사하거나 직접 작성하지 않는다. 로그인과 서비스는 같은 Compose와 volume을 사용한다.
로그인도 시작 절차를 거쳐야 하므로
이전의 `--entrypoint codex` 옵션은 사용하지 않는다.

```sh
cd /docker/trading-engine
docker compose run --rm trading-engine doctor
docker compose up -d --force-recreate --remove-orphans

cd /docker/telegram-gateway
docker compose pull
docker compose up -d --force-recreate --remove-orphans
```

기본 offline 설정에서는 엔진이 기동해도 계좌 조회·매매·Telegram 접수를 하지 않는다.
외부 운용에는 기존 검증 절차에 따라 확정된 설정과 `config/runtime.json`,
`config/runtime-manifest.json`이 필요하다. 승인 내용·유효기간·설정 hash 검사는 유지된다.
승인 파일 경로는 자동으로 읽으므로 별도 실행 옵션은 필요 없다. gateway에서 엔진으로
접속할 운영 설정은 listen_host `0.0.0.0`, route `trading-engine`, 허용 sender/chat을 맞춘다.
`doctor`의 설정 검사 성공은 외부 연결이나 실제 주문 성공의 증거가 아니다.

## 4. 이후 업데이트

개발 PC에서 1번 명령으로 새 `latest`를 올린 뒤 서버에서 실행한다.
설정이 변경되지 않았다면 config를 다시 복사할 필요 없다. 설정·코드가 바뀐 경우 승인에
기록된 hash를 기존 검증 절차에 따라 갱신한다. `--force-recreate`는 이미지가 같아도 변경된
config를 다시 읽도록 컨테이너를 재생성한다. 기존 비밀값·DB·인증 volume은 보존한다.
`--remove-orphans`는 이전 구성의 별도 init
컨테이너를 정리한다. 새 엔진 컨테이너 이름은 `trading-engine`으로 고정된다.

```sh
cd /docker/trading-engine
docker compose pull
docker compose up -d --force-recreate --remove-orphans

cd /docker/telegram-gateway
docker compose pull
docker compose up -d --force-recreate --remove-orphans
```

기존 배포에서 옮길 때는 `approvals/runtime.json`과 `approvals/runtime-manifest.json`을
엔진의 `config/`로 옮기고 app.yaml의 해당 경로를 `/app/config/runtime-manifest.json`으로
맞춘다. `.env`, `compose.auth.yaml`, `compose.runtime.yaml`은 새 Compose에서는 사용하지 않는다.
기존 인증 volume 이름이 다르면 엔진 Compose의 `volumes.codex-auth.name`만 기존 이름으로 맞춘다.
기존 codex-exec 컨테이너는 새 엔진을 시작하기 전에 정상 종료하고, 같은 계좌를 다른 폴더에서
동시에 실행하지 않는다. 인증 보존을 위해 `docker compose down -v`는 사용하지 않는다.
