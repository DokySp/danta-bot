# 단타봇

- **KIS open-trading-api MCP**를 활용하여 다양한 거래 자동화 기법을 테스트해본다.

## KIS MCP
- https://github.com/koreainvestment/open-trading-api
- https://apiportal.koreainvestment.com/tools
- https://apiportal.koreainvestment.com/tools-trading
- https://github.com/koreainvestment/open-trading-api/blob/main/MCP/Kis%20Trading%20MCP/Readme.md

## Docker 구성

- `trading-engine`: 명세에 따른 투자 연구·실행 엔진. 기본 설정은 offline이며 실제 거래는 별도 승인과 운영 설정이 필요합니다.
- `telegram-gateway`: 텔레그램 송수신 컨테이너.
- `kis-trade-mcp`: 한국투자증권 MCP 컨테이너. 새 엔진의 KIS 어댑터는 직접 API를 호출합니다.

명세는 [trading-engine README](containers/trading-engine/README.md), 현재 검증 범위는
[구현 상태](containers/trading-engine/docs/status.md), 실행·복구 절차는
[runbook](containers/trading-engine/docs/runbook.md)에 있습니다.

## 테스트

Docker가 실행 중이면 로컬 Python에 패키지를 설치하지 않고 테스트할 수 있습니다.

```bash
python3 scripts/run_tests.py --docker
```

Python 3.12와 고정 의존성은 테스트 이미지에 자동으로 준비되고 다음 실행부터 Docker
빌드 캐시를 재사용합니다. Git에서 무시되는 비밀 설정·인증·데이터와 레거시는 테스트
이미지에 포함하지 않습니다. 테스트 컨테이너는 외부 네트워크 없이 실행합니다.
로컬 Python 환경에서 직접 테스트하려면 다음 명령을 사용합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r containers/trading-engine/requirements.lock
.venv/bin/python scripts/run_tests.py
```

이 명령은 새 `trading-engine/tests`, `telegram-gateway/tests`, `scripts/tests`를 각각
독립된 `unittest discover`로 실행합니다. 어느 스위트든 실패하거나 테스트를 0개 발견하면
실패하며, 레거시 스킬 테스트는 실행하지 않습니다. 외부 API·실제 계좌·모델 연결 없이
검증합니다. 실제 연결과 거래 승인은 테스트 통과와 별개입니다.

## Docker 이미지 빌드/배포

기존 명령 형식으로 이미지 빌드와 Docker Hub 푸시를 실행할 수 있습니다.

```bash
docker login -u dokysp
./scripts/deploy-trading-engine.sh dokysp
./scripts/deploy-telegram-gateway.sh dokysp
```

버전을 생략하면 두 이미지 모두 `latest`로 푸시하며 `APP_VERSION`은 git describe 결과입니다.
로컬 `python3`는 표준 라이브러리만 사용하는 테스트 실행기를 시작하고, 실제 회귀 테스트는
Docker의 Python 3.12와 고정 의존성으로 실행합니다. 별도 `PYTHON_BIN` 설정이나
가상환경 활성화는 필요하지 않습니다. 스크립트는 전체 회귀 검증을 통과한 뒤
해당 서비스 폴더만 빌드 컨텍스트로 보냅니다. 이어서 실제 두 이미지를 임시 내부 네트워크에
연결해 버전·준비 상태·오류 응답을 검사한 뒤 이미지를 푸시합니다. 상대 이미지가 필요하면
동일 소스에서 검증용으로 빌드하며 별도로 푸시하지 않습니다. 버전을 직접 넘기면
이미지 태그와 `APP_VERSION`에 같은 값이 들어갑니다. `APP_VERSION`은 OCI 버전 라벨과
이미지 환경변수이며, Codex CLI 버전은 Dockerfile에 고정되어 있습니다.

이 스크립트가 끝나도 NAS 파일 동기화나 컨테이너 재시작은 수행되지 않습니다.
운영 대상에서 이미지 pull과 Compose 재생성을 별도로 수행해야 합니다.

로컬 이미지 검증은 푸시 없이 실행할 수 있습니다.

```bash
docker build --build-arg APP_VERSION=local -t danta-trading-engine:local ./containers/trading-engine
PYTHONPATH=containers/trading-engine/src .venv/bin/python containers/trading-engine/scripts/verify_offline_image.py --image danta-trading-engine:local
```

검증은 네트워크가 없는 임시 컨테이너에서 합성 데이터만 사용합니다.
실제 비밀값·계좌·기존 운영 데이터를 이미지 검증에 마운트하지 않습니다.

## Docker Compose와 설정

각 서비스 폴더는 **`compose.yaml` 하나와 `config/` 하나**를 사용합니다.
Compose에는 `dokysp/trading-engine:latest`, `dokysp/telegram-gateway:latest`가 직접 적혀
있으며 별도 `.env`, 실행용 override, 로그인용 Compose는 필요하지 않습니다.

```text
trading-engine/
  compose.yaml
  config/
    app.yaml, strategy.yaml, schedules.yaml
    secrets.yaml              # KIS·DART 비밀값과 연결 주소
    runtime.json              # 확정된 운영 승인 (외부 연결 시)
    runtime-manifest.json     # 검증된 운영 정보 (외부 연결 시)
    *.example                 # 작성 형식 참고용
  var/                        # 실행 시 자동 생성: 상태·발급 토큰
  locks/                      # 실행 시 자동 생성: 중복 실행 방지
