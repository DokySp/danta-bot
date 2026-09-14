# 외부 어댑터 계약 확인

확인일: 2026-09-13. **실제 계좌·KIS/DART 인증·실제 모델·배포 gateway 연결은 실행하지 않았다.** 아래 공식 문서와 신규 합성 wire fixture로 구현했다. `EXTERNAL_INTEGRATION_UNVERIFIED`, `LIVE_NOT_AUTHORIZED` 상태다. 구 프로젝트 소스/로그/프롬프트는 입력으로 사용하지 않았다.

## KIS REST

실전 origin은 `https://openapi.koreainvestment.com:9443`, 모의는 `https://openapivts.koreainvestment.com:29443`이다. 환경과 mode/승인을 교차검사하며 기본 HTTP transport는 네트워크를 차단한다. 명시적으로 주입한 승인된 transport만 실제 연결할 수 있다. TLS 검증을 해제하지 않으며 리다이렉트/환경 proxy 상속을 허용하지 않는다. [공식 API 개요](https://apiportal.koreainvestment.com/apiservice-summary)

| 기능 | path (`/uapi/domestic-stock/v1/` 기준) | 실전 TR | 모의 TR |
|---|---|---|---|
| 잔고 | `trading/inquire-balance` | TTTC8434R | VTTC8434R |
| 종목·가격별 주문 가능 자원 | `trading/inquire-psbl-order` | TTTC8908R | VTTC8908R |
| 최근 주문/누적 체결 | `trading/inquire-daily-ccld` | TTTC0081R | VTTC0081R |
| 3개월 이전 주문 | 동일 | CTSC9215R | VTSC9215R |
| 지정가 매수 | `trading/order-cash` | TTTC0012U | VTTC0012U |
| 시장가/지정가 매도 | 동일 | TTTC0011U | VTTC0011U |
| 정정/취소 | `trading/order-rvsecncl` | TTTC0013U | VTTC0013U |
| 현재가 | `quotations/inquire-price` | FHKST01010100 | 동일 |
| 호가 | `quotations/inquire-asking-price-exp-ccn` | FHKST01010200 | 동일 |
| 종목 일봉 | `quotations/inquire-daily-itemchartprice` | FHKST03010100 | 동일 |
| 소속시장 지수 일봉 | `quotations/inquire-daily-indexchartprice` | FHKUP03500100 | 동일 |

위 표는 공식 저장소의 각 신규 sample에 맞췄다. [잔고](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_balance/inquire_balance.py), [주문 가능](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_psbl_order/inquire_psbl_order.py), [주문/체결](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/inquire_daily_ccld.py), [현금주문](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_cash/order_cash.py), [정정취소](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/order_rvsecncl/order_rvsecncl.py), [현재가](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_price/inquire_price.py), [호가](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_asking_price_exp_ccn/inquire_asking_price_exp_ccn.py), [종목 일봉](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py), [지수 일봉](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_indexchartprice/inquire_daily_indexchartprice.py).

잔고·주문은 `tr_cont=F/M`이면 `CTX_AREA_FK100/NK100`을 반영하고 후속 `tr_cont=N`으로 조회한다. cursor 누락/반복, 페이지 한도, 중간 실패는 `PARTIAL/FETCH_FAILED`이다. `read_account()`의 잔고 완료와 특정 종목의 주문 가능 자원 조회 완료는 별도 필드다. raw output2 현금/평가 원문도 보존한다. 종목 일봉은 날짜 범위를 과거로 이동해 여러 페이지를 수집하고 세션 달력으로 충분한 coverage를 확인한다.

주문은 KRX로 고정한다. `ORD_DVSN=00` 지정가, `01` 시장가를 사용하고 매수는 지정가만 허용한다. 취소는 `RVSE_CNCL_DVSN_CD=02`, 정정은 `01`; 실제 잔량만 전달하고 `QTY_ALL_ORD_YN=N`이다. 주문 POST에는 자동 재전송/자동 토큰 갱신이 없다. 성공 JSON에도 주문 ID가 없으면 `UNKNOWN`, 정상 브로커 거부는 `REJECTED`, 전송/응답 불명은 `UNKNOWN`이다. strategy/execution의 최신 호가단위·한도·승인 검사가 선행해야 한다.

종목 정보는 공식 KOSPI/KOSDAQ master ZIP의 cp949 고정 필드로 읽는다. suffix는 개행 제외 각각 227/221자다. SPAC/ETP/우선주/정지/관리 등의 **제공자 원시 코드**를 보존하며 미확인/빈 코드를 정상으로 바꾸지 않는다. 현재 master는 과거 시점 모집단이 아니다. [KOSPI 규격](https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/kis_kospi_code_mst.py), [KOSDAQ 규격](https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/kis_kosdaq_code_mst.py)

지수 코드는 KOSPI `0001`, KOSDAQ `1001`, 업종 구분 `U`다. 지수 일봉의 `stck_bsop_date/bstp_nmix_prpr/bstp_nmix_oprc/bstp_nmix_hgpr/bstp_nmix_lwpr`를 그대로 보존한다. [공식 지수 코드 설명](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_index_daily_price/inquire_index_daily_price.py)

`ord_tmd`는 주문시각이며 첫 체결시각으로 바꾸지 않는다. `read_fills`는 주문별 누적 수량/금액이며 개별 fill ID를 만들지 않는다. 이 endpoint의 주문별 실제 체결 수수료·세금 및 first-fill 정확시각은 이번 문서 확인으로 확정하지 못했다. 추정 제비용/주문시각을 실제 비용/체결시각으로 승격하지 않는다. [공식 응답 필드](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_ccld/chk_inquire_daily_ccld.py)

인증은 별도 명시적 `issue_token()`으로 `/oauth2/tokenP`에 `grant_type=client_credentials/appkey/appsecret`를 보내며 반환 token은 repr에서 숨긴다. 자동 호출되지 않으며 `broker_auth` 승인과 환경 확인이 필요하다. [공식 인증 sample](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/kis_auth.py)

WebSocket `subscribe_quotes`는 검증된 운영 capability가 없어 명시적으로 거부한다. 구현된 fallback은 승인된 REST polling이다. 스트림/native stop/보호 체결 가능성이 검증됐다고 보고하지 않는다. 현재 호가의 제공자 시각과 세션 날짜, 수정주가의 과거 시점 조정 정의, API 속도 한도, 실제 계좌 귀속·비용은 운영 capability manifest와 별도 연결 검증 대상이다.

## OpenDART와 공식 IR

`list.json`은 날짜/기업/page_no/page_count=100으로 전체 페이지를 조회한다. `last_reprt_at=N`으로 원보고서와 정정을 보존한다. 전체기업 조회는 공식 3개월 제한보다 보수적인 최대 89일로 분할한다. `013`은 첫 페이지의 빈 기간일 때만 `COMPLETE_NO_EVENT`; 인증 오류/한도/중간 누락은 실패/부분 결과다. 페이지 중 total_count 변동·중복 접수번호는 완료가 아니다. 목록에서 확인되는 일자를 정확한 장중 공개시각으로 만들지 않는다. [공식 목록 규격](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019001)

`document.xml`은 접수번호별 ZIP 원본을 읽어 원문 hash와 수집시각을 남긴다. ZIP 내부 경로를 filesystem에 추출하지 않는다. `corpCode.xml`은 종목 코드와 회사 고유번호의 중복/형식을 검증한다. 공시 제목만으로 수치/정정의 부모 관계를 확정하지 않는다. [원문 규격](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019003), [고유번호 규격](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018)

공식 IR은 승인 목록의 정확한 HTTPS hostname만 허용하고 리다이렉트를 따라가지 않는다. 원문 hash를 보존하지만 공개시각의 검증은 별도 증거가 필요하다. 신규 유료 데이터 서비스는 추가하지 않았다.

## Codex CLI와 모델 도구 경계

설치 `codex-cli 0.153.4`의 `exec --help/features list/--version`을 확인했다. `--json`, `--output-schema`, `--output-last-message`, `--ignore-user-config`, `--ignore-rules`, `--strict-config`, `--ephemeral`을 사용한다. 프로세스 종료·JSONL 이벤트·turn 완료·새 final 파일·schema·의미 검증을 순서대로 적용한다. stdout 마지막 줄이나 이전 final로 성공을 대체하지 않는다. [공식 비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode)

실제 model ID/effort/auth mode가 없으면 호출하지 않는다. `forced_login_method`는 승인된 `chatgpt/api`로 명시한다. 모델 fallback은 없다. quota는 제공자/모델별 회로에 저장하고 reset 시각이 없으면 운영자 확인 전 재호출하지 않는다. transient는 5초 뒤 한 번, schema 오류는 같은 사실 입력으로 한 번만 교정한다. timeout/semantic 거부는 재시도하지 않는다. [공식 설정 계약](https://learn.chatgpt.com/docs/config-file/config-reference)

판단 subprocess는 PATH/LANG/HOME/CODEX_HOME만 받으며 KIS/DART/Telegram 환경변수를 상속하지 않는다. 별도 auth home을 사용하고 shell/unified_exec/apps/plugins/browser/computer/code-mode/subagent/hooks/memory/image/workspace-dependency 기능을 비활성화한다. 읽기 MCP 프로세스는 지정된 frozen JSON 파일만 로드하며 모델 도구 인자로 파일 경로나 외부 HTTP를 받지 않는다. read-only sandbox만으로 인증정보가 격리된다고 가정하지 않는다. [공식 보안 경계](https://learn.chatgpt.com/docs/security)

명세의 6개 도구 `get_event/get_fact/get_bars/get_candidate/get_position_thesis/search_official_evidence`만 market namespace에 등록한다. 기업/기간/공식 도메인/페이지/응답 크기를 검사한다. 부속 자료는 `tool_records`, 범위는 `tool_scope`로 제공하며 조회 결과·새 available_at·원문 hash·입력 snapshot ID를 attempt의 `lookup-manifest.jsonl`에 기록한다. 큰 원문 부속을 제외한 frozen 입력 전체는 첫 prompt에 들어간다.

재현용 `tests/fixtures/adapters_cli_probe.py --run`은 **임시 loopback mock provider와 가짜 응답만** 사용한다. 실제 CLI로 tool inventory를 관측하고, 서버가 강제로 반환한 `exec_command/apply_patch`가 거부되고 auth canary가 노출되지 않는지 확인한다. 결과는 `adapters_cli_probe_result.json`이다. CLI에는 MCP resource enumeration/읽기와 request_user_input builtin도 남으나, 연결된 MCP는 market 한 개이며 그 서버는 임의 resource/path를 제공하지 않는다. 이 probe는 실제 운영 auth mount/승인/배포 환경 검증을 대체하지 않는다. runtime은 승인된 isolation probe가 없으면 `SPEC_GAP_MODEL_ISOLATION_UNVERIFIED`로 차단한다.

## Telegram과 스케줄

gateway 통신은 제공 명세 §11.3을 사용한다. 기본 ingress off이며 신뢰 transport가 제공한 peer ID와 sender/chat allowlist가 모두 맞아야 수신한다. body의 route/sender는 인증 증거로 쓰지 않는다. `(peer,route,update_id)`와 본문 hash를 SQLite에 저장한 뒤 즉시 접수 응답을 반환한다. 같은 ID/다른 본문은 거부하며 raw_message는 실행하지 않는다. controller callback의 별도 승인이 없는 변경 명령은 거부한다.

송신은 `/sendMessage|/notify`에 `route/chat_id/text/parse_mode=""/escape=true`, 문서는 secret_scan 통과 후 `/sendDocument`에 base64를 보낸다. `ok:true`를 확인한다. 전송 불명의 outbox 재시도는 application 소유이며 어댑터가 거래를 재실행하지 않는다. 배포 gateway 실응답·peer 인증은 미검증이다.

SchedulePlanner는 전달받은 실제 세션 개장/종료시각으로 due intent를 만든다. 고정 평일 시계를 거래소 달력으로 대체하지 않는다. 재량 pause는 risk/reconcile/time-limit 의도를 막지 않으며, 만료된 심사를 재시작 후 재생하지 않는다. 실행/중복 claim/보호 권한 검사는 application의 동일 workflow가 소유한다.

## 검증 명령

```sh
PYTHONPATH=src python -m unittest discover -s tests/contract -p test_adapters.py -v
PYTHONPATH=src python tests/fixtures/adapters_cli_probe.py --run
```

기본 contract 테스트는 주입 fixture만 사용한다. local CLI probe는 명시적 실행으로 분리했다. 실제 외부 연결 성공이나 실제 투자 성과로 보고하지 않는다.
