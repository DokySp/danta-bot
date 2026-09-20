"""Read-only deployment facts; no account, model, or order authority is created here."""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .adapters import AdapterError, require_http_ok
from .adapters.kis import MASTER_ORIGIN, QUOTATIONS
from .config import digest

SEOUL = ZoneInfo("Asia/Seoul")
KIS_SOURCE = "https://github.com/koreainvestment/open-trading-api/blob/main/"
HOLIDAY_SOURCE = KIS_SOURCE + "examples_llm/domestic_stock/chk_holiday/chk_holiday.py"
MASTER_SOURCE = KIS_SOURCE + "stocks_info/"
BAR_SOURCE = KIS_SOURCE + "examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py"
INDEX_SOURCE = KIS_SOURCE + "examples_llm/domestic_stock/inquire_daily_indexchartprice/inquire_daily_indexchartprice.py"
QUOTE_SOURCE = "https://apiportal.koreainvestment.com/api/apis/guide/property/714d1437-8f62-43db-a73c-cf509d3f6aa7"
DART_SOURCE = "https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS001&apiId=2019018"
TICK_SOURCE = "https://regulation.krx.co.kr/contents/RGL/03/03010100/RGL03010100T3.jsp"
HOURS_SOURCE = "https://regulation.krx.co.kr/contents/RGL/03/03020407/RGL03020407.jsp"
TICK_BANDS = [["0", "1"], ["2000", "5"], ["5000", "10"], ["20000", "50"],
              ["50000", "100"], ["200000", "500"], ["500000", "1000"]]


def normalize_master_flags(row):
    """Categorical ETP/preferred codes are not interchangeable with Y/N flags."""
    # The live master uses SPACE for ETP on ST shares; group ST is the independent share classification.
    no_etp = row.get("etp") == "0" or row.get("etp") == "" and row.get("group") == "ST"
    known = ((no_etp or row.get("etp") in {"1", "2", "3", "4", "5"})
             and row.get("preferred") in {"0", "1", "2"}
             and all(row.get(key) in {"Y", "N"} for key in ("spac", "halted", "liquidation", "managed")))
    common = row.get("group") == "ST" and no_etp and row.get("preferred") == "0" and row.get("spac") == "N"
    status = ("UNKNOWN" if not known else "HALTED" if row["halted"] == "Y" else
              "DELISTING" if row["liquidation"] == "Y" else "ADMINISTRATIVE" if row["managed"] == "Y" else "NORMAL")
    return {"kind": "common_stock" if common else "excluded_instrument", "status": status, "codes_known": known}


def read_open_days(kis, start: date, end: date):
    """Collect the complete requested daily holiday range, including closed days."""
    if start > end:
        raise AdapterError("INVALID_DATE_RANGE")
    records, pages, seen, cursor = {}, [], set(), ("", "")
    expected = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    for _ in range(kis.max_pages):
        if cursor in seen:
            raise AdapterError("HOLIDAY_REPEATED_CURSOR")
        seen.add(cursor)
        data, headers = kis._request(QUOTATIONS + "chk-holiday", "CTCA0903R", {
            "BASS_DT": start.strftime("%Y%m%d"), "CTX_AREA_FK": cursor[0], "CTX_AREA_NK": cursor[1]},
            continuation="N" if any(cursor) else "")
        rows = data.get("output")
        if not isinstance(rows, list) or not rows:
            raise AdapterError("HOLIDAY_RESPONSE_INCOMPLETE")
        pages.append(rows)
        try:
            for row in rows:
                day = date.fromisoformat(row["bass_dt"])
                opened = row["opnd_yn"]
                if opened not in {"Y", "N"} or day in records and records[day] != opened:
                    raise ValueError
                records[day] = opened
        except (ValueError, TypeError, KeyError):
            raise AdapterError("HOLIDAY_CONTRACT_MISMATCH") from None
        if all(day in records for day in expected):
            break  # A continuation beyond the requested range is not missing coverage.
        if headers.get("tr_cont") not in {"M", "F"}:
            break
        values = (data.get("ctx_area_fk", ""), data.get("ctx_area_nk", ""))
        if not all(isinstance(value, str) for value in values):
            raise AdapterError("HOLIDAY_INVALID_CURSOR")
        cursor = tuple(value.strip() for value in values)
        if not any(cursor):
            raise AdapterError("HOLIDAY_MISSING_CURSOR")
    else:
        raise AdapterError("HOLIDAY_PAGE_LIMIT")
    if any(day not in records for day in expected):
        raise AdapterError("HOLIDAY_DATE_COVERAGE_INCOMPLETE")
    return [day for day in expected if records[day] == "Y"], digest(pages)


