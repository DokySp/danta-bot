# 다른 컴퓨터에서 실행하는 배포 절차

개발 PC는 코드·테스트·이미지 빌드/push를 담당한다. 운영 컴퓨터는 전달받은 파일과
이미지만 사용한다. 개발 PC의 소스 경로, Python 환경, Docker 인증 volume을 공유하지 않는다.
이 문서는 준비된 배포 폴더의 `README.md`로도 복사된다. 명령의 `<배포폴더>`는 운영 컴퓨터의
실제 절대 경로로 바꾼다. 기존 서비스가 있는 폴더에 최초 설치 파일을 덮어쓰지 않는다.

## 1. 개발 PC: 이미지 태그와 전달 파일 준비

저장소 루트에서 기존 lockfile을 설치한 Python을 사용한다. 예시의 `RELEASE`는 두 이미지와
설정 폴더에 동일하게 적용한다. 이미지 빌드/push는 별도 배포 단계이며 준비 스크립트는 실행하지 않는다.

```sh
RELEASE=$(git rev-parse --short HEAD)
mkdir -p containers/codex-exec/var
.venv/bin/python scripts/prepare-codex-deployment.py \
  --namespace dokysp --version "$RELEASE" \
  --output "containers/codex-exec/var/deployment-$RELEASE" --include-secrets

# 이미지 배포 단계에서 실행한다.
PYTHON_BIN="$PWD/.venv/bin/python" ./scripts/deploy-codex-exec.sh dokysp "$RELEASE"
PATH="$PWD/.venv/bin:$PATH" ./scripts/deploy-telegram-gateway.sh dokysp "$RELEASE"
```

`--include-secrets`는 현재 엔진의 private secrets.yaml, gateway의 v1 env와 일치하는 peer 키를
새 폴더에 0600으로 복사한다. 토큰 캐시·Codex auth·원장·레거시는 복사하지 않는다.
출력 폴더가 존재하면 중단하며 기존 파일은 갱신하지 않는다. 공개 example만 필요하면 옵션을 생략한다.
확인된 Telegram 발신자 ID는 `--sender-id 숫자`로 추가할 수 있다. 채팅 ID를 사용자 ID로 추정하지 않는다.
그룹 채팅에서 누가 제어할 수 있는지는 별도 확인한다. 옵션 생략 시 발신자 목록은 비어 있어 접수할 수 없다.

전달 폴더 전체는 비공개 파일 전송 수단으로 운영 컴퓨터의 새 디렉터리에 복사한다.
실제 비밀값이 들어 있으므로 Git에 추가하거나 Docker build context로 사용하지 않는다.

```text
<배포폴더>/
  README.md
  codex-exec/
    .env                         # 운영 PC 상대 경로와 게시할 이미지 태그
    compose.yaml                 # build 항목 없이 image pull만 사용
    compose.runtime.yaml         # 승인 후 serve 및 gateway 네트워크 연결
    compose.auth.yaml            # 이 운영 PC에서 Codex 로그인
    config/
      app.yaml                   # 최초 검사는 offline, 외부 수신 비활성
      app.shadow.yaml.example    # 주문 없는 연결 검증용, 아직 적용하지 않음
      strategy.yaml
      schedules.yaml
      secrets.yaml.example
      secrets.yaml               # --include-secrets일 때만
    approvals/
      runtime-manifest.json.example
      runtime.json.example
      telegram-peer.json.example
    var/                         # 운영 PC 로컬 파일시스템의 DB/토큰
    locks/                       # 동일 계좌 writer가 공유할 잠금
  telegram-gateway/
    .env                         # 이미지·네트워크·고정 IP
    compose.yaml
    config/
      routes.yaml
      telegram-v1.env.example
      codex-peer.secret.example
      telegram-v1.env             # --include-secrets일 때만
      codex-peer.secret           # --include-secrets일 때만
    memory/
```

## 2. 운영 컴퓨터: 경로·권한·네트워크

운영 PC에 Docker Engine와 `docker compose`가 있어야 한다. 이미지의 OS/CPU 아키텍처와
운영 PC가 일치하는지 확인한다. 개발 PC와 아키텍처가 다르면 해당 플랫폼 이미지 빌드가 선행돼야 한다.
DB는 운영 PC 자체의 로컬 디스크에 둔다. 개발 PC에서 SMB로 보이는 경로를 활성 DB로 사용하지 않는다.
한 계좌의 writer를 여러 배포 폴더로 나눠 실행하면 `.env`의 `DANTA_LOCK_DIR`은 같은 실제 경로여야 한다.

