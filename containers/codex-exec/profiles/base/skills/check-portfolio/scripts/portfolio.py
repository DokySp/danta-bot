#!/usr/bin/env python3
"""Read configured portfolio symbols and direct KIS holding symbols."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"


def find_repo_root(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def memory_root(repo_root: Path | None) -> Path:
    configured = os.environ.get("DAILY_TRADING_MEMORY_DIR")
    if configured:
        return Path(configured).expanduser()
    if repo_root is not None:
        return repo_root / "memory"
    return Path.cwd() / "memory"


def first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def configured_portfolio_path(repo_root: Path | None) -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("PORTFOLIO_FILE"):
        candidates.append(Path(os.environ["PORTFOLIO_FILE"]).expanduser())
    candidates.extend(
        [
            Path("/app/config/portfolio.txt"),
            Path("/workspace/containers/codex-exec/profiles/base/config/portfolio.txt"),
            Path("containers/codex-exec/profiles/base/config/portfolio.txt"),
        ]
    )
    if repo_root is not None:
        candidates.append(repo_root / "containers/codex-exec/profiles/base/config/portfolio.txt")
    return first_existing(candidates)


def assistant_recommendation_path(repo_root: Path | None) -> Path | None:
    candidates: list[Path] = []
    if os.environ.get("ASSISTANT_PORTFOLIO_CACHE_FILE"):
        candidates.append(Path(os.environ["ASSISTANT_PORTFOLIO_CACHE_FILE"]).expanduser())
    candidates.append(memory_root(repo_root) / "check-portfolio" / "assistant-recommendations.txt")
    candidates.append(Path.cwd() / "memory" / "check-portfolio" / "assistant-recommendations.txt")
    return first_existing(candidates)


def dedupe(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            output.append(symbol)
    return output


def symbols_from_text(text: str) -> list[str]:
    symbols: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for entry in line.split(","):
            parts = entry.split()
            if parts:
                symbols.append(parts[0])
    return dedupe(symbols)


def symbols_from_file(path: Path | None) -> list[str]:
    if path is None:
        return []
    return symbols_from_text(path.read_text(encoding="utf-8"))


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


def request_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    params: dict[str, str] | None = None,
    timeout: int = 20,
) -> tuple[dict[str, Any], dict[str, str]]:
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


def retry_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    params: dict[str, str] | None = None,
    retries: int = 3,
) -> tuple[dict[str, Any], dict[str, str]]:
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


def kis_token_module_candidates() -> list[Path]:
    configured = os.environ.get("KIS_TOKEN_HELPER_PATH", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path("/app/skills/kis-token/scripts/kis_token.py"),
            Path("/codex-home/skills/kis-token/scripts/kis_token.py"),
            Path("/workspace/containers/codex-exec/shared-skills/kis-token/scripts/kis_token.py"),
        ]
    )
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidates.append(parent / "kis-token" / "scripts" / "kis_token.py")
        candidates.append(parent / "shared-skills" / "kis-token" / "scripts" / "kis_token.py")
    return candidates


def load_kis_token_module() -> Any:
    for path in kis_token_module_candidates():
        if path.exists():
            spec = importlib.util.spec_from_file_location("codex_kis_token", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    raise RuntimeError("shared kis-token helper not found")


def fetch_token(app_key: str, app_secret: str, env_dv: str, retries: int) -> str:
    return load_kis_token_module().get_token(app_key, app_secret, env_dv=env_dv, retries=retries).token


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def normalize_trading_env(raw: str) -> str:
    raw = raw.strip().lower()
    if raw in {"paper", "demo", "mock"}:
        return "demo"
    if raw in {"acct", "real"}:
        return "real"
    raise SystemExit(f"unsupported trading env: {raw}")


def trading_env() -> str:
    raw = os.environ.get("CHECK_PORTFOLIO_TRADING_ENV") or os.environ.get("CODEX_MCP_TRADING_ENV") or "acct"
    return normalize_trading_env(raw)


def account_parts(env_dv: str) -> tuple[str, str]:
    if env_dv == "demo":
        account = require_env("KIS_PAPER_STOCK")
    else:
        account = require_env("KIS_ACCT_STOCK")
    product = os.environ.get("KIS_PROD_TYPE", "").strip().strip('"') or "01"
    compact = re.sub(r"[^0-9]", "", account)
    if len(compact) >= 10:
        return compact[:-2], compact[-2:]
    if len(compact) == 8:
        return compact, product
    raise SystemExit("KIS stock account must be 8 digits, or account+product code digits")


def kis_credentials(env_dv: str) -> tuple[str, str]:
    if env_dv == "demo":
        return require_env("KIS_PAPER_APP_KEY"), require_env("KIS_PAPER_APP_SECRET")
    return require_env("KIS_APP_KEY"), require_env("KIS_APP_SECRET")


def balance_tr_id(env_dv: str) -> str:
    return "VTTC8434R" if env_dv == "demo" else "TTTC8434R"


def balance_params(cano: str, product_code: str, ctx_fk100: str = "", ctx_nk100: str = "") -> dict[str, str]:
    return {
        "CANO": cano,
        "ACNT_PRDT_CD": product_code,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": ctx_fk100,
        "CTX_AREA_NK100": ctx_nk100,
    }


def normalize_holding_symbol(row: dict[str, Any]) -> str:
    for key in ("pdno", "PDNO", "prdt_code", "shtn_pdno", "item_code"):
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def positive_quantity(row: dict[str, Any]) -> bool:
    for key in ("hldg_qty", "ord_psbl_qty", "evlu_amt"):
        raw = str(row.get(key, "")).replace(",", "").strip()
        if raw in {"", "-"}:
            continue
        try:
            if float(raw) > 0:
                return True
        except ValueError:
            continue
    return False


def output_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("output1")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if isinstance(rows, dict):
        return [rows]
    return []


def output_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("output2")
    if isinstance(summary, list) and summary and isinstance(summary[0], dict):
        return summary[0]
    if isinstance(summary, dict):
        return summary
    return {}


def continuation_context(payload: dict[str, Any]) -> tuple[str, str]:
    ctx_fk100 = str(payload.get("ctx_area_fk100") or payload.get("CTX_AREA_FK100") or "").strip()
    ctx_nk100 = str(payload.get("ctx_area_nk100") or payload.get("CTX_AREA_NK100") or "").strip()
    if ctx_fk100 or ctx_nk100:
        return ctx_fk100, ctx_nk100
    summary = output_summary(payload)
    return (
        str(summary.get("ctx_area_fk100") or summary.get("CTX_AREA_FK100") or "").strip(),
        str(summary.get("ctx_area_nk100") or summary.get("CTX_AREA_NK100") or "").strip(),
    )


def fetch_holding_symbols(retries: int) -> list[str]:
    env_dv = trading_env()
    app_key, app_secret = kis_credentials(env_dv)
    cano, product_code = account_parts(env_dv)
    token = fetch_token(app_key, app_secret, env_dv, retries)
    base_headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": balance_tr_id(env_dv),
        "custtype": "P",
    }

    symbols: list[str] = []
    tr_cont = ""
    ctx_fk100 = ""
    ctx_nk100 = ""
    for _page in range(20):
        headers = dict(base_headers)
        if tr_cont:
            headers["tr_cont"] = tr_cont
        body, response_headers = retry_json(
            "GET",
            BALANCE_PATH,
            headers=headers,
            params=balance_params(cano, product_code, ctx_fk100, ctx_nk100),
            retries=retries,
        )
        rt_cd = str(body.get("rt_cd", "0"))
        if rt_cd not in {"0", ""}:
            message = str(body.get("msg1") or body.get("msg_cd") or "KIS balance request failed")
            raise RuntimeError(message)
        for row in output_rows(body):
            symbol = normalize_holding_symbol(row)
            if symbol and positive_quantity(row):
                symbols.append(symbol)
        ctx_fk100, ctx_nk100 = continuation_context(body)
        next_tr_cont = response_headers.get("tr_cont", "").strip()
        if next_tr_cont not in {"F", "M"}:
            break
        tr_cont = "N"
        time.sleep(0.2)
    return dedupe(symbols)


def compose_payload(recommanded: list[str], specified: list[str], raw_holding: list[str]) -> dict[str, list[str]]:
    recommanded = dedupe(recommanded)
    specified = dedupe(specified)
    holding = dedupe(raw_holding)
    universe = dedupe(recommanded + specified + holding)
    return {
        "recommanded": recommanded,
        "specified": specified,
        "holding": holding,
        "universe": universe,
    }


def build_payload(*, include_holdings: bool, retries: int) -> dict[str, list[str]]:
    repo_root = find_repo_root()
    recommanded = symbols_from_file(assistant_recommendation_path(repo_root))
    specified = symbols_from_file(configured_portfolio_path(repo_root))
    raw_holding = fetch_holding_symbols(retries) if include_holdings else []
    return compose_payload(recommanded, specified, raw_holding)


def command_read(args: argparse.Namespace) -> int:
    payload = build_payload(include_holdings=not args.skip_holdings, retries=args.retries)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_self_test(_args: argparse.Namespace) -> int:
    assert symbols_from_text("005930 삼성전자, 0183J0 TIGER # 035420\nKR70183J0002") == ["005930", "0183J0", "KR70183J0002"]
    assert dedupe(["005930", "000660", "005930"]) == ["005930", "000660"]
    sample = {"output1": [{"pdno": "0183J0", "hldg_qty": "10"}, {"pdno": "000660", "hldg_qty": "0"}]}
    assert [normalize_holding_symbol(row) for row in output_rows(sample)] == ["0183J0", "000660"]
    assert positive_quantity(sample["output1"][0])
    assert not positive_quantity(sample["output1"][1])
    assert continuation_context({"ctx_area_fk100": "A", "ctx_area_nk100": "B"}) == ("A", "B")
    assert continuation_context({"output2": [{"ctx_area_fk100": "C", "ctx_area_nk100": "D"}]}) == ("C", "D")
    payload = compose_payload(["111111", "005930"], ["005930", "000660"], ["005930", "035420", "035420"])
    assert payload["holding"] == ["005930", "035420"]
    assert payload["universe"] == ["111111", "005930", "000660", "035420"]
    assert normalize_trading_env("acct") == "real"
    assert normalize_trading_env("paper") == "demo"
    assert balance_tr_id("demo") == "VTTC8434R"
    assert balance_tr_id("real") == "TTTC8434R"
    print("self-test ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read portfolio symbols and direct KIS holdings.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    read_parser = subparsers.add_parser("read", help="Print portfolio JSON.")
    read_parser.add_argument("--retries", type=int, default=3, help="Retry count per KIS request.")
    read_parser.add_argument("--skip-holdings", action="store_true", help=argparse.SUPPRESS)
    read_parser.set_defaults(func=command_read)

    self_test_parser = subparsers.add_parser("self-test", help="Run local parser tests.")
    self_test_parser.set_defaults(func=command_self_test)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
