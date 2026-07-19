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

`codex-exec.env`에는 프로세스 실행 값, 해당 인스턴스의 MCP 연결 값, 그리고 `$check-portfolio` 같은 codex-exec 내부 direct API helper가 쓰는 KIS app key/secret/계좌번호를 함께 둡니다.
실제 `codex-exec.env` 파일은 git에서 무시하고, `codex-exec.env.example`만 추적합니다.

프로필 Compose는 `./config`를 `/app/config`로 writable bind mount합니다. 따라서 호스트의
`containers/codex-exec/profiles/<name>/config/schedules.yaml`, `portfolio.txt`, `portfolio-except.txt`,
`touch-points.yaml`, `execute-trade.yaml`, `codex-runtime.yaml`을 수정하면 컨테이너 안의 `/app/config`에도 즉시 보이고,
다음 Codex 실행이나 스케줄러 tick부터 새 내용이 사용됩니다. `codex-exec.env`처럼 프로세스 환경변수로 주입되는 값은
컨테이너 시작 시점에만 읽히므로 변경 후 Compose 재생성이 필요합니다. 컨테이너 안의 Codex 스킬이
config를 수정하려면 호스트 config 파일과 디렉터리가 컨테이너 실행 UID 1000에 쓰기 가능해야 합니다.

일반 Codex 실행 기본값, `/new` 프롬프트, daily-trading sub-agent 모델은 프로필별
`config/codex-runtime.yaml`에서 함께 관리합니다. 이 파일은 실행 직전에 다시 읽으므로 내용 변경에는
컨테이너 재시작이나 Compose 재생성이 필요하지 않습니다.

기존 Docker 배포를 새 이미지로 올릴 때 호스트 `config/codex-runtime.yaml`이 아직 없으면 이미지에 포함된
`/app/default-config/codex-runtime.yaml`을 fallback으로 사용하므로 컨테이너 시작은 유지됩니다. 설정을 직접
수정하고 hot reload하려면 배포 프로필의 `codex-runtime.yaml`을 호스트 `./config`에도 배치해야 합니다.

텔레그램의 `/reasoning_effort`는 현재 `defaults.model_reasoning_effort`를 표시하고,
`/reasoning_effort <값>`은 비어 있지 않은 단일 값을 그대로 저장합니다. 예: `low`, `xhigh`, `max`, `ultra`.
변경값은 다음 일반 Codex 실행부터 반영되며, 스케줄별 override와 `daily_trading` sub-agent 설정은 바꾸지 않습니다.
호스트 설정 파일이 없으면 baked fallback을 `/app/config/codex-runtime.yaml`로 생성한 뒤 변경합니다.
고정 allowlist는 두지 않으며 지원 여부는 실제 Codex CLI와 선택된 모델이 판단합니다.

```yaml
defaults:
  model: gpt-5.6-sol
  model_reasoning_effort: xhigh
  new_session_prompt: 새 대화 시작

daily_trading:
  collection:
    model: gpt-5.6-luna
    model_reasoning_effort: low
  analyst_review:
    model: gpt-5.6-sol
    model_reasoning_effort: xhigh
  judge_review:
    model: gpt-5.6-sol
    model_reasoning_effort: xhigh
```

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

- `/stop`: 현재 실행 중인 Telegram 요청의 Codex CLI 프로세스 그룹만 즉시 중지합니다. 스케줄 작업에는 영향을 주지 않습니다.
- `/session`: 기본 세션 ID, 실행 상태, 모델·effort, 작업 경로와 거래 환경을 Codex 실행 없이 표시합니다.
- `/new`: `"새 대화 시작"` 더미 메시지로 새 Codex 세션을 만들고 기본 세션으로 저장합니다.
- 일반 메시지: 저장된 기본 세션으로 `codex exec resume`을 실행합니다.

결과는 `TELEGRAM_GATEWAY_URL`의 `/sendMessage`로 `parse_mode=HTML`, `escape=false` 형태로 전송합니다.
Telegram에서 들어온 요청은 gateway가 넘긴 `route`로 다시 보내고, 스케줄 작업처럼 route가 없는 요청은 `TELEGRAM_ROUTE`를 사용합니다.
Codex가 실행 중일 때는 `TELEGRAM_TYPING_INTERVAL_SECONDS` 간격으로 gateway의 `/sendChatAction`에 `typing`을 보내 Telegram 앱에 입력 중 상태를 유지합니다.
일반 메시지와 `/new` 실행 중에는 공개 진행 메시지의 최신 상태만 같은 `draft_id`의 `/sendMessageDraft`로 갱신합니다. 마지막 `agent_message`는 draft에서 제외하고 기존 `/sendMessage`로만 최종 전송합니다.
모든 Codex 실행 직전에 `run_id`와 Asia/Seoul `started_at`을 프롬프트에 주입합니다.
`$execute-trade`, `daily-*`, `pre-open` 작업과 daily-trading 아티팩트가 생성된 간접 실행은 성공 및 실패 Telegram 메시지에 `작업 시작: YYYY-MM-DD HH:MM:SS KST`를 표시합니다.

## Schedule

`containers/codex-exec/profiles/base/config/schedules.yaml`:

```yaml
schedules:
  - id: weekday-open
    enabled: true
    cron: "0 9 * * 1-5"
    model: gpt-5.6-luna
    model_reasoning_effort: low
    route: v2
    message: |
      오늘 장 시작 전 점검을 수행해줘.
```

