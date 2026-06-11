#!/usr/bin/env python3
"""Collect and retrieve KIS financial YAML caches."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import yaml


KST = ZoneInfo("Asia/Seoul")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_PATH = "/oauth2/tokenP"
MISSING_CACHE_MESSAGE = "해당 날짜 재무 캐시가 아직 생성되지 않았습니다."

ENDPOINTS = {
    "search_stock_info": {
        "path": "/uapi/domestic-stock/v1/quotations/search-stock-info",
        "tr_id": "CTPF1002R",
    },
    "estimate_perform": {
        "path": "/uapi/domestic-stock/v1/quotations/estimate-perform",
        "tr_id": "HHKST668300C0",
    },
    "invest_opinion": {
        "path": "/uapi/domestic-stock/v1/quotations/invest-opinion",
        "tr_id": "FHKST663300C0",
    },
    "inquire_price": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-price",
        "tr_id": "FHKST01010100",
    },
    "inquire_price_2": {
        "path": "/uapi/domestic-stock/v1/quotations/inquire-price-2",
        "tr_id": "FHPST01010000",
    },
    "etf_inquire_price": {
        "path": "/uapi/etfetn/v1/quotations/inquire-price",
        "tr_id": "FHPST02400000",
    },
    "etf_nav_comparison_trend": {
        "path": "/uapi/etfetn/v1/quotations/nav-comparison-trend",
        "tr_id": "FHPST02440000",
    },
}

API_DISPLAY_NAMES = {
    "search_stock_info": "주식기본조회",
    "estimate_perform": "국내주식 종목추정실적",
    "invest_opinion": "국내주식 종목투자의견",
    "inquire_price": "주식현재가 시세",
    "inquire_price_2": "주식현재가 시세2",
    "etf_inquire_price": "ETF/ETN 현재가",
    "etf_nav_comparison_trend": "NAV 비교추이(종목)",
}

OUTPUT_DISPLAY_NAMES = {
    "output": "응답",
    "output1": "응답 1",
    "output2": "응답 2",
    "output3": "응답 3",
    "output4": "응답 4",
}

API_OUTPUT_DISPLAY_NAMES = {
    ("estimate_perform", "output1"): "종목 및 최신 투자의견 요약",
    ("etf_nav_comparison_trend", "output1"): "NAV 비교 요약",
    ("etf_nav_comparison_trend", "output2"): "NAV 비교 추이",
}

EXCLUDED_API_OUTPUTS = {
    ("estimate_perform", "output2"),
    ("estimate_perform", "output3"),
    ("estimate_perform", "output4"),
}

FIELD_LABELS = {
    "acml_tr_pbmn": "누적 거래대금",
    "acml_vol": "누적 거래량",
    "admn_item_yn": "관리종목 여부",
    "aspr_unit": "호가 단위",
    "bfdy_clpr": "전일 종가",
    "bps": "주당순자산(BPS)",
    "bstp_cls_code": "업종 분류 코드",
    "bstp_kor_isnm": "업종명",
    "capital": "자본금",
    "clpr_chng_dt": "종가 변경일",
    "clpr_rang_cont_yn": "종가 범위연장 여부",
    "cpfn": "자본금",
    "cpfn_cnnm": "자본금 단위명",
    "cpta": "자본금",
    "cptt_trad_tr_psbl_yn": "경쟁매매 거래 가능 여부",
    "crdt_able_yn": "신용 가능 여부",
    "crdt_rate": "신용 비율",
    "data1": "자료 1",
    "data2": "자료 2",
    "data3": "자료 3",
    "data4": "자료 4",
    "data5": "자료 5",
    "divi_app_cls_code": "배당 적용 구분 코드",
    "dpsi_aptm_erlm_yn": "예탁 지정 등록 여부",
    "dmrs_val": "매도 잔량",
    "dmsp_val": "매수 잔량",
    "dprt": "괴리율",
    "dryy_hgpr_date": "연중 최고가 일자",
    "dryy_hgpr_vrss_prpr_rate": "연중 최고가 대비 현재가율",
    "dryy_lwpr_date": "연중 최저가 일자",
    "dryy_lwpr_vrss_prpr_rate": "연중 최저가 대비 현재가율",
    "d250_hgpr": "250일 최고가",
    "d250_hgpr_date": "250일 최고가 일자",
    "d250_hgpr_vrss_prpr_rate": "250일 최고가 대비 현재가율",
    "d250_lwpr": "250일 최저가",
    "d250_lwpr_date": "250일 최저가 일자",
    "d250_lwpr_vrss_prpr_rate": "250일 최저가 대비 현재가율",
    "dt": "기준 기간",
    "elw_pblc_yn": "ELW 발행 여부",
    "elec_scty_yn": "전자증권 여부",
    "eps": "주당순이익(EPS)",
    "estdate": "추정일자",
    "etf_chas_erng_rt_dbnb": "ETF 추적수익률 차이",
    "etf_cu_qty": "ETF CU 수량",
    "etf_dvsn_cd": "ETF 구분 코드",
    "etf_etn_ivst_heed_item_yn": "ETF/ETN 투자유의 여부",
    "etf_txtn_type_cd": "ETF 과세 유형 코드",
    "excg_dvsn_cd": "거래소 구분 코드",
    "fcam_cnnm": "액면가 단위명",
    "forn_item_lmtrt": "외국인 종목 한도율",
    "frbd_mket_lstg_dt": "외국인보드 시장 상장일자",
    "frgn_hldn_qty": "외국인 보유 수량",
    "frgn_ntby_qty": "외국인 순매수 수량",
    "frnr_psnl_lmt_rt": "외국인 개인 한도율",
    "grmn_rate_cls_code": "증거금률 구분 코드",
    "hgpr_vrss_prpr": "고가 대비 현재가",
    "hgpr_vrss_prpr_sign": "고가 대비 현재가 부호",
    "hts_avls": "시가총액",
    "hts_deal_qty_unit_val": "HTS 거래수량 단위",
    "hts_frgn_ehrt": "외국인 소진율",
    "hts_goal_prc": "목표가",
    "insn_pbnt_yn": "불성실공시 여부",
    "invt_caful_yn": "투자주의 여부",
    "invt_opnn": "투자의견",
    "invt_opnn_cls_code": "투자의견 구분 코드",
    "iscd_stat_cls_code": "종목 상태 구분 코드",
    "idx_bztp_lcls_cd": "지수업종 대분류 코드",
    "idx_bztp_lcls_cd_name": "지수업종 대분류명",
    "idx_bztp_mcls_cd": "지수업종 중분류 코드",
    "idx_bztp_mcls_cd_name": "지수업종 중분류명",
    "idx_bztp_scls_cd": "지수업종 소분류 코드",
    "idx_bztp_scls_cd_name": "지수업종 소분류명",
    "issu_istt_cd": "발행기관 코드",
    "issu_pric": "발행가",
    "item_kor_nm": "종목명",
    "ivst_prdt_type_cd": "투자상품 유형 코드",
    "ivst_prdt_type_cd_name": "투자상품 유형명",
    "kospi200_item_yn": "KOSPI200 편입 여부",
    "last_ssts_cntg_qty": "최근 공매도 체결 수량",
    "low_current_yn": "저유동성 여부",
    "lstg_cptl_amt": "상장 자본금",
    "lstg_stqt": "상장 주식 수",
    "lstn_stcn": "상장 주식 수",
    "lwpr_vrss_prpr": "저가 대비 현재가",
    "lwpr_vrss_prpr_sign": "저가 대비 현재가 부호",
    "mang_issu_cls_code": "관리종목 구분 코드",
    "mang_issu_yn": "관리종목 여부",
    "marg_rate": "증거금률",
    "mbcr_name": "증권사명",
    "mket_id_cd": "시장 ID 코드",
    "mrkt_warn_cls_code": "시장경고 구분 코드",
    "name1": "담당자명",
    "nday_dprt": "목표가 괴리율",
    "new_lstn_cls_name": "신규상장 구분명",
    "nwst_odst_dvsn_cd": "신주/구주 구분 코드",
    "nxt_tr_stop_yn": "NXT 거래정지 여부",
    "ocr_no": "OCR 번호",
    "oilf_fund_yn": "유전펀드 여부",
    "oprc_rang_cont_yn": "시가 범위연장 여부",
    "oprc_vrss_prpr": "시가 대비 현재가",
    "oprc_vrss_prpr_sign": "시가 대비 현재가 부호",
    "ovtm_vi_cls_code": "시간외 VI 구분 코드",
    "papr": "액면가",
    "pbr": "주가순자산비율(PBR)",
    "pdno": "상품번호",
    "per": "주가수익비율(PER)",
    "pgtr_ntby_qty": "프로그램 순매수 수량",
    "prdt_abrv_name": "상품 약어명",
    "prdt_clsf_cd": "상품 분류 코드",
    "prdt_clsf_name": "상품 분류명",
    "prdt_eng_abrv_name": "상품 영문 약어명",
    "prdt_eng_name": "상품 영문명",
    "prdt_eng_name120": "상품 영문명(120)",
    "prdt_name": "상품명",
    "prdt_name120": "상품명(120)",
    "prdt_type_cd": "상품 유형 코드",
    "prdy_clpr_vrss_hgpr_rate": "전일종가 대비 고가율",
    "prdy_clpr_vrss_lwpr_rate": "전일종가 대비 저가율",
    "prdy_clpr_vrss_oprc_rate": "전일종가 대비 시가율",
    "prdy_ctrt": "전일 대비율",
    "prdy_vol": "전일 거래량",
    "prdy_vrss": "전일 대비",
    "prdy_vrss_sign": "전일 대비 부호",
    "prdy_vrss_vol_rate": "전일 대비 거래량 비율",
    "pvt_frst_dmrs_prc": "1차 매도호가",
    "pvt_frst_dmsp_prc": "1차 매수호가",
    "pvt_pont_val": "피벗 기준값",
    "pvt_scnd_dmrs_prc": "2차 매도호가",
    "pvt_scnd_dmsp_prc": "2차 매수호가",
    "rcmd_name": "추천의견",
    "rgbf_invt_opnn": "직전 투자의견",
    "rgbf_invt_opnn_cls_code": "직전 투자의견 구분 코드",
    "rprs_mrkt_kor_name": "대표 시장명",
    "rstc_wdth_prc": "가격 제한폭",
    "sbst_pric": "대용가격",
    "scty_grp_id_cd": "증권 그룹 ID 코드",
    "scts_mket_lstg_dt": "증권시장 상장일자",
    "setl_mmdd": "결산 월일",
    "sht_cd": "단축 종목코드",
    "shtn_pdno": "단축 상품번호",
    "short_over_cls_code": "공매도 과열 구분 코드",
    "short_over_yn": "공매도 과열 여부",
    "sltr_yn": "정리매매 여부",
    "ssts_hot_yn": "공매도 과열 지정 여부",
    "ssts_yn": "공매도 가능 여부",
    "stac_month": "결산월",
    "stange_runup_yn": "이상급등 여부",
    "std_idst_clsf_cd": "표준산업분류 코드",
    "std_idst_clsf_cd_name": "표준산업분류명",
    "std_pdno": "표준 상품번호",
    "stft_esdg": "첫 목표가 대비 괴리금액",
    "stln_int_rt_dvsn_cd": "대주 이자율 구분 코드",
    "stck_bsop_date": "주식 영업일자",
    "stck_dryy_hgpr": "연중 최고가",
    "stck_dryy_lwpr": "연중 최저가",
    "stck_fcam": "주식 액면가",
    "stck_hgpr": "고가",
    "stck_kind_cd": "주식 종류 코드",
    "stck_llam": "하한가",
    "stck_lwpr": "저가",
    "stck_mxpr": "상한가",
    "stck_nday_esdg": "목표가 대비 괴리금액",
    "stck_oprc": "시가",
    "stck_prdy_clpr": "전일 종가",
    "stck_prpr": "현재가",
    "stck_sdpr": "기준가",
    "stck_shrn_iscd": "주식 단축 종목코드",
    "stck_sspr": "대용가",
    "temp_stop_yn": "임시 정지 여부",
    "thco_sbst_pric": "당사 대용가격",
    "thco_sbst_pric_chng_dt": "당사 대용가격 변경일",
    "thdt_clpr": "당일 종가",
    "tr_stop_yn": "거래 정지 여부",
    "trht_yn": "거래정지 여부",
    "vi_cls_code": "VI 구분 코드",
    "vlnt_deal_cls_name": "임의매매 구분명",
    "vlnt_fin_cls_code": "임의종료 구분 코드",
    "vol_tnrt": "거래량 회전율",
    "w52_hgpr": "52주 최고가",
    "w52_hgpr_date": "52주 최고가 일자",
    "w52_hgpr_vrss_prpr_ctrt": "52주 최고가 대비 현재가율",
    "w52_lwpr": "52주 최저가",
    "w52_lwpr_date": "52주 최저가 일자",
    "w52_lwpr_vrss_prpr_ctrt": "52주 최저가 대비 현재가율",
    "wghn_avrg_stck_prc": "가중평균 주가",
    "whol_loan_rmnd_rate": "전체 융자 잔고율",
    "wrap_asst_type_cd": "랩어카운트 자산 유형 코드",
}


class QuotedString(str):
    """YAML scalar that must be emitted with quotes."""


class FinancialYamlDumper(yaml.SafeDumper):
    pass


def quoted_string_representer(dumper: yaml.Dumper, value: QuotedString) -> yaml.nodes.ScalarNode:
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style='"')


FinancialYamlDumper.add_representer(QuotedString, quoted_string_representer)


def find_repo_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def memory_root() -> Path:
    configured = os.environ.get("DAILY_TRADING_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    repo_root = find_repo_root()
    if repo_root is not None:
        return repo_root / "memory"
    return Path.cwd() / "memory"


def cache_dir() -> Path:
    configured = os.environ.get("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    return memory_root() / "collect-financial-information"


def financial_cache_path(date_hyphen: str) -> Path:
    return cache_dir() / f"financial-{date_hyphen}.yaml"


def source_fields_cache_path(date_hyphen: str) -> Path:
    return cache_dir() / f"financial-source-fields-{date_hyphen}.yaml"


def token_cache_path() -> Path:
    configured = os.environ.get("COLLECT_FINANCIAL_INFORMATION_TOKEN_CACHE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "codex" / "collect-financial-information" / "kis-token.json"


def today_kst() -> str:
    return datetime.now(KST).date().isoformat()


def normalize_date(value: str | None) -> str:
    raw = (value or today_kst()).strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise SystemExit(f"date must be YYYY-MM-DD or YYYYMMDD: {raw!r}") from exc


def api_date(date_hyphen: str) -> str:
    return date_hyphen.replace("-", "")


def default_start_date(date_hyphen: str) -> str:
    target = datetime.strptime(date_hyphen, "%Y-%m-%d").date()
    return (target - timedelta(days=3)).strftime("%Y%m%d")


def read_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def read_yaml(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.dump(display_cache(payload), handle, Dumper=FinancialYamlDumper, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def write_source_fields_yaml(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.dump(source_fields_cache(payload), handle, Dumper=FinancialYamlDumper, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def request_json(method: str, path: str, *, headers: dict[str, str], payload: Any = None, params: dict[str, str] | None = None, timeout: int = 20) -> tuple[dict[str, Any], dict[str, str]]:
    url = KIS_BASE_URL + path
    if params:
        url = url + "?" + urlencode(params)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        response_headers = {key.lower(): value for key, value in response.headers.items()}
    if not body.strip():
        return {}, response_headers
    return json.loads(body), response_headers


def retry_json(method: str, path: str, *, headers: dict[str, str], payload: Any = None, params: dict[str, str] | None = None, retries: int = 10) -> tuple[dict[str, Any], dict[str, str]]:
    delays = [1, 2, 4, 8, 16, 30, 30, 30, 30, 30]
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return request_json(method, path, headers=headers, payload=payload, params=params)
        except HTTPError as exc:
            last_error = exc
            if exc.code in {400, 401, 403, 404}:
                raise
        except (TimeoutError, URLError, OSError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(delays[min(attempt, len(delays) - 1)])
    raise RuntimeError(f"KIS request failed after retries: {last_error}")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def parse_expiry(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def cached_token() -> str | None:
    path = token_cache_path()
    if not path.exists():
        return None
    try:
        payload = read_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    token = str(payload.get("access_token", "")).strip()
    expires_at = parse_expiry(payload.get("expires_at"))
    if not token or expires_at is None:
        return None
    if datetime.now(timezone.utc) + timedelta(minutes=30) >= expires_at:
        return None
    return token


def fetch_token(app_key: str, app_secret: str, retries: int) -> str:
    cached = cached_token()
    if cached:
        return cached
    body, _headers = retry_json(
        "POST",
        TOKEN_PATH,
        headers={"content-type": "application/json; charset=utf-8"},
        payload={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
        retries=retries,
    )
    token = str(body.get("access_token", "")).strip()
    if not token:
        raise RuntimeError("KIS token response did not include access_token")
    expires_at = parse_expiry(body.get("access_token_token_expired") or body.get("expires_at"))
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=23)
    write_json(token_cache_path(), {"access_token": token, "expires_at": expires_at.isoformat()})
    return token


def response_success(body: dict[str, Any]) -> bool:
    rt_cd = str(body.get("rt_cd", "0"))
    return rt_cd in {"0", ""}


def output_value(body: dict[str, Any], key: str) -> Any:
    value = body.get(key)
    if value is None:
        return []
    return value


def normalize_output(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        rows = [value]
    elif isinstance(value, list):
        rows = [item for item in value if isinstance(item, dict)]
    else:
        rows = []
    return [{str(key): str(val) for key, val in row.items() if val not in (None, "")} for row in rows]


def call_kis_endpoint(endpoint_name: str, params: dict[str, str], app_key: str, app_secret: str, token: str, retries: int, max_pages: int) -> tuple[dict[str, Any], list[str]]:
    endpoint = ENDPOINTS[endpoint_name]
    base_headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": endpoint["tr_id"],
        "custtype": "P",
    }
    errors: list[str] = []
    outputs: dict[str, list[dict[str, str]]] = {}
    tr_cont = ""
    for page in range(max_pages):
        headers = dict(base_headers)
        if tr_cont:
            headers["tr_cont"] = tr_cont
        try:
            body, response_headers = retry_json(
                "GET",
                endpoint["path"],
                headers=headers,
                params=params,
                retries=retries,
            )
        except Exception as exc:  # noqa: BLE001 - non-sensitive collection error
            errors.append(str(exc))
            break
        if not response_success(body):
            msg = body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "api_failure"
            errors.append(str(msg))
            break
        for key in ("output", "output1", "output2", "output3", "output4"):
            rows = normalize_output(output_value(body, key))
            if rows:
                outputs.setdefault(key, []).extend(rows)
        next_cont = response_headers.get("tr_cont", "")
        if next_cont != "M":
            break
        tr_cont = "N"
        if page + 1 >= max_pages:
            errors.append("max_pages_reached")
    return outputs, errors


def parse_symbol(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        raise SystemExit("--symbol must not be empty")
    if ":" in raw:
        symbol_id, symbol_name = raw.split(":", 1)
    elif "," in raw:
        symbol_id, symbol_name = raw.split(",", 1)
    else:
        symbol_id, symbol_name = raw, raw
    symbol_id = symbol_id.strip()
    symbol_name = symbol_name.strip() or symbol_id
    if not symbol_id:
        raise SystemExit(f"invalid symbol: {value!r}")
    return normalize_symbol_key(symbol_id), symbol_name


def parse_symbols_list(value: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in value.replace("\n", ",").split(","):
        item = item.strip()
        if item:
            result.append(parse_symbol(item))
    return result


def load_symbols(args: argparse.Namespace, *, require: bool = True) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for item in args.symbols or []:
        result.extend(parse_symbols_list(item))
    for item in args.symbol or []:
        result.append(parse_symbol(item))
    if args.symbols_file:
        path = Path(args.symbols_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            for part in line.split(","):
                part = part.strip()
                if part:
                    result.append(parse_symbol(part))
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for symbol_id, symbol_name in result:
        if symbol_id in seen:
            continue
        seen.add(symbol_id)
        unique.append((symbol_id, symbol_name))
    if require and not unique:
        raise SystemExit("at least one --symbols, --symbol, or --symbols-file entry is required")
    return unique


def normalize_symbol_key(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and digits == text:
        return digits.zfill(6)
    return text


def field_name(source_field: str) -> str:
    return FIELD_LABELS.get(source_field, "미해석 필드")


def estimate_periods(raw_payload: dict[str, Any]) -> dict[str, str]:
    rows = normalize_output(raw_payload.get("output4"))
    periods: dict[str, str] = {}
    for index, row in enumerate(rows, start=1):
        period = row.get("dt")
        if period not in (None, ""):
            periods[f"data{index}"] = f"{period} 기준"
    return periods


def contextual_field_name(api_name: str, output_key: str, source_field: str, periods: dict[str, str]) -> str:
    if api_name == "estimate_perform" and output_key in {"output2", "output3"}:
        period = periods.get(source_field)
        if period:
            return period
    return field_name(source_field)


def field_payload(source_field: Any, value: Any, *, name: str | None = None) -> tuple[str, str, str]:
    source = str(source_field)
    return name or field_name(source), str(value), source


def canonical_field(raw_field: Any) -> tuple[str, str, str] | None:
    if not isinstance(raw_field, dict):
        return None
    source_field = raw_field.get("source_field")
    value = raw_field.get("value")
    if source_field in (None, "") or value in (None, ""):
        return None
    name = raw_field.get("name") or field_name(str(source_field))
    return str(name), str(value), str(source_field)


def unique_field_name(name: str, source_field: str, fields: dict[str, str], source_fields: dict[str, str]) -> str:
    existing_source = source_fields.get(name)
    if name not in fields or existing_source == source_field:
        return name
    candidate = f"{name} ({source_field})"
    suffix = 2
    while candidate in fields and source_fields.get(candidate) != source_field:
        candidate = f"{name} ({source_field} {suffix})"
        suffix += 1
    return candidate


def row_from_entries(entries: list[tuple[str, str, str]]) -> dict[str, dict[str, str]]:
    fields: dict[str, str] = {}
    source_fields: dict[str, str] = {}
    for name, value, source_field in entries:
        unique_name = unique_field_name(name, source_field, fields, source_fields)
        fields[unique_name] = value
        source_fields[unique_name] = source_field
    return {"fields": fields, "source_fields": source_fields}


def humanized_row(api_name: str, output_key: str, row: dict[str, str], periods: dict[str, str]) -> dict[str, dict[str, str]]:
    entries = [
        field_payload(key, value, name=contextual_field_name(api_name, output_key, key, periods))
        for key, value in row.items()
        if value not in (None, "")
    ]
    return row_from_entries(entries)


def output_name(api_name: str, output_key: str) -> str:
    return API_OUTPUT_DISPLAY_NAMES.get((api_name, output_key), OUTPUT_DISPLAY_NAMES.get(output_key, output_key))


def canonical_output(api_name: str, output_key: str, rows: Any, periods: dict[str, str]) -> dict[str, Any] | None:
    normalized_rows = normalize_output(rows)
    if not normalized_rows:
        return None
    return {
        "output_name": output_name(api_name, output_key),
        "source_output": output_key,
        "rows": [humanized_row(api_name, output_key, row, periods) for row in normalized_rows],
    }


def canonical_existing_output(raw_output: Any) -> dict[str, Any] | None:
    if not isinstance(raw_output, dict):
        return None
    output_name_value = raw_output.get("output_name")
    source_output = raw_output.get("source_output")
    rows = raw_output.get("rows")
    if output_name_value in (None, "") or source_output in (None, "") or not isinstance(rows, list):
        return None
    canonical_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        fields = row.get("fields")
        source_fields = row.get("source_fields")
        if isinstance(fields, dict):
            canonical_fields = {str(key): str(value) for key, value in fields.items() if value not in (None, "")}
            canonical_source_fields = {}
            if isinstance(source_fields, dict):
                canonical_source_fields = {
                    str(key): str(value)
                    for key, value in source_fields.items()
                    if key in canonical_fields and value not in (None, "")
                }
            canonical_rows.append({"fields": canonical_fields, "source_fields": canonical_source_fields})
            continue
        if isinstance(fields, list):
            entries = [field for field in (canonical_field(item) for item in fields) if field is not None]
            canonical_rows.append(row_from_entries(entries))
    return {
        "output_name": str(output_name_value),
        "source_output": str(source_output),
        "rows": canonical_rows,
    }


def canonical_api_payload(api_name: str, raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    payload: dict[str, Any] = {"api_name": API_DISPLAY_NAMES.get(api_name, api_name)}
    outputs = []
    existing_outputs = raw_payload.get("outputs")
    if isinstance(existing_outputs, list):
        for item in existing_outputs:
            output = canonical_existing_output(item)
            if output is not None:
                outputs.append(output)
    periods = estimate_periods(raw_payload) if api_name == "estimate_perform" else {}
    for key in ("output", "output1", "output2", "output3", "output4"):
        if (api_name, key) in EXCLUDED_API_OUTPUTS:
            continue
        output = canonical_output(api_name, key, raw_payload.get(key), periods)
        if output is not None:
            outputs.append(output)
    payload["outputs"] = outputs
    errors = raw_payload.get("errors")
    if isinstance(errors, list) and errors:
        payload["errors"] = [str(item) for item in errors if str(item)]
    return payload


def canonical_symbol_payload(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    payload: dict[str, Any] = {}
    symbol_name = raw_payload.get("symbol_name")
    if symbol_name not in (None, ""):
        payload["symbol_name"] = str(symbol_name)
    apis = raw_payload.get("apis")
    canonical_apis: dict[str, dict[str, Any]] = {}
    if isinstance(apis, dict):
        for api_name in sorted(apis):
            api_payload = canonical_api_payload(str(api_name), apis[api_name])
            if api_payload:
                canonical_apis[str(api_name)] = api_payload
    payload["apis"] = canonical_apis
    return payload


def canonical_cache(raw_payload: Any) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raw_payload = {}
    raw_symbols = raw_payload.get("symbols")
    symbols: dict[QuotedString, dict[str, Any]] = {}
    if isinstance(raw_symbols, dict):
        for raw_symbol_id, raw_symbol_payload in sorted(raw_symbols.items(), key=lambda item: normalize_symbol_key(item[0])):
            symbol_id = normalize_symbol_key(raw_symbol_id)
            if not symbol_id:
                continue
            symbols[QuotedString(symbol_id)] = canonical_symbol_payload(raw_symbol_payload)
    return {
        "date": str(raw_payload.get("date") or ""),
        "source": str(raw_payload.get("source") or "kis_open_api"),
        "symbols": symbols,
    }


def strip_source_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_source_fields(item) for key, item in value.items() if key != "source_fields"}
    if isinstance(value, list):
        return [strip_source_fields(item) for item in value]
    return value


def keep_only_source_fields(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "fields":
                continue
            if key == "source_fields":
                result[key] = item
            else:
                kept = keep_only_source_fields(item)
                if kept not in ({}, []):
                    result[key] = kept
        return result
    if isinstance(value, list):
        return [item for item in (keep_only_source_fields(item) for item in value) if item not in ({}, [])]
    return value


def source_fields_cache(raw_payload: Any) -> dict[str, Any]:
    return source_reference_cache(raw_payload)


def unique_display_key(name: str, source_key: str, payload: dict[str, Any]) -> str:
    if name not in payload:
        return name
    candidate = f"{name} ({source_key})"
    suffix = 2
    while candidate in payload:
        candidate = f"{name} ({source_key} {suffix})"
        suffix += 1
    return candidate


def display_cache(raw_payload: Any) -> dict[str, Any]:
    canonical = canonical_cache(raw_payload)
    symbols: dict[QuotedString, dict[str, Any]] = {}
    for symbol_id, symbol_payload in canonical["symbols"].items():
        display_symbol_content: dict[str, Any] = {}
        apis = symbol_payload.get("apis")
        if isinstance(apis, dict):
            for source_api, api_payload in apis.items():
                if not isinstance(api_payload, dict):
                    continue
                api_display_name = str(api_payload.get("api_name") or source_api)
                api_key = unique_display_key(api_display_name, str(source_api), display_symbol_content)
                display_api: dict[str, Any] = {}
                for output in api_payload.get("outputs") or []:
                    if not isinstance(output, dict):
                        continue
                    output_display_name = str(output.get("output_name") or output.get("source_output") or "응답")
                    output_key = unique_display_key(output_display_name, str(output.get("source_output") or ""), display_api)
                    rows = []
                    for row in output.get("rows") or []:
                        if not isinstance(row, dict):
                            continue
                        fields = row.get("fields")
                        if isinstance(fields, dict) and fields:
                            rows.append(fields)
                    if rows:
                        display_api[output_key] = rows
                errors = api_payload.get("errors")
                if errors:
                    display_api["오류"] = errors
                if display_api:
                    display_symbol_content[api_key] = display_api
        symbol_name = symbol_payload.get("symbol_name")
        symbol_level = str(symbol_name) if symbol_name not in (None, "") else "종목"
        symbols[symbol_id] = {symbol_level: display_symbol_content}
    return {
        "date": canonical["date"],
        "source": canonical["source"],
        "symbols": symbols,
    }


def source_reference_cache(raw_payload: Any) -> dict[str, Any]:
    canonical = canonical_cache(raw_payload)
    symbols: dict[QuotedString, dict[str, Any]] = {}
    for symbol_id, symbol_payload in canonical["symbols"].items():
        reference_symbol_content: dict[str, Any] = {}
        apis = symbol_payload.get("apis")
        if isinstance(apis, dict):
            for source_api, api_payload in apis.items():
                if not isinstance(api_payload, dict):
                    continue
                api_display_name = str(api_payload.get("api_name") or source_api)
                api_key = unique_display_key(api_display_name, str(source_api), reference_symbol_content)
                reference_api: dict[str, Any] = {"source_api": str(source_api)}
                for output in api_payload.get("outputs") or []:
                    if not isinstance(output, dict):
                        continue
                    source_output = str(output.get("source_output") or "")
                    output_display_name = str(output.get("output_name") or source_output or "응답")
                    output_key = unique_display_key(output_display_name, source_output, reference_api)
                    rows = []
                    for row in output.get("rows") or []:
                        if not isinstance(row, dict):
                            continue
                        source_fields = row.get("source_fields")
                        if isinstance(source_fields, dict) and source_fields:
                            rows.append({"source_fields": source_fields})
                    if rows:
                        reference_api[output_key] = {
                            "source_output": source_output,
                            "rows": rows,
                        }
                errors = api_payload.get("errors")
                if errors:
                    reference_api["errors"] = errors
                if len(reference_api) > 1:
                    reference_symbol_content[api_key] = reference_api
        symbol_name = symbol_payload.get("symbol_name")
        symbol_level = str(symbol_name) if symbol_name not in (None, "") else "종목"
        symbols[symbol_id] = {symbol_level: reference_symbol_content}
    return {
        "date": canonical["date"],
        "source": canonical["source"],
        "symbols": symbols,
    }


def cache_sidecar_path(path: Path, date_hyphen: str) -> Path:
    return path.with_name(f"financial-source-fields-{date_hyphen}.yaml")


def canonical_from_display_cache(display_payload: dict[str, Any], source_payload: dict[str, Any]) -> dict[str, Any] | None:
    display_symbols = display_payload.get("symbols")
    source_symbols = source_payload.get("symbols")
    if not isinstance(display_symbols, dict) or not isinstance(source_symbols, dict):
        return None
    symbols: dict[QuotedString, dict[str, Any]] = {}
    for raw_symbol_id, display_symbol in sorted(display_symbols.items(), key=lambda item: normalize_symbol_key(item[0])):
        symbol_id = normalize_symbol_key(raw_symbol_id)
        source_symbol = source_symbols.get(raw_symbol_id) or source_symbols.get(symbol_id)
        if not symbol_id or not isinstance(display_symbol, dict) or not isinstance(source_symbol, dict):
            continue
        if len(display_symbol) != 1:
            continue
        symbol_name, display_company = next(iter(display_symbol.items()))
        source_company = source_symbol.get(symbol_name)
        if not isinstance(display_company, dict) or not isinstance(source_company, dict):
            continue
        apis: dict[str, dict[str, Any]] = {}
        for api_display_name, display_api in display_company.items():
            source_api_payload = source_company.get(api_display_name)
            if not isinstance(display_api, dict) or not isinstance(source_api_payload, dict):
                continue
            source_api = source_api_payload.get("source_api")
            if source_api in (None, ""):
                continue
            raw_api: dict[str, Any] = {}
            for output_display_name, display_rows in display_api.items():
                if output_display_name == "오류":
                    continue
                source_output_payload = source_api_payload.get(output_display_name)
                if not isinstance(display_rows, list) or not isinstance(source_output_payload, dict):
                    continue
                source_output = source_output_payload.get("source_output")
                source_rows = source_output_payload.get("rows")
                if source_output in (None, "") or not isinstance(source_rows, list):
                    continue
                raw_rows = []
                for display_row, source_row in zip(display_rows, source_rows):
                    if not isinstance(display_row, dict) or not isinstance(source_row, dict):
                        continue
                    source_fields = source_row.get("source_fields")
                    if not isinstance(source_fields, dict):
                        continue
                    raw_row = {
                        str(source_field): str(value)
                        for display_field, value in display_row.items()
                        for source_field in [source_fields.get(display_field)]
                        if source_field not in (None, "") and value not in (None, "")
                    }
                    if raw_row:
                        raw_rows.append(raw_row)
                if raw_rows:
                    raw_api[str(source_output)] = raw_rows
            errors = source_api_payload.get("errors")
            if isinstance(errors, list) and errors:
                raw_api["errors"] = [str(item) for item in errors if str(item)]
            if raw_api:
                apis[str(source_api)] = raw_api
        symbols[QuotedString(symbol_id)] = {"symbol_name": str(symbol_name), "apis": apis}
    return {
        "date": str(display_payload.get("date") or ""),
        "source": str(display_payload.get("source") or "kis_open_api"),
        "symbols": symbols,
    }


def empty_cache(date_hyphen: str) -> dict[str, Any]:
    return {
        "date": date_hyphen,
        "source": "kis_open_api",
        "symbols": {},
    }


def load_existing_cache(path: Path, date_hyphen: str) -> dict[str, Any]:
    if not path.exists():
        return empty_cache(date_hyphen)
    payload = read_yaml(path)
    if not isinstance(payload, dict):
        raise SystemExit(f"invalid cache YAML: {path}")
    if str(payload.get("date", "")) not in {"", date_hyphen}:
        raise SystemExit(f"cache date mismatch: {path}")
    payload["date"] = date_hyphen
    payload.setdefault("source", "kis_open_api")
    if not isinstance(payload.get("symbols"), dict):
        payload["symbols"] = {}
    if all(isinstance(item, dict) and "apis" in item for item in payload["symbols"].values()):
        return canonical_cache(payload)
    sidecar_path = cache_sidecar_path(path, date_hyphen)
    if sidecar_path.exists():
        source_payload = read_yaml(sidecar_path)
        if isinstance(source_payload, dict):
            restored = canonical_from_display_cache(payload, source_payload)
            if restored is not None:
                restored["date"] = date_hyphen
                return canonical_cache(restored)
    return canonical_cache(payload)


def api_params(api_name: str, symbol_id: str, args: argparse.Namespace, date_hyphen: str) -> dict[str, str]:
    end_date = api_date(date_hyphen)
    start_date = args.start_date or default_start_date(date_hyphen)
    if api_name == "search_stock_info":
        return {"PRDT_TYPE_CD": args.product_type, "PDNO": symbol_id}
    if api_name == "estimate_perform":
        return {"SHT_CD": symbol_id}
    if api_name == "invest_opinion":
        return {
            "FID_COND_MRKT_DIV_CODE": args.market,
            "FID_COND_SCR_DIV_CODE": "16633",
            "FID_INPUT_ISCD": symbol_id,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
        }
    if api_name == "inquire_price":
        return {"FID_COND_MRKT_DIV_CODE": args.market, "FID_INPUT_ISCD": symbol_id}
    if api_name == "inquire_price_2":
        return {"FID_COND_MRKT_DIV_CODE": args.market, "FID_INPUT_ISCD": symbol_id}
    if api_name == "etf_inquire_price":
        return {"FID_COND_MRKT_DIV_CODE": args.market, "FID_INPUT_ISCD": symbol_id}
    if api_name == "etf_nav_comparison_trend":
        return {"FID_COND_MRKT_DIV_CODE": args.market, "FID_INPUT_ISCD": symbol_id}
    raise ValueError(f"unsupported api: {api_name}")


def api_list(include_etf: bool) -> list[str]:
    apis = [
        "search_stock_info",
        "estimate_perform",
        "invest_opinion",
        "inquire_price",
        "inquire_price_2",
    ]
    if include_etf:
        apis.extend(["etf_inquire_price", "etf_nav_comparison_trend"])
    return apis


def collect_symbol_financial(symbol_id: str, symbol_name: str, date_hyphen: str, args: argparse.Namespace, app_key: str, app_secret: str, token: str) -> dict[str, Any]:
    apis: dict[str, Any] = {}
    for api_name in api_list(args.include_etf):
        outputs, errors = call_kis_endpoint(
            api_name,
            api_params(api_name, symbol_id, args, date_hyphen),
            app_key,
            app_secret,
            token,
            args.retries,
            args.max_pages,
        )
        api_payload = dict(outputs)
        if errors:
            api_payload["errors"] = errors
        apis[api_name] = api_payload
    return {"symbol_name": symbol_name, "apis": apis}


def symbol_payload_has_data(symbol_payload: dict[str, Any]) -> bool:
    apis = symbol_payload.get("apis")
    if not isinstance(apis, dict):
        return False
    for api_payload in apis.values():
        if not isinstance(api_payload, dict):
            continue
        for output_key in ("output", "output1", "output2", "output3", "output4"):
            if normalize_output(api_payload.get(output_key)):
                return True
    return False


def merge_cache(date_hyphen: str, path: Path, symbol_payloads: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    payload = load_existing_cache(path, date_hyphen)
    symbols = payload.setdefault("symbols", {})
    for symbol_id, symbol_payload in symbol_payloads:
        if symbol_payload_has_data(symbol_payload):
            symbols[normalize_symbol_key(symbol_id)] = canonical_symbol_payload(symbol_payload)
    return payload


def command_get(args: argparse.Namespace) -> int:
    date_hyphen = normalize_date(args.date)
    path = financial_cache_path(date_hyphen)
    if not path.exists():
        print(MISSING_CACHE_MESSAGE)
        return 0
    print(path)
    return 0


def command_collect(args: argparse.Namespace) -> int:
    date_hyphen = normalize_date(args.date)
    path = financial_cache_path(date_hyphen)
    symbols = load_symbols(args, require=False)
    if not symbols:
        print(path)
        return 0
    app_key = require_env("KIS_APP_KEY")
    app_secret = require_env("KIS_APP_SECRET")
    token = fetch_token(app_key, app_secret, args.retries)
    collected = []
    for symbol_id, symbol_name in symbols:
        collected.append(
            (
                symbol_id,
                collect_symbol_financial(symbol_id, symbol_name, date_hyphen, args, app_key, app_secret, token),
            )
        )
    output = merge_cache(date_hyphen, path, collected)
    write_yaml(path, output)
    write_source_fields_yaml(source_fields_cache_path(date_hyphen), output)
    print(path)
    return 0


def command_self_test(_args: argparse.Namespace) -> int:
    date_hyphen = normalize_date("20260610")
    assert date_hyphen == "2026-06-10"
    assert financial_cache_path(date_hyphen).name == "financial-2026-06-10.yaml"
    namespace = argparse.Namespace(
        symbols=["005930,000660"],
        symbol=None,
        symbols_file=None,
        product_type="300",
        market="J",
        start_date=None,
        include_etf=True,
    )
    assert load_symbols(namespace) == [("005930", "005930"), ("000660", "000660")]
    empty_namespace = argparse.Namespace(
        date=date_hyphen,
        symbols=None,
        symbol=None,
        symbols_file=None,
        product_type="300",
        market="J",
        start_date=None,
        include_etf=False,
        retries=0,
        max_pages=1,
    )
    assert load_symbols(empty_namespace, require=False) == []
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        assert command_collect(empty_namespace) == 0
    assert stdout.getvalue().strip().endswith("financial-2026-06-10.yaml")
    assert api_params("estimate_perform", "005930", namespace, date_hyphen) == {"SHT_CD": "005930"}
    assert api_params("invest_opinion", "005930", namespace, date_hyphen)["FID_COND_SCR_DIV_CODE"] == "16633"
    assert api_params("invest_opinion", "005930", namespace, date_hyphen)["FID_INPUT_DATE_1"] == "20260607"
    assert api_params("invest_opinion", "005930", namespace, date_hyphen)["FID_INPUT_DATE_2"] == "20260610"
    cache = merge_cache(
        date_hyphen,
        Path("/tmp/nonexistent-financial-cache.yaml"),
        [
            (
                "005930",
                {
                    "symbol_name": "삼성전자",
                    "apis": {
                        "estimate_perform": {
                            "output1": [{"stck_bsop_date": "20260610", "eps": 1234}],
                            "output2": [{"data1": 100, "data2": 200}],
                            "output4": [{"dt": "2025.12"}, {"dt": "2026.12E"}],
                        },
                        "inquire_price": {"output": {"stck_prpr": "80000"}},
                    },
                },
            )
        ],
    )
    canonical = canonical_cache(cache)
    assert list(canonical.keys()) == ["date", "source", "symbols"]
    assert list(canonical["symbols"]["005930"].keys()) == ["symbol_name", "apis"]
    assert "estimate_perform" in canonical["symbols"]["005930"]["apis"]
    estimate_api = canonical["symbols"]["005930"]["apis"]["estimate_perform"]
    assert estimate_api["api_name"] == "국내주식 종목추정실적"
    assert len(estimate_api["outputs"]) == 1
    estimate_output = estimate_api["outputs"][0]
    assert estimate_output["output_name"] == "종목 및 최신 투자의견 요약"
    estimate_fields = estimate_output["rows"][0]["fields"]
    assert estimate_fields["주식 영업일자"] == "20260610"
    assert estimate_fields["주당순이익(EPS)"] == "1234"
    estimate_source_fields = estimate_output["rows"][0]["source_fields"]
    assert estimate_source_fields["주식 영업일자"] == "stck_bsop_date"
    assert estimate_source_fields["주당순이익(EPS)"] == "eps"
    price_api = canonical["symbols"]["005930"]["apis"]["inquire_price"]
    price_fields = price_api["outputs"][0]["rows"][0]["fields"]
    assert price_fields["현재가"] == "80000"
    assert price_api["outputs"][0]["rows"][0]["source_fields"]["현재가"] == "stck_prpr"
    temp = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-financial-information-self-test.yaml"
    write_yaml(temp, cache)
    written = temp.read_text(encoding="utf-8")
    assert '  "005930":' in written
    assert "국내주식 종목추정실적:" in written
    assert "종목 및 최신 투자의견 요약:" in written
    assert "추정 실적 표 1:" not in written
    assert "추정 실적 표 2:" not in written
    assert "추정 실적 기준 기간:" not in written
    assert "현재가: '80000'" in written
    assert "estimate_perform:" not in written
    assert "api_name:" not in written
    assert "output_name:" not in written
    assert "source_output:" not in written
    assert "source_fields:" not in written
    source_temp = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-financial-information-source-fields-self-test.yaml"
    write_source_fields_yaml(source_temp, cache)
    source_written = source_temp.read_text(encoding="utf-8")
    assert "source_api: estimate_perform" in source_written
    assert "source_output: output" in source_written
    assert "source_fields:" in source_written
    assert "현재가: stck_prpr" in source_written
    assert "추정 실적 표 1:" not in source_written
    assert "추정 실적 표 2:" not in source_written
    assert "추정 실적 기준 기간:" not in source_written
    assert "현재가: '80000'" not in source_written
    existing_path = Path(os.environ.get("TMPDIR", "/tmp")) / "collect-financial-information-existing.yaml"
    existing_source_path = cache_sidecar_path(existing_path, date_hyphen)
    write_yaml(existing_path, cache)
    write_source_fields_yaml(existing_source_path, cache)
    failed_update = merge_cache(
        date_hyphen,
        existing_path,
        [
            (
                "005930",
                {
                    "symbol_name": "삼성전자",
                    "apis": {"inquire_price": {"errors": ["temporary_failure"]}},
                },
            )
        ],
    )
    assert failed_update["symbols"]["005930"]["apis"]["inquire_price"]["outputs"][0]["rows"][0]["fields"]["현재가"] == "80000"
    appended = merge_cache(
        date_hyphen,
        existing_path,
        [
            (
                "000660",
                {
                    "symbol_name": "SK하이닉스",
                    "apis": {"inquire_price": {"output": {"stck_prpr": "90000"}}},
                },
            )
        ],
    )
    assert sorted(appended["symbols"]) == ["000660", "005930"]
    assert appended["symbols"]["000660"]["apis"]["inquire_price"]["outputs"][0]["rows"][0]["fields"]["현재가"] == "90000"
    temp.unlink(missing_ok=True)
    source_temp.unlink(missing_ok=True)
    existing_path.unlink(missing_ok=True)
    existing_source_path.unlink(missing_ok=True)
    print("self-test ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and retrieve KIS financial YAML caches.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    get_parser = subparsers.add_parser("get", help="Return the date cache path.")
    get_parser.add_argument("--date", help="Target date in YYYY-MM-DD or YYYYMMDD. Defaults to today in Asia/Seoul.")
    get_parser.set_defaults(func=command_get)

    collect_parser = subparsers.add_parser("collect", help="Collect financial data and write the date cache.")
    collect_parser.add_argument("--date", help="Target date in YYYY-MM-DD or YYYYMMDD. Defaults to today in Asia/Seoul.")
    collect_parser.add_argument("--symbols", action="append", help="Comma-separated symbol code list. Can be repeated.")
    collect_parser.add_argument("--symbol", action="append", help="Symbol as CODE or CODE:NAME. Can be repeated.")
    collect_parser.add_argument("--symbols-file", help="Comma/newline separated symbol list.")
    collect_parser.add_argument("--market", default="J", help="KIS market code. Defaults to J.")
    collect_parser.add_argument("--product-type", default="300", help="KIS product type code. Defaults to 300.")
    collect_parser.add_argument("--start-date", help="Invest opinion start date in YYYYMMDD. Defaults to three days before --date.")
    collect_parser.add_argument("--include-etf", action="store_true", help="Also call ETF/ETN price and NAV APIs.")
    collect_parser.add_argument("--retries", type=int, default=3, help="Retry count per KIS request.")
    collect_parser.add_argument("--max-pages", type=int, default=3, help="Maximum KIS continuation pages per API.")
    collect_parser.set_defaults(func=command_collect)

    self_test_parser = subparsers.add_parser("self-test", help="Run local deterministic tests.")
    self_test_parser.set_defaults(func=command_self_test)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