아래 소유권 변경은 **새 배포 폴더만** 대상으로 한다. 기존 운영 DB·인증 volume에는 일괄 적용하지 않는다.

```sh
cd <배포폴더>
sudo chown -R root:root codex-exec/config codex-exec/approvals telegram-gateway/config
sudo chmod 755 codex-exec/config codex-exec/approvals
sudo chmod 644 codex-exec/config/app.yaml codex-exec/config/strategy.yaml codex-exec/config/schedules.yaml
sudo chown 10001:10001 codex-exec/config/secrets.yaml
sudo chmod 400 codex-exec/config/secrets.yaml
sudo chown -R 10001:10001 codex-exec/var codex-exec/locks
sudo chmod 700 codex-exec/var codex-exec/locks telegram-gateway/config
sudo chmod 600 telegram-gateway/config/telegram-v1.env telegram-gateway/config/codex-peer.secret
```

gateway의 `.env`에는 별도 네트워크 `danta-catalyst-net`, 예시 subnet `172.30.85.0/24`,
고정 IP `172.30.85.3`이 들어 있다. 기존 Docker/LAN/VPN 대역과 겹치지 않는지 확인한다.
다른 대역은 개발 PC의 준비 명령에 `--gateway-subnet 대역 --gateway-ip 주소`를 지정하면
gateway `.env`와 peer example에 함께 반영된다. 네트워크 이름을 바꾸면 양쪽 `.env`를 같이 바꾼다.

```sh
# gateway/.env와 같은 이름·대역으로 최초 한 번 생성한다.
docker network create --subnet 172.30.85.0/24 danta-catalyst-net
cd telegram-gateway
docker compose config --quiet
docker compose pull
cd ../codex-exec
docker compose -f compose.yaml -f compose.runtime.yaml config --quiet
docker compose pull
docker compose run --rm codex-exec doctor
```

이미 같은 이름의 네트워크가 있으면 삭제하거나 다시 만들지 말고 `docker network inspect`로
이름·대역을 대조한다. 기존 gateway와 같은 컨테이너 이름/봇을 쓰므로 기존 poller와 동시에 시작하지 않는다.
공통 네트워크의 서비스 DNS는 `codex-exec`, `telegram-gateway`이며 호스트 IP를 secrets.yaml에 넣지 않는다.

## 3. 운영 컴퓨터: Codex 로그인

```sh
cd <배포폴더>/codex-exec
docker compose -f compose.auth.yaml pull
docker compose -f compose.auth.yaml run --rm codex-login login status
# 인증이 없으면 아래 명령의 URL/코드를 브라우저에서 완료한다.
docker compose -f compose.auth.yaml run --rm codex-login
```

login과 runtime은 `.env`의 `DANTA_AUTH_VOLUME`을 공유한다. 새 volume은 이미지의 UID10001·0700
auth 디렉터리로 초기화된다. 기존 로그인 상태가 유효하면 재로그인하지 않는다.
기존 volume의 권한 문제는 별도 확인하며 자동 초기화하지 않는다. 업데이트할 때 이름을 유지하고
`docker compose down -v`를 사용하지 않는다. 개발 PC에서 로그인한 사실은 이 운영 PC의 로그인 증거가 아니다.

## 4. 운영 검증 후 설정 확정

이 배포 준비는 운영 승인이나 실제 수수료·달력 확인을 만들지 않는다. `*.json.example`의
`verified=false`, `capabilities=[]`, null은 의도적으로 미확인 상태다. 임의로 true/0/장기 유효기간으로 바꾸지 않는다.
검증 전에는 `app.yaml`의 offline 상태로 doctor만 실행할 수 있다.

다음 항목은 운영 컴퓨터의 실제 연결 검증 단계에서 확인하고 파일을 확정한다.

| 파일 | 확인·반영할 값 |
|---|---|
| config/app.shadow.yaml.example → app.yaml | 계좌 별칭/환경, 실제 허용 sender/chat. shadow·execution=false·스케줄 비활성 유지 |
| approvals/runtime-manifest.json | 실제 달력/호가단위/수수료/가격·시간 필드/호출 제한/기업 코드·공시/계좌 귀속·자금/CLI 격리 증거와 유효기간 |
| approvals/telegram-peer.json | 고정 gateway IP, config hash, 검증 출처·만료시각, 검증된 peer임을 확인 |
| approvals/runtime.json | 같은 config/strategy/code/model/prompt, manifest SHA-256, 확인된 읽기·모델·Telegram 권한과 유효기간 |

