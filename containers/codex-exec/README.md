# codex-exec

`telegram-gateway`에서 받은 메시지와 YAML 스케줄을 Codex CLI로 실행하는 상주형 worker입니다.

## Build

```bash
docker build -f ./containers/codex-exec/Dockerfile \
  --build-arg APP_VERSION=1.0.0 \
  --build-arg CODEX_EXEC_PROFILE=base \
  --build-arg IMAGE_TITLE=codex-exec \
  -t codex-exec:1.0.0 \
  ./containers
```

## First Login

`CODEX_HOME`은 이미지에 넣지 않고 Docker volume에 저장합니다.
이미지에 포함된 공용 `containers/codex-exec/shared-skills`와 프로필별 `containers/codex-exec/profiles/<name>/skills`는 컨테이너 시작 시
`$CODEX_HOME/skills`로 동기화됩니다.
기존 스킬은 `CODEX_SYNC_SKILLS_OVERWRITE=true`일 때만 삭제한 뒤 다시 복사됩니다.
동기화 후에는 `$CODEX_HOME/.bundled_skills_initialized` 마커에 복사/교체/스킵 수가 기록됩니다.

```bash
docker volume create codex-home

docker run --rm -it \
  -e CODEX_HOME=/codex-home \
  -e CODEX_SYNC_SKILLS_OVERWRITE=false \
  -v codex-home:/codex-home \
  codex-exec:1.0.0 \
  codex login --device-auth
```

API key 방식:

```bash
read -s OPENAI_API_KEY

printf '%s' "$OPENAI_API_KEY" | docker run --rm -i \
  -e CODEX_HOME=/codex-home \
  -e CODEX_SYNC_SKILLS_OVERWRITE=false \
  -v codex-home:/codex-home \
  codex-exec:1.0.0 \
  codex login --with-api-key
```

확인:

```bash
docker run --rm \
  -e CODEX_HOME=/codex-home \
  -e CODEX_SYNC_SKILLS_OVERWRITE=false \
  -v codex-home:/codex-home \
  codex-exec:1.0.0 \
  codex login status
```

스킬 내용을 이미지 기준으로 강제로 다시 맞추려면 `CODEX_SYNC_SKILLS_OVERWRITE=true`로 컨테이너를 시작합니다.

## Runtime Env

Compose 실행 값은 프로필별 `containers/codex-exec/profiles/<name>/config/codex-exec.env`로 주입합니다.
프로필 Compose 파일은 `containers/codex-exec/profiles/<name>/compose.yaml`에 있으므로, Compose 안에서는 `./config/codex-exec.env`로 참조합니다.

```yaml
env_file:
  - ./config/codex-exec.env
```

`codex-exec.env`에는 Codex 실행 값, 해당 인스턴스의 MCP 연결 값, 그리고 `$check-portfolio` 같은 codex-exec 내부 direct API helper가 쓰는 KIS app key/secret/계좌번호를 함께 둡니다.
실제 `codex-exec.env` 파일은 git에서 무시하고, `codex-exec.env.example`만 추적합니다.

프로필 Compose는 `./config`를 `/app/config`로 writable bind mount합니다. 따라서 호스트의
`containers/codex-exec/profiles/<name>/config/schedules.yaml`, `portfolio.txt`,
`default-trade-prompt`를 수정하면 컨테이너 안의 `/app/config`에도 즉시 보이고, 다음 Codex 실행이나
스케줄러 tick부터 새 내용이 사용됩니다. `codex-exec.env`처럼 프로세스 환경변수로 주입되는 값은
컨테이너 시작 시점에만 읽히므로 변경 후 Compose 재생성이 필요합니다. 컨테이너 안의 Codex 스킬이
config를 수정하려면 호스트 config 파일과 디렉터리가 컨테이너 실행 UID 1000에 쓰기 가능해야 합니다.

## Codex MCP Config

인스턴스별 Codex MCP 설정은 `containers/codex-exec/profiles/<name>/config/codex-exec.env`로 주입합니다.
컨테이너 시작 시 entrypoint가 기존 `/codex-home/config.toml`은 보존하고, 관리 블록만 추가/갱신합니다.

```yaml
env_file:
  - ./config/codex-exec.env
```

이 방식은 `/codex-home/config.toml`을 덮어씌우지 않습니다. 기존 설정 파일이 있으면 그대로 두고, 아래처럼 표시된 블록만 관리합니다.

```toml
# BEGIN codex-exec managed: env-mcp.toml
...
# END codex-exec managed: env-mcp.toml
```

Compose 네트워크 안에서는 `localhost`가 아니라 서비스명을 사용합니다.

```dotenv
CODEX_MCP_SERVER_NAME=kis-trade-mcp
CODEX_MCP_COMMAND=npx
CODEX_MCP_ARGS_TOML=["-y","mcp-remote","http://kis-trade-mcp:3000/sse","--allow-http"]
CODEX_MCP_ENABLED=true
CODEX_MCP_TRADING_ENV=paper
```

`CODEX_MCP_TRADING_ENV`는 `paper` 또는 `acct`만 허용합니다. `paper`는 KIS MCP 호출의
`env_dv="demo"`로, `acct`는 `env_dv="real"`로 매핑되며 스케줄 메시지나 사용자 요청의
모의/실전 표현보다 우선합니다.

## Telegram Commands

- `/new`: `"새 대화 시작"` 더미 메시지로 새 Codex 세션을 만들고 기본 세션으로 저장합니다.
- 일반 메시지: 저장된 기본 세션으로 `codex exec resume`을 실행합니다.

결과는 `TELEGRAM_GATEWAY_URL`의 `/sendMessage`로 `parse_mode=HTML`, `escape=false` 형태로 전송합니다.
Telegram에서 들어온 요청은 gateway가 넘긴 `route`로 다시 보내고, 스케줄 작업처럼 route가 없는 요청은 `TELEGRAM_ROUTE`를 사용합니다.
Codex가 실행 중일 때는 `TELEGRAM_TYPING_INTERVAL_SECONDS` 간격으로 gateway의 `/sendChatAction`에 `typing`을 보내 Telegram 앱에 입력 중 상태를 유지합니다.
모든 Codex 실행 직전에 `run_id`와 Asia/Seoul `started_at`을 프롬프트에 주입합니다.
`$daily-trading`, `$execute-trade`, `daily-*`, `pre-open` 작업과 daily-trading 아티팩트가 생성된 간접 실행은 성공 및 실패 Telegram 메시지에 `작업 시작: YYYY-MM-DD HH:MM:SS KST`를 표시합니다.

## Schedule

`containers/codex-exec/profiles/base/config/schedules.yaml`:

```yaml
schedules:
  - id: weekday-open
    enabled: true
    cron: "0 9 * * 1-5"
    route: v2
    message: |
      오늘 장 시작 전 점검을 수행해줘.
```

스케줄 작업은 채팅 기본 세션과 독립된 one-off Codex 실행으로 처리됩니다.
스케줄러는 매 tick마다 `SCHEDULE_FILE`을 다시 읽습니다. `$trading-schedule-toggle` 스킬은
`/app/config/schedules.yaml`의 `daily-{number}` 항목만 on/off로 수정하며, 수정 결과는 컨테이너
재시작 없이 다음 scheduler tick부터 반영됩니다.