def read_sector_map(kis):
    kis._permit("market_read")
    response = kis.transport("GET", MASTER_ORIGIN + "/common/master/idxcode.mst.zip", {}, None, 15)
    require_http_ok(response)
    try:
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            info = archive.getinfo("idxcode.mst")
            if info.file_size > 32 * 1024 * 1024:
                raise AdapterError("RESPONSE_TOO_LARGE")
            content = archive.read(info)
        sectors = {}
        for line in content.splitlines():
            # The official C layout is 1 + 4 + 40 bytes, including CP949 names.
            if len(line) != 45:
                raise ValueError
            if line[:1] not in {b"0", b"1"}:  # Only KOSPI/KOSDAQ industries; other index/product families are separate.
                continue
            code, name = line[1:5].decode("ascii"), line[5:45].decode("cp949").strip()
            if not re.fullmatch(r"\d{4}", code) or not name:
                raise ValueError
            if code in sectors and sectors[code] != name:
                raise AdapterError("SECTOR_MAPPING_CONFLICT")
            sectors[code] = name
        if not sectors:
            raise ValueError
    except (ValueError, KeyError, UnicodeError, zipfile.BadZipFile):
        raise AdapterError("SECTOR_MASTER_CONTRACT_MISMATCH") from None
    return sectors, hashlib.sha256(response.body).hexdigest()


