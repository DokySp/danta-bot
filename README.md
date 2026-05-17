# 단타봇

- **KIS open-trading-api MCP**를 활용하여 다양한 거래 자동화 기법을 테스트해본다.

## KIS MCP
- https://github.com/koreainvestment/open-trading-api
- https://apiportal.koreainvestment.com/tools
- https://apiportal.koreainvestment.com/tools-trading
- https://github.com/koreainvestment/open-trading-api/blob/main/MCP/Kis%20Trading%20MCP/Readme.md

## Docker 구성
- telegram-gateway: 텔레그램 송수신 컨테이너
- codex-exec: codex 예약 및 작업 수행 컨테이너. 버전별로 스킬 및 스케줄링 관리.
- kis-trading-mcp: 한국투자증권에서 제작한 컨테이너.

## Docker 이미지 빌드

```bash
$ docker build -t telegram-gateway:1.0.0 ./containers/telegram-gateway
$ docker build -t codex-exec:1.0.0 ./containers/codex-exec

$ docker save -o ./containers/images/telegram-gateway-1.0.0.tar telegram-gateway:1.0.0
$ docker save -o ./containers/images/codex-exec-1.0.0.tar codex-exec:1.0.0
```

## Docker 내에 Codex CLI 로그인

```bash
$ docker exec -it codex-exec-stock-v1 bash
```

## Docker Compose 실행

`telegram-gateway`와 `danta-bot-v1_1`은 분리해서 실행합니다. 두 compose는 외부 공용 네트워크 `danta-bot-net`을 사용하므로 최초 1회 네트워크를 먼저 만듭니다.

```bash
$ cd containers
$ docker network create danta-bot-net
$ docker compose -f telegram-gateway/telegram-gateway.yml up -d
$ docker compose -f danta-bot-v1_1.yml up -d
```

이미 네트워크가 있으면 `docker network create`는 한 번만 실행하면 됩니다.

```bash
$ docker compose -f danta-bot-v1_1.yml down
$ docker compose -f telegram-gateway/telegram-gateway.yml down
```

## 배포 시, 환경 구조

```
containers/
  danta-bot-v1_1.yml

  telegram-gateway/
    telegram-gateway.yml
    configs/
      routes.yaml     # 라우팅 설정
    envs/
      .env.v1         # telegram 봇 연결을 위한 환경변수 설정
    ...

  codex-exec/
    envs/
      .env.common     # Codex 환경 설정
      .env.mcp        # kis-trade-mcp에 대한 MCP 설정
    configs/
      schedules.yaml  # 스케줄링 설정

  trade/
    envs/.env.v1      # kis-trade-mcp 환경변수 설정
    ...
```

## Codex CLI

### 개요
- [Codex CLI 정리](./codex-cli.md)
- 로컬에서 구동되는 agent로, MCP를 연결 및 채널 설정 등 다양한 기능을 지원합니다.

## Harness Engineering

### 개요
- [Harness Engineering 정리](./harness-engineering.md)
- Harness Engineering은 LLM/agent가 안정적으로 일하게 만드는 외부 제어 시스템을 설계하는 일입니다.
- `Agent = Model + Harness`
- OpenAI의 표현에 가깝게 말하면, 사람이 직접 코드를 쓰는 대신 환경을 설계하고, 의도를 명확히 하고, agent가 신뢰성 있게 일하도록 피드백 루프를 만드는 엔지니어링입니다.
- multi-agent orchestration 샘플 구조
    ```
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

### Harness Engineering을 통한 거래 Skill 작성

- [sample 폴더](./sample/) 참조

## MCP를 활용한 Rule base 거래 테스트

### 테스트 분석 프롬프트
```
2026년 3월 1일 기준으로 오늘까지 삼성전자(005930) 주가 그래프를 가지고 아래 조건으로 거래한다고 쳤을 때 수익률을 계산해줘.
실제 주가가 3월 1일부터 오늘 날짜까지 얼마나 올랐는지도 같이 보여줘.
매도, 매수 타이밍에 삼성전자 관련 뉴스를 분석해서 해당 날짜의 뉴스가 삼성전자 주가에 어떠한 영향을 주었는지도 거래표에 같이 표기해줘. 긍정인지 부정인지 +, -로 추가 열로 표기해줘

