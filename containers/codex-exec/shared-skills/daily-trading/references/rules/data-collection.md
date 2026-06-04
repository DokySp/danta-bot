# 데이터 수집 규칙

## 조사 기준

- 확인 저장소: `koreainvestment/open-trading-api`
- 확인 경로: `MCP/Kis Trading MCP/configs/*.json`, `tools/*.py`, `examples_llm/domestic_stock/*/chk_*.py`, `MCP/Kis Trading MCP/Readme.md`
- MCP 최상위 도구명: `auth`, `domestic_stock`, `domestic_bond`, `domestic_futureoption`, `overseas_stock`, `overseas_futureoption`, `etfetn`, `elw`
- 실행 전 `find_api_detail`로 최신 파라미터를 다시 확인한다. 확정되지 않은 반환 필드는 실행 시점 MCP 응답으로 확인해 기록한다.
- KIS API 호출 전 `auth-token.md`의 인증 프리플라이트를 수행한다. 접근토큰이 없거나 만료되었거나 만료 임박이면 `auth(api_type="auth_token")`으로 재발급한다.

## 호출 속도 제한

- KIS MCP 호출은 기본적으로 순차 실행한다.
- 일반 시세 API도 호출 사이에 최소 1초 간격을 둔다.
- 계좌·잔고·주문·주문가능·체결조회처럼 원장을 조회하거나 변경하는 API는 병렬 호출하지 않고 호출 사이에 최소 1초, 가능하면 1.2초 이상 간격을 둔다.
- `EGW00201` 또는 `원장에서 허용 가능한 초당 거래건수를 초과` 오류가 발생하면 즉시 반복 호출하지 않는다. 최소 3초 대기 후 같은 API를 최대 2회만 재시도한다.
- 다수 종목 현재가는 가능하면 `intstock_multprice` 같은 멀티 조회 API를 우선 사용해 호출 수를 줄인다.
- `multi_tool_use.parallel`로 KIS MCP 도구를 묶지 않는다. 파일 읽기나 로컬 셸 조회처럼 KIS 서버와 무관한 작업에만 병렬화를 사용한다.

## 필수 수집 순서

1. `auth-token.md` 기준으로 요청 환경의 접근토큰 상태를 확인하고 필요 시 재발급한다.
2. 종목명이 입력된 경우 `domestic_stock(api_type="find_stock_code")`로 식별자를 확인한다. 식별자는 특정 숫자 형식으로 강제하지 않는다.
3. `domestic_stock(api_type="inquire_price")` 또는 다수 종목이면 `domestic_stock(api_type="intstock_multprice")`로 현재가와 핵심 가격 데이터를 수집한다.
4. `domestic_stock(api_type="inquire_daily_itemchartprice")`로 일봉, 주봉, 월봉을 각각 수집한다.
5. `domestic_stock(api_type="inquire_asking_price_exp_ccn")`과 `domestic_stock(api_type="inquire_ccnl")`로 호가와 체결을 수집한다.
6. `domestic_stock(api_type="inquire_investor")`, `domestic_stock(api_type="investor_trade_by_stock_daily")`, `domestic_stock(api_type="foreign_institution_total")`로 수급을 수집한다.
7. `domestic_stock(api_type="search_stock_info")`, `domestic_stock(api_type="invest_opinion")`, `domestic_stock(api_type="estimate_perform")`으로 기본정보와 추정실적을 수집한다.
8. `domestic_stock(api_type="fluctuation")`, `domestic_stock(api_type="volume_rank")`, `domestic_stock(api_type="market_cap")`, `domestic_stock(api_type="volume_power")`로 순위와 시장 상대 위치를 확인한다.
9. ETF/ETN이면 `etfetn(api_type="inquire_price")`, `etfetn(api_type="nav_comparison_trend")`를 추가한다.

## 다종목 수집 원칙

- 다종목 포트폴리오 모드에서는 모든 후보에 대해 완전한 단일 종목 리포트 수준의 데이터를 수집하려고 하지 않는다.
- 먼저 현재가, 등락률, 거래량, 52주 고저, 보유수량, 당일 체결/미체결/예약 상태를 수집해 종목 카드를 만든다.
- 주문검토대상 후보로 남은 종목에 대해서만 차트, 호가/체결, 수급, 재무/추정, ETF/NAV를 추가 수집한다.
- 호출량 제한으로 생략한 항목은 `누락 데이터`에 남기고, 다른 종목의 데이터로 보완하지 않는다.

## 실제 도구명 - 용도 - 파라미터 - 반환 필드

