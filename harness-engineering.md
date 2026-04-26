# Harness Engineering

Harness Engineering은 LLM/agent가 안정적으로 일하게 만드는 외부 제어 시스템을 설계하는 일이다.

간단히 표현하면 다음과 같다.

```text
Agent = Model + Harness
```

모델은 추론하고 말하고 코드를 쓰는 두뇌에 가깝고, harness는 그 모델이 실제 업무를 안전하고 반복 가능하게 수행하도록 감싸는 환경, 규칙, 도구, 피드백 루프, 권한, 메모리, 평가 체계다.

OpenAI의 표현에 가깝게 말하면, 사람이 직접 코드를 쓰는 대신 환경을 설계하고, 의도를 명확히 하고, agent가 신뢰성 있게 일하도록 피드백 루프를 만드는 엔지니어링이다.

## Multi-Agent 구조와의 관계

사용자가 말한 "여러 sub-agent에 각각 룰을 설정하고, 그것을 관장하는 하나의 main agent가 있는 구조"는 Harness Engineering의 대표적인 구현 패턴 중 하나다.

예시는 다음과 같다.

```text
Human
  |
Main Agent / Orchestrator
  |
  +-- Planner Agent
  +-- Coder Agent
  +-- Reviewer Agent
  +-- Test Agent
  +-- Research Agent
  +-- CI / Tooling / Memory / Rules / Logs
```

이 구조는 보통 multi-agent orchestration 패턴으로 볼 수 있다.

다만 Harness Engineering은 단순히 main agent가 sub-agent들을 관리하는 것에 그치지 않는다. 더 중요한 부분은 다음이다.

1. 각 agent가 무엇을 해도 되는지, 무엇을 하면 안 되는지 정한다.
2. agent가 참조할 문서, 코드 구조, 룰, 컨텍스트를 설계한다.
3. agent가 실행할 수 있는 도구와 권한을 제한한다.
4. 결과를 검증할 테스트, 린트, 리뷰, 평가기를 붙인다.
5. 실패가 반복되면 프롬프트만 고치는 것이 아니라 시스템 자체를 개선한다.

## 주요 구성요소

### Guides

Guides는 agent가 행동하기 전에 방향을 잡아주는 장치다.

예:

- `AGENTS.md`
- architecture docs
- coding rules
- examples
- skills
- API docs

### Sensors

Sensors는 agent가 행동한 뒤 결과를 검증하는 장치다.

예:

- tests
- typecheck
- linter
- CI
- 로그 분석
- browser screenshot
- code review agent

### Tools

Tools는 agent가 실제로 조작할 수 있는 인터페이스다.

예:

- shell
- git
- GitHub
- browser
- database
- internal APIs
- MCP tools

### Memory / Context

Memory와 context는 agent가 필요한 지식을 저장하고 주입받는 방식이다.

예:

- repo docs
- task history
- decision records
- issue context
- prior implementation notes

### Orchestration

Orchestration은 작업 분해와 역할 분담이다.

예:

- main agent가 planner, reviewer, tester sub-agent에게 일을 나눈다.
- 각 sub-agent는 자기 역할에 맞는 규칙과 도구만 사용한다.
- main agent는 결과를 통합하고 다음 단계를 결정한다.

### Permissions / Guardrails

Permissions와 guardrails는 위험한 행동을 제한하는 장치다.

예:

- production DB 접근 금지
- destructive command 승인 필요
- 특정 파일만 수정 가능
- 외부 API 호출 제한
- 민감 정보 출력 금지

### Evaluation Loop

Evaluation loop는 agent 품질을 측정하고 개선하는 체계다.

예:

- benchmark tasks
- reward signals
- trace logging
- regression tests
- 실행 결과 비교
- 실패 케이스 수집

## Prompt Engineering과의 차이

Prompt Engineering은 보통 다음 질문에 가깝다.

```text
모델에게 어떤 말을 할 것인가
```

Harness Engineering은 다음 질문에 가깝다.

```text
모델이 일하는 전체 시스템을 어떻게 설계할 것인가
```

따라서 prompt는 harness의 일부일 뿐이다. 좋은 harness는 프롬프트뿐 아니라 문서 구조, 테스트, 리뷰 루프, 도구 권한, sub-agent 분업, 실패 복구까지 포함한다.

## 코딩 Agent Harness 예시

코딩 agent harness는 다음과 같은 흐름으로 설계할 수 있다.

```text
1. 사용자가 기능 요청
2. Main agent가 작업을 쪼갬
3. Planner agent가 구현 계획 작성
4. Coder agent가 코드 수정
5. Test agent가 테스트 실행
6. Reviewer agent가 변경사항 리뷰
7. CI가 린트/타입체크/테스트 실행
8. 실패하면 agent가 로그를 읽고 수정
9. 최종 PR 생성
10. 반복되는 실수는 AGENTS.md, 테스트, 룰, 스크립트로 시스템화
```

여기서 핵심은 sub-agent가 많다는 점이 아니라, 결과 품질을 agent 개인 능력에만 맡기지 않고 시스템적으로 제어한다는 점이다.

## 핵심 요약

Harness Engineering은 AI agent를 그냥 호출하는 것이 아니라, agent가 좋은 결과를 반복적으로 내도록 작업 환경, 역할, 도구, 규칙, 검증 루프를 엔지니어링하는 방식이다.

사용자가 말한 "main agent가 여러 sub-agent를 룰 기반으로 관리하는 구조"는 Harness Engineering의 대표적인 orchestration 형태다.

