# codex-exec

`telegram-gateway`에서 받은 메시지와 YAML 스케줄을 Codex CLI로 실행하는 상주형 worker입니다.

## Build

```bash
docker build -t codex-exec:1.0.0 ./codex-exec
```

## First Login

`CODEX_HOME`은 이미지에 넣지 않고 Docker volume에 저장합니다.
이미지에 포함된 `codex-exec/skills` 폴더는 컨테이너 시작 시
`$CODEX_HOME/skills`가 비어 있을 때만 한 번 복사됩니다.
복사 후에는 `$CODEX_HOME/.bundled_skills_initialized` 마커가 생기며,
다음 시작부터 Docker는 `$CODEX_HOME/skills`를 건드리지 않습니다.

```bash
docker volume create codex-home-stock-v1

docker run --rm -it \
  -e CODEX_HOME=/codex-home \
  -v codex-home-stock-v1:/codex-home \
  codex-exec:1.0.0 \
  codex login --device-auth
```

API key 방식:

```bash
read -s OPENAI_API_KEY

printf '%s' "$OPENAI_API_KEY" | docker run --rm -i \
  -e CODEX_HOME=/codex-home \
  -v codex-home-stock-v1:/codex-home \
  codex-exec:1.0.0 \
  codex login --with-api-key
```

확인:

```bash
docker run --rm \
  -e CODEX_HOME=/codex-home \
  -v codex-home-stock-v1:/codex-home \
  codex-exec:1.0.0 \
  codex login status
```

스킬을 이미지 기준으로 다시 초기화하고 싶으면 volume 안의
`$CODEX_HOME/skills`와 `$CODEX_HOME/.bundled_skills_initialized`를 직접 정리한 뒤
컨테이너를 다시 시작해야 합니다.

## Telegram Commands

- `/new`: `"새 대화 시작"` 더미 메시지로 새 Codex 세션을 만들고 기본 세션으로 저장합니다.
- 일반 메시지: 저장된 기본 세션으로 `codex exec resume`을 실행합니다.

결과는 `TELEGRAM_GATEWAY_URL`의 `/sendMessage`로 `parse_mode=HTML`, `escape=false` 형태로 전송합니다.
Telegram에서 들어온 요청은 gateway가 넘긴 `route`로 다시 보내고, 스케줄 작업처럼 route가 없는 요청은 `TELEGRAM_ROUTE`를 사용합니다.

## Schedule

`config/schedules.yaml`:

```yaml
schedules:
  - id: weekday-open
    enabled: true
    cron: "0 9 * * 1-5"
    message: |
      오늘 장 시작 전 점검을 수행해줘.
```

스케줄 작업은 채팅 기본 세션과 독립된 one-off Codex 실행으로 처리됩니다.