준비된 shadow 예시는 `kis-primary`/실전 API 환경 `real`이다. 계좌 키를 실제 API에서 확인하고
다른 환경을 사용하면 app과 두 manifest의 별칭/환경을 함께 맞춘다. `strategy_cash`는 연구 자본과
계좌 귀속 정책 검증을 거쳐 확정한다. 기존 보유·미체결을 자동으로 새 전략에 인수하지 않는다.
`kis-primary`는 예시 식별자이며 실제 계좌 번호나 계좌 귀속 확인을 뜻하지 않는다.

`runtime.json`의 shadow 연결 권한은 승인된 경우에 한해 `account_read`, `market_read`,
`disclosure_read`, `model_call`, `broker_auth`, `telegram_ingress`, `telegram_send`를 명시한다.
제어 명령에는 `telegram_control`, 후보 목록 변경에는 `candidate_control`이 추가로 필요하다.
이 예시에 `live_orders`, `demo_orders`, `paper_simulation` 권한을 추가하지 않는다.

설정을 확정한 뒤 `doctor`가 반환하는 config_hash/strategy_hash/code_id와 아래 파일 hash를
운영 검증 기록에 대조한다. 초기 example의 코드 hash도 실제 pull한 이미지와 반드시 대조한다.

```sh
docker compose run --rm codex-exec doctor
docker compose run --rm --entrypoint sha256sum codex-exec /opt/codex/bin/codex /app/prompts/portfolio_decision.md
sha256sum approvals/runtime-manifest.json
sudo chown root:root approvals/*.json
sudo chmod 444 approvals/*.json
```

실제 외부 연결 검증과 Telegram 전송이 허용되고 위 파일이 확정된 뒤에만 서비스 시작 단계로 간다.
Telegram 메뉴의 일반 문장을 모델·주문 실행으로 해석하지 않는다. `/review`가 심사 요청이다.

```sh
cd <배포폴더>/codex-exec
docker compose -f compose.yaml -f compose.runtime.yaml up -d --no-build
docker compose -f compose.yaml -f compose.runtime.yaml logs --tail 100 codex-exec
cd ../telegram-gateway
docker compose up -d --no-build
```

## 5. 업데이트와 복구

새 릴리스마다 개발 PC에서 같은 태그의 두 이미지를 게시하고 새로운 준비 폴더를 만든다.
운영 PC에는 변경된 Compose/메뉴 파일을 검토해 반영하고 양쪽 `.env`의 이미지 태그를 바꾼다.
기존 secrets.yaml·gateway env/키·확정된 app/strategy/schedules·approvals·DB·잠금·인증 volume은
새 설치용 example으로 덮어쓰지 않는다. 코드/설정 hash 변경에 따른 승인 갱신을 확인한다.

활성 원장은 SQLite backup API로 복사한다. 아래 명령은 실행 중인 엔진의 현재 DB를 같은
운영 PC의 `var/backups`에 새 파일로 보관하며 원본을 바꾸지 않는다. 인증/설정은 별도로 비공개 백업한다.

```sh
cd <배포폴더>/codex-exec
docker compose -f compose.yaml -f compose.runtime.yaml exec -T codex-exec python - <<'PY'
from pathlib import Path
from datetime import datetime, timezone
import os, sqlite3
from danta.config import load_config
os.umask(0o077)
source = load_config('/app/config').state_dir / 'state.sqlite'
target = Path('/app/var/backups')
target.mkdir(mode=0o700, exist_ok=True)
target = target / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '.sqlite')
target.touch(mode=0o600, exist_ok=False)
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
    assert dst.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
print(target)
PY
docker compose -f compose.yaml -f compose.runtime.yaml pull
docker compose -f compose.yaml -f compose.runtime.yaml up -d --no-build
cd ../telegram-gateway
docker compose pull
docker compose up -d --no-build
```

복구는 서비스 중지 후 검증한 백업을 **새 상태 디렉터리**에 두고 상태 경로·권한·config hash와
승인을 재검증하는 절차다. 활성 DB/WAL을 덮어쓰거나 이미지 태그만 되돌려 계좌가 복구됐다고 판단하지 않는다.
실제 계좌와 원장을 대사한 뒤 재개하며, 기존 봇 코드로 자동 fallback하지 않는다.