스케줄 작업은 채팅 기본 세션과 독립된 one-off Codex 실행으로 처리됩니다. 단, 항목에
`daily_trading` 블록이 있으면 Main Codex를 띄우지 않고 codex-exec Python direct runner가
`run_daily_trading_pipeline.py run`을 직접 실행한 뒤 `telegram-summary.txt`를 전송합니다.
일반 스케줄 항목의 `model`과 `model_reasoning_effort`는 선택 사항입니다. 생략하면
`codex-runtime.yaml`의 `defaults` 값을 사용하고, 지정하면 해당 스케줄의 Main Codex 실행에만 적용됩니다.
`daily_trading` direct runner 항목은 Main Codex를 실행하지 않으므로 이 두 값 대신
`codex-runtime.yaml`의 `daily_trading` sub-agent 모델 설정을 따릅니다.
스케줄러는 매 tick마다 `SCHEDULE_FILE`을 다시 읽습니다. `$trading-schedule-toggle` 스킬은
`/app/config/schedules.yaml`의 `daily-{number}` 항목만 on/off로 수정하며, 수정 결과는 컨테이너
재시작 없이 다음 scheduler tick부터 반영됩니다.

## Price Triggers

`TOUCH_POINT_CONFIG_FILE`은 기본적으로 `/app/config/touch-points.yaml`입니다. base 프로필은 KIS
국내업종 현재지수 API(`/uapi/domestic-stock/v1/quotations/inquire-index-price`,
`tr_id=FHPUP02100000`)의 KOSPI(`FID_COND_MRKT_DIV_CODE=U`, `FID_INPUT_ISCD=0001`)만 감시하며,
각 case의 기준값 대비 설정된 임계치에 닿으면 터치 이벤트를 기록하고, `send_telegram`이 켜진 case만
Telegram 알림을 보냅니다. 이 알림은 Codex 실행을
호출하지 않습니다. 각 case는 서로 다른 `id`를 사용하므로 같은 cache file 안에서도 기준값이
case별로 따로 저장됩니다.
`active_weekdays`는 KST 기준 cron 요일 필드 형식입니다(`0`/`7`=일요일, `1-5`=월-금).
설정하지 않으면 기존처럼 모든 요일에 실행합니다. `active_start_time`과 `active_end_time`은 KST 기준
4자리 `HHMM` 문자열 형식이며, 둘 다 설정된 경우 해당 요일과 시간 범위 안에서만 quote 조회와 touch 계산을
수행합니다.

```yaml
enabled: true
poll_seconds: 60
active_weekdays: "1-5"
active_start_time: "0900"
active_end_time: "1530"
cache_file: /state/touch-points/triggers.json
quote_history_file: /state/touch-points/quote-history.jsonl
touch_log_file: /state/touch-points/touch-events.jsonl

touch_points:
  - id: kospi-case-1
    enabled: true
    send_telegram: false
    case_title: "case 1 - 기본 민감도"
    name: KOSPI
    symbol: KOSPI
    source: kis_domestic_index
    up_percent: 1.0
    down_percent: -1.0
```

기본 case는 `case 1 - 기본 민감도`(`+1.0%`, `-1.0%`), `case 2 - 하락 민감형`(`+1.5%`,
`-0.8%`), `case 3 - 상승 추세 확인형`(`+1.2%`, `-1.8%`), `case 4 - 저소음형`(`+2.0%`,
`-2.0%`), `case 5 - 급락 감지형`(`+2.0%`, `-1.2%`), `case 6 - 강한 리스크 경보`(`+3.0%`,
`-2.0%`), `case 7 - 테스트 후보 1`(`+1.2%`, `-1.2%`)입니다.

처음 관측한 값은 알림 없이 캐시 기준값으로 저장됩니다. 이후 관측값이 기준값 대비 임계치에 닿으면
`case N - 제목`이 최상단에 포함된 `가격 조건 터치` Telegram 메시지를 보내고, 그 터치값을 해당
case의 새 기준값으로 캐시합니다.
각 case의 `send_telegram`으로 Telegram 메시지 발송 여부를 따로 제어합니다. `send_telegram: false`로
설정하면 터치 이벤트와 기준값 갱신은 그대로 수행하되 해당 case의 Telegram 메시지만 보내지 않습니다.

각 poll에서 관측된 유효 지수값은 `quote_history_file`에 JSONL로 누적됩니다. 터치 이벤트는
`touch_log_file`에 구조화 JSONL로 누적되어, `/show_touch_point {id}`가 이후 `case_title`이 바뀌어도
같은 id의 터치 이벤트를 찾을 수 있습니다. 이 명령은 KIS 지표 차트 API와 codex-exec의 터치 이벤트 로그를 함께 읽어,
해당 id가 사용하는 KIS 제공 30분봉 지표 캔들 차트 위에 알림 발생 지점을 표시합니다.
명령 형식은 `/show_touch_point kospi-case-1`입니다. KIS 30분봉 조회는 과거 데이터 포함으로
한 번만 수행하고, 반환된 최대 99개 캔들 범위를 전체 차트 범위로 사용합니다.
KIS 30분봉 차트 조회가 실패하면 `quote_history_file`을 보조 시계열로 사용하고, 지표 시계열이 없으면 점만
이어 그리지 않고 실패합니다.
Telegram Bot API의 명령명 제약 때문에 메뉴와 codex-exec 명령은 `/show_touch_point kospi-case-1`
형식을 사용합니다. 기존 `/show-touch-point kospi-case-1` 형식도 호환 처리합니다.