telegram-gateway/
  compose.yaml
  config/
    routes.yaml               # 엔진 연결과 Telegram 메뉴
    telegram.env              # 봇 토큰·허용 채팅
  memory/                     # 실행 시 자동 생성: 대화·첨부 기록
```

설정과 비밀값은 이미지에 들어가지 않으므로 서버에는 각 `config/`를 전달합니다.
엔진 컨테이너는 `trading-engine` 하나입니다. 시작 시 내부에서 권한을 준비한 뒤 UID 10001과
읽기 전용 config로 실행하며, 기본 명령은 `serve`입니다. `runtime.json`은 config에서 자동으로
찾되 기존 내용·유효기간·권한 검사를 통과해야 사용합니다. 관리용 HTTP와 Telegram `/version`은
거래 설정과 독립적으로 작동합니다. `/status`는 누락된 설정을 표시합니다. 기본 app.yaml은
offline이며 실제 계좌·모델·주문 준비가 완료되지 않은 상태를 명시합니다.

Codex 인증은 운영 컴퓨터에서 기존처럼 `codex login`으로 생성하며, 로그인과 엔진이
`trading-engine-auth` Docker volume을 공유합니다. 인증 파일을 직접 작성하거나 옮길 필요는 없습니다.
로그인도 같은 Compose를 사용합니다. 실제 명령과 최초 설치·업데이트 순서는
[배포 절차](containers/trading-engine/deployment/README.md) 한 곳에서 확인합니다.

`var`는 운영 서버의 로컬 디스크에 둡니다. NAS로 파일을 SMB 전송하는 것과 NAS의 Docker가
자기 로컬 디스크에서 실행하는 것은 별개이며, 활성 DB를 SMB/NFS 마운트 위에서 실행하지 않습니다.
레거시 파일은 새 이미지와 런타임에 포함되지 않습니다.

설정 묶음을 만드는 `scripts/prepare-trading-deployment.py`는 선택 사항입니다.
이미 필요한 config가 있으면 직접 복사하면 됩니다. 생성 결과도 각 서비스의 Compose 하나와
config뿐이며 `.env`나 데이터 디렉터리는 만들지 않습니다. 실제 비밀값은 `--include-secrets`를
지정했을 때만 복사합니다. 실제 runtime 파일이 있으면 내용을 그대로 보존해 함께 복사하며,
없으면 example만 제공합니다. 검증되지 않은 운영 정보를 자동 승인하지 않습니다.

Telegram은 `trading-engine` route와 `config/telegram.env` 하나를 사용합니다. 두 서비스는
`danta-bot-net` 네트워크로 연결하며 엔진 포트를 호스트에 공개하지 않습니다.
허용 sender/chat과 제어·거래 권한 검사는 유지합니다. `/report`는 승인된 설정에서 일일
HTML 파일을 전달합니다. 상세 동작은 [runbook](containers/trading-engine/docs/runbook.md)을 참고하세요.

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