| 카테고리 | 실제 도구명 | 용도 | 필수 파라미터 | 반환 필드 |
|---|---|---|---|---|
| 인증 | `auth(api_type="auth_token")` | 접근토큰 발급 | `grant_type`, `env_dv` | 토큰 응답. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 인증 | `auth(api_type="auth_ws_token")` | 웹소켓 접속키 발급 | `grant_type`, `env_dv` | 웹소켓 접속키. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 종목 검색 | `domestic_stock(api_type="find_stock_code")` | 종목명으로 식별자 검색 | `stock_name` | `name`, `code`, `ex` |
| 시세/현재가 | `domestic_stock(api_type="inquire_price")` | 주식현재가 시세 | `env_dv`, `fid_cond_mrkt_div_code`, `fid_input_iscd` | `stck_prpr`, `prdy_vrss`, `prdy_ctrt`, `acml_vol`, `acml_tr_pbmn`, `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `per`, `pbr`, `eps`, `bps`, `hts_avls`, `w52_hgpr`, `w52_lwpr`, `frgn_ntby_qty`, `frgn_hldn_qty` |
| 차트 | `domestic_stock(api_type="inquire_daily_itemchartprice")` | 국내주식 기간별 시세. 일/주/월/년봉 | `env_dv`, `fid_cond_mrkt_div_code`, `fid_input_iscd`, `fid_input_date_1`, `fid_input_date_2`, `fid_period_div_code`, `fid_org_adj_prc` | `output1`: `hts_kor_isnm`, `stck_prpr`, `per`, `pbr`, `eps`, `hts_avls`; `output2`: `stck_bsop_date`, `stck_clpr`, `stck_oprc`, `stck_hgpr`, `stck_lwpr`, `acml_vol`, `acml_tr_pbmn` |
| 분봉 | `domestic_stock(api_type="inquire_time_itemchartprice")` | 당일 분봉 조회 | `env_dv`, `fid_cond_mrkt_div_code`, `fid_input_iscd`, `fid_input_hour_1`, `fid_pw_data_incu_yn`, `fid_etc_cls_code` | 분봉 시각, 현재가, 거래량 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 일별 분봉 | `domestic_stock(api_type="inquire_time_dailychartprice")` | 지정일 분봉 조회 | `fid_cond_mrkt_div_code`, `fid_input_iscd`, `fid_input_date_1`, `fid_input_hour_1`, `fid_pw_data_incu_yn`, `fid_fake_tick_incu_yn` | 분봉 시각, 가격, 거래량 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 호가 | `domestic_stock(api_type="inquire_asking_price_exp_ccn")` | 호가/예상체결 | `env_dv`, `fid_cond_mrkt_div_code`, `fid_input_iscd` | `output1`: `askp1`~`askp10`, `bidp1`~`bidp10`, `askp_rsqn1`~`askp_rsqn10`, `bidp_rsqn1`~`bidp_rsqn10`, `total_askp_rsqn`, `total_bidp_rsqn`; `output2`: `stck_prpr`, `antc_cnpr`, `antc_vol`, `vi_cls_code` |
| 체결 | `domestic_stock(api_type="inquire_ccnl")` | 현재가 체결 | `env_dv`, `fid_cond_mrkt_div_code`, `fid_input_iscd` | `stck_prpr` 등 체결 가격/수량 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 투자자별 수급 | `domestic_stock(api_type="inquire_investor")` | 주식현재가 투자자 | `env_dv`, `fid_cond_mrkt_div_code`, `fid_input_iscd` | `frgn_ntby_qty`, `orgn_ntby_qty`, `frgn_ntby_tr_pbmn`, `orgn_ntby_tr_pbmn`, `frgn_shnu_vol`, `orgn_shnu_vol`, `frgn_seln_vol`, `orgn_seln_vol` |
| 투자자별 일별 | `domestic_stock(api_type="investor_trade_by_stock_daily")` | 종목별 투자자매매동향 일별 | `fid_cond_mrkt_div_code`, `fid_input_iscd`, `fid_input_date_1`, `fid_org_adj_prc`, `fid_etc_cls_code` | `output1`, `output2`: `stck_prpr`, `acml_vol`, `frgn_ntby_qty`, `orgn_ntby_qty`, `frgn_ntby_tr_pbmn`, `orgn_ntby_tr_pbmn` 등 |
| 외국인/기관 집계 | `domestic_stock(api_type="foreign_institution_total")` | 국내기관·외국인 매매종목가집계 | `fid_cond_mrkt_div_code`, `fid_cond_scr_div_code`, `fid_input_iscd`, `fid_rank_sort_cls_code`, `fid_div_cls_code`, `fid_etc_cls_code` | `hts_kor_isnm`, `stck_prpr`, `acml_vol`, `frgn_ntby_qty`, `orgn_ntby_qty`, `frgn_ntby_tr_pbmn`, `orgn_ntby_tr_pbmn` |
| 기본정보 | `domestic_stock(api_type="search_stock_info")` | 주식기본조회 | `pdno`, `prdt_type_cd` | `thdt_clpr` 등 상품 기본정보. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 재무/추정 | `domestic_stock(api_type="estimate_perform")` | 종목추정실적 | `sht_cd` | `output1`~`output4`: 매출, 영업이익, 순이익, EPS 등 추정실적 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 투자의견 | `domestic_stock(api_type="invest_opinion")` | 국내주식 종목투자의견 | `fid_cond_mrkt_div_code`, `fid_cond_scr_div_code`, `fid_input_iscd`, `fid_input_date_1`, `fid_input_date_2` | 증권사 의견, 목표가, 투자의견 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 배당 | `domestic_stock(api_type="period_rights")` | 기간별계좌권리현황조회 | `inqr_strt_dt`, `inqr_end_dt`, `pdno`, `prdt_type_cd`, `rght_type_cd` | 계좌 기준 권리/배당 내역. `cano`, `acnt_prdt_cd`는 MCP가 자동 처리하므로 직접 제공하지 않는다. 종목 전체 배당 이력은 실행 시점 MCP 응답으로 확인 |
| 순위 | `domestic_stock(api_type="fluctuation")` | 등락률 순위 | `fid_cond_mrkt_div_code`, `fid_cond_scr_div_code`, `fid_input_iscd`, `fid_rank_sort_cls_code`, 기타 필터 | `hts_kor_isnm`, `stck_prpr`, `prdy_ctrt`, `acml_vol`, `acml_hgpr_date`, `acml_lwpr_date` |
| 순위 | `domestic_stock(api_type="volume_rank")` | 거래량순위 | `fid_cond_mrkt_div_code`, `fid_cond_scr_div_code`, `fid_input_iscd`, 기타 필터 | `hts_kor_isnm`, `stck_prpr`, `acml_vol`, `acml_tr_pbmn` |
| 순위 | `domestic_stock(api_type="market_cap")` | 시가총액 상위 | `fid_cond_mrkt_div_code`, `fid_cond_scr_div_code`, `fid_input_iscd`, 기타 필터 | `hts_kor_isnm`, `stck_prpr`, `acml_vol`, 시가총액 계열 |
| 순위 | `domestic_stock(api_type="volume_power")` | 체결강도 상위 | `fid_cond_mrkt_div_code`, `fid_cond_scr_div_code`, `fid_input_iscd`, 기타 필터 | 체결강도, 현재가, 거래량 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 업종/테마 | `domestic_stock(api_type="inquire_index_price")` | 국내업종 현재지수 | `fid_cond_mrkt_div_code`, `fid_input_iscd` | 업종지수 현재가, 등락률 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| ETF/ETN | `etfetn(api_type="inquire_price")` | ETF/ETN 현재가 | `fid_cond_mrkt_div_code`, `fid_input_iscd` | `stck_prpr`, `prdy_ctrt`, `acml_vol`, `nav`, `trc_errt`, `dprt`, `etf_ntas_ttam`, `etf_cnfg_issu_cnt`, `etf_dvdn_cycl`, `lp_hldn_rate` |
| ETF/ETN | `etfetn(api_type="nav_comparison_trend")` | NAV 비교추이 | `fid_cond_mrkt_div_code`, `fid_input_iscd` | `output1`, `output2`: `stck_prpr`, `prdy_ctrt`, `acml_vol`, `acml_tr_pbmn`, `nav`, `nav_prdy_ctrt`, `prdy_clpr_nav`, `oprc_nav`, `hprc_nav`, `lprc_nav` |
| ELW | `elw(api_type="volume_rank")` | ELW 거래량순위 | `fid_cond_mrkt_div_code`, `fid_cond_scr_div_code`, `fid_unas_input_iscd`, `fid_input_iscd`, 기타 필터 | ELW 순위, 가격, 거래량 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |
| 채권 참고 | `domestic_bond(api_type="inquire_price")` | 장내채권현재가 시세 | `fid_cond_mrkt_div_code`, `fid_input_iscd` | 채권 현재가/수익률 계열. 상세 필드는 실행 시점 MCP 응답으로 확인 |

## 본 시스템에서의 용도

| 데이터 | 사용 역할 |
|---|---|
| 현재가, 등락률, 거래량, 52주 고저 | 모든 분석가, 배심원, 단기/중기 판사 |
| 일/주/월봉 | 전략 신호, 모멘텀/차티스트/퀀트 배심원, 단기/중기 판사 |
| 호가, 체결 | 단기 판사, 골드만 분석가, 모멘텀 배심원 |
| PER, PBR, EPS, BPS, 시가총액 | 가치투자자, 피델리티, 뱅가드, 장기 판사 |
| 외국인/기관/개인 수급 | 매크로탑다운, JP모건, 단기/중기 판사 |
| ETF NAV, 괴리율, 추적오차 | 블랙록, 스테이트스트리트, ETF 대상 분석 |
| 투자의견/추정실적 | 피델리티, 모건스탠리, 장기 판사 |
| 순위/업종/테마 | 골드만, 모건스탠리, 매크로탑다운 |
