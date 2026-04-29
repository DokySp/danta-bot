# Codex CLI

- non-active 모드를 사용하여 직접 콘솔에서 cli에 명령을 내릴 수 있습니다.
- https://developers.openai.com/codex/noninteractive

### 예시
codex에서 non active 모드로 0000 세션id에 5.5 xhigh로 "안녕"을 구동시키는 명령어
```
codex exec resume <세션ID> -m gpt-5.5 -c model_reasoning_effort=\"xhigh\" "<프롬프트 내용>"
```