- 투자금: 10,000,000원
- 조건
  - 거래 시작일에 전량 주식을 매수하고 시작한다.
  - 직전 5 거래일 동안 종가 기준으로 주식이 세번 올랐으면 그 다음날 200만원어치를 매수한다.
    - 잔고가 200만원 보다 부족한 경우, 남은 잔고를 모두 사용하여 매수한다.
  - 직전 5 거래일 내에 종가 기준으로 두 번 주식이 하락했다면 그 다음날 100만원 어치 주식을 매도한다.
    - 주식 보유량이 100만원 보다 부족한 경우, 남은 잔고를 모두 사용하여 매수한다.
  - 위 조건은 각각 독립시행으로 신호 발생 후 연속일수는 초기화하지 않는다.
```

### 결과
```
전략 최종 평가액: 9,172,800원
전략 손익: -827,200원
전략 수익률: -8.27%
최종 보유: 현금 1,929,300원 + 삼성전자 33주
첫 거래일 종가 기준 실제 주가: 195,100원 → 219,500원, +12.51%
실제 투자 시작 체결가 기준: 209,500원 → 219,500원, +4.77%

날짜	매매	가격	수량	금액	현금	보유	+/-	뉴스 영향 요약
03-03	초기매수	209,500	47	9,846,500	153,500	47	-	이란 쇼크, 코스피 급락, 20만전자 붕괴
03-11	매도	193,000	5	965,000	1,118,500	42	+	유가 하락, 자사주 소각·반도체 회복 기대
03-12	매도	186,600	5	933,000	2,051,500	37	-	AI/HBM 기대는 있었지만 외국인 매도와 약세
03-12	매수	186,600	10	1,866,000	185,500	47	-	수급 부담 우세
03-13	매도	180,000	5	900,000	1,085,500	42	-	외국인·기관 매도, 중동 리스크
...
```

### 뉴스 기반 분석 프롬프트
```
2026년 3월 1일 기준으로 오늘까지 삼성전자(005930) 주가 그래프를 가지고 아래 조건으로 거래한다고 쳤을 때 수익률을 계산해줘.
실제 주가가 3월 1일부터 오늘 날짜까지 얼마나 올랐는지도 같이 보여줘.
매도, 매수 타이밍에 삼성전자 관련 뉴스를 분석해서 해당 날짜의 뉴스가 삼성전자 주가에 어떠한 영향을 주었는지도 거래표에 같이 표기해줘. 긍정인지 부정인지 5점척도 점수를 같이 표기해줘 (-2 ~ +2)
뉴스 출처가 어떻게 되는지도 함께 알려줘 (mcp인지 직접 웹 검색을 한 것인지)

- 투자금: 10,000,000원
- 조건
  - 거래 시작일에 전량 주식을 매수하고 시작한다.
  - 뉴스의 경우에는 5점 척도로 긍정과 부정을 분석한다.
  - 2점: 200만원어치 주식을 매수한다.
  - 1점: 100만원어치 주식을 매수한다.
  - -2점: 300만원어치 주식을 매도한다.
```

### 결과
```
기준을 이렇게 잡았습니다.

- 데이터 출처: KIS MCP domestic_stock
- 뉴스 출처: KIS MCP news_title, 직접 웹 검색은 사용하지 않음
- 최신 일봉: 2026-04-24
- 2026-04-27 기준 조회했지만, KIS가 반환한 최신 삼성전자 일봉은 4월 24일입니다.
- 2026-03-01은 휴일이라 첫 거래일 2026-03-03부터 계산
- 뉴스 점수는 당일 뉴스 제목을 보고 매긴 뒤, 다음 거래일 시가에 체결
- 수수료/세금 제외, 정수 주식만 매매

결과 요약

- 전략 최종 평가액: 8,767,000원
- 손익: -1,233,000원
- 수익률: -12.33%
- 최종 보유: 현금 206,500원 + 삼성전자 39주
- 실제 주가 상승률: 195,100원 → 219,500원 = +12.51%
- 실제 첫 매수가 기준: 209,500원 → 219,500원 = +4.77%

4월 21일 이후에도 긍정 뉴스 신호가 있었지만 현금이 206,500원뿐이라 1주도 추가 매수하지 못했습니다. 4월 24일 뉴스는 -2로 봤지만 다음 거래일 데이터가 없어서 매도 체결은 반영하지 않았습니다.

정리하면, 이 전략은 초반 악재에 대응해 일부 매도했지만, 시작일 전량 매수 가격이 높았고 이후 반등 구간에서 다시 높은 가격에 재매수하면서 최종적으로 **-12.33%**가 나왔습니다. 삼성전자 자체는 같은 기간 종가 기준 +12.51% 올랐습니다.
```