def read_historical_days(kis, start, end):
    """Derive past sessions from fully paged, matching KOSPI/KOSDAQ histories."""
    if start > end:
        raise AdapterError("INVALID_DATE_RANGE")
    histories = []
    for ticker in ("0001", "1001"):
        rows, upper = {}, end
        for _ in range(kis.max_pages):
            data, _headers = kis._request(QUOTATIONS + "inquire-daily-indexchartprice", "FHKUP03500100", {
                "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"), "FID_INPUT_DATE_2": upper.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D"})
            page = data.get("output2")
            if not isinstance(page, list):
                raise AdapterError("INDEX_CALENDAR_CONTRACT_MISMATCH")
            if not page:
                break
            try:
                dates = [date.fromisoformat(row["stck_bsop_date"]) for row in page]
                if any(day > upper for day in dates) or len(set(dates)) != len(dates):
                    raise ValueError
                for day, row in zip(dates, page):
                    if day >= start:
                        rows[day] = row
            except (KeyError, TypeError, ValueError):
                raise AdapterError("INDEX_CALENDAR_CONTRACT_MISMATCH") from None
            if min(dates) <= start:
                break
            upper = min(dates) - timedelta(days=1)
        else:
            raise AdapterError("INDEX_CALENDAR_PAGE_LIMIT")
        if not rows:
            raise AdapterError("INDEX_CALENDAR_EMPTY")
        histories.append(rows)
    days = sorted(histories[0])
    if days != sorted(histories[1]):
        raise AdapterError("INDEX_CALENDAR_BOARDS_DIFFER")
    return days, digest([[history[day] for day in days] for history in histories])


def daily_bar_available_at(session):
    return session.daily_bar_available_at or session.closes_at


def completed_sessions(calendar, now):
    return [session for session in calendar.sessions if daily_bar_available_at(session) <= now]


def validate_bar_observations(result, expected_sessions, *, index=False):
    """Validate one observed snapshot; this does not certify historical PIT data."""
    if result.metadata.get("adjustment") != "provider_adjusted" or result.quality not in {"COMPLETE", "PARTIAL"}:
        raise AdapterError("BAR_ADJUSTMENT_UNVERIFIED")
    names = ("bstp_nmix_hgpr", "bstp_nmix_lwpr", "bstp_nmix_prpr") if index else ("stck_hgpr", "stck_lwpr", "stck_clpr")
    sessions = []
    try:
        for row in result.records:
            sessions.append(date.fromisoformat(row["stck_bsop_date"]).isoformat())
            if any(isinstance(row[name], bool) for name in (*names, "acml_tr_pbmn")):
                raise ValueError
            high, low, close = [Decimal(str(row[name])) for name in names]
            turnover = Decimal(str(row["acml_tr_pbmn"]))
            if not all(value.is_finite() for value in (high, low, close, turnover)) or not 0 < low <= close <= high or turnover < 0:
                raise ValueError
        if sessions != list(expected_sessions) or not sessions:
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise AdapterError("BAR_OBSERVATION_CONTRACT_MISMATCH") from None
    return {"source": INDEX_SOURCE if index else BAR_SOURCE, "observation_sha256": digest(result.records),
            "retrieved_at": result.retrieved_at.isoformat(), "consistent_ohlc_verified": True,
            "point_in_time_adjustment_verified": False}


def prepare_market_sources(kis, dart, *, now, start=None, end=None):
    if now.tzinfo is None or now.utcoffset() is None:
        raise AdapterError("PREPARATION_TIMEZONE_REQUIRED")
    today = now.astimezone(SEOUL).date()
    start, end = start or today - timedelta(days=400), end or today + timedelta(days=31)
    if not start < today <= end:
        raise AdapterError("INVALID_PREPARATION_DATE_RANGE")
    history, history_hash = read_historical_days(kis, start, today - timedelta(days=1))
    future, holiday_hash = read_open_days(kis, today, end)
    days = history + future
    sectors, sector_hash = read_sector_map(kis)
    corporations = dart.read_corporations()
    if corporations.quality != "COMPLETE" or not corporations.records:
        raise AdapterError("CORPORATION_MAPPING_INCOMPLETE")
    issuers, instruments = {}, {}
    for row in corporations.records:
        symbol, code = row.get("symbol"), row.get("corp_code")
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{6}", symbol)
                or not isinstance(code, str) or not re.fullmatch(r"\d{8}", code)
                or symbol in issuers or code in instruments):
            raise AdapterError("CORPORATION_MAPPING_CONFLICT")
        issuers[symbol], instruments[code] = "DART:" + code, "KRX:" + symbol
    sessions = []
    for ordinal, day in enumerate(days):
        sessions.append({"session_id": day.isoformat(), "ordinal": ordinal,
            "opens_at": datetime.combine(day, time(9), SEOUL).isoformat(),
            "closes_at": datetime.combine(day, time(15, 20), SEOUL).isoformat(),
            # ponytail: prior-day bars only; add official special-session close data for same-day refresh.
            "daily_bar_available_at": datetime.combine(day + timedelta(days=1), time(), SEOUL).isoformat(),
            "hours_verified": False, "venue": "KRX"})
    return {
        "calendar": {"source": HOLIDAY_SOURCE, "verified": True, "sessions": sessions,
            "observed_at": now.isoformat(), "observation_sha256": holiday_hash,
            "history_source": INDEX_SOURCE, "history_observation_sha256": history_hash,
            "coverage_start": start.isoformat(), "coverage_end": end.isoformat(),
            "verification_scope": "observed_open_dates_and_conservative_regular_window",
            "special_hours_verified": False, "hours_source": HOURS_SOURCE},
        "ticks": {"source": TICK_SOURCE, "verified": True, "bands": [list(row) for row in TICK_BANDS],
            "effective_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
            "verification_scope": "bundled_official_price_unit_contract"},
        "normalization": {
            "instruments": {"source": MASTER_SOURCE, "normalizer": "kis_master_field_codes_v1",
                "common_groups": ["ST"], "issuer_by_symbol": issuers, "sector_by_industry": sectors,
                "sector_observation_sha256": sector_hash, "corporation_observation_sha256": digest(corporations.records)},
            "bars": {"source": BAR_SOURCE, "index_source": INDEX_SOURCE, "price_returns_only": True,
                "stock_basis": "kis_krx_provider_adjusted_snapshot_price_only", "index_basis": "kis_krx_index_price_only",
                "consistent_ohlc_verified": False, "point_in_time_adjustment_verified": False},
            "quote": {"source": QUOTE_SOURCE, "transport": "websocket", "session_date": "BSOP_DATE",
                "observed_time": "STCK_CNTG_HOUR", "bid": "BIDP1", "ask": "ASKP1",
                "bid_quantity": "BIDP_RSQN1", "ask_quantity": "ASKP_RSQN1"}},
        "disclosures": {"source": DART_SOURCE, "start_date": start.isoformat(),
            "instrument_by_corp_code": instruments, "mapping_observed_at": corporations.retrieved_at.isoformat()},
    }
