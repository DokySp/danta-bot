import html
import importlib.util
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml

from .config import Config
from .telegram_gateway import TelegramGateway, TypingIndicator


NAVER_INDEX_URL = "https://polling.finance.naver.com/api/realtime/domestic/index/{symbol}"
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_INDEX_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-index-price"
KIS_INDEX_PRICE_TR_ID = "FHPUP02100000"
KST = timezone(timedelta(hours=9))
CONFIG_TIME_RE = re.compile(r"^\d{4}$")
KIS_INDEX_CODES = {
    "KOSPI": "0001",
    "KOSDAQ": "1001",
    "KOSPI200": "2001",
}


@dataclass(frozen=True)
class PriceTrigger:
    trigger_id: str
    case_title: str
    name: str
    symbol: str
    source: str
    up_percent: float
    down_percent: float
    enabled: bool
    send_telegram: bool
    chat_id: str | None
    route: str | None


@dataclass(frozen=True)
class TriggerConfig:
    enabled: bool
    poll_seconds: int
    active_start_time: time | None
    active_end_time: time | None
    cache_file: Path
    quote_history_file: Path
    touch_log_file: Path
    triggers: list[PriceTrigger]


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    value: float
    observed_at: str
    market_status: str | None
    session_change_percent: float | None = None


def parse_price_trigger_config(path: Path, state_dir: Path) -> TriggerConfig:
    if not path.exists():
        cache_file = state_dir / "touch-points" / "triggers.json"
        return TriggerConfig(
            False,
            60,
            None,
            None,
            cache_file,
            quote_history_path(cache_file, None),
            touch_log_path(cache_file, None),
            [],
        )

    raw_text = path.read_text()
    quoted_fields = quoted_yaml_scalar_fields(
        raw_text,
        {"active_start_time", "active_end_time"},
    )
    data = yaml.safe_load(raw_text) or {}
    if not isinstance(data, dict):
        raise ValueError("price trigger file must contain a YAML object")

    raw_triggers = data.get("touch_points", data.get("triggers", []))
    if not isinstance(raw_triggers, list):
        raise ValueError("price trigger file must contain a touch_points list")

    defaults = data.get("telegram", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("price trigger telegram must be a YAML object")

    triggers: list[PriceTrigger] = []
    for item in raw_triggers:
        if not isinstance(item, dict):
            continue
        trigger_id = str(item.get("id", "")).strip()
        symbol = str(item.get("symbol", "")).strip()
        if not trigger_id or not symbol:
            continue
        up_percent = float(item.get("up_percent", 0))
        down_percent = float(item.get("down_percent", 0))
        if up_percent <= 0:
            raise ValueError(f"{trigger_id}: up_percent must be greater than 0")
        if down_percent >= 0:
            raise ValueError(f"{trigger_id}: down_percent must be less than 0")
        chat_id = item.get("chat_id", defaults.get("chat_id"))
        route = item.get("route", defaults.get("route"))
        triggers.append(
            PriceTrigger(
                trigger_id=trigger_id,
                case_title=str(item.get("case_title") or trigger_id),
                name=str(item.get("name") or symbol),
                symbol=symbol,
                source=str(item.get("source") or "naver_domestic_index"),
                up_percent=up_percent,
                down_percent=down_percent,
                enabled=item.get("enabled", True) is not False,
                send_telegram=item.get("send_telegram", True) is not False,
                chat_id=str(chat_id) if chat_id else None,
                route=str(route) if route else None,
            )
        )

    cache_file = Path(data.get("cache_file") or state_dir / "touch-points" / "triggers.json")
    for field_name in ("active_start_time", "active_end_time"):
        if data.get(field_name) is not None and quoted_fields.get(field_name) is not True:
            raise ValueError(f"{field_name} must use quoted HHMM string format")
    active_start_time = parse_config_time(data.get("active_start_time"), "active_start_time")
    active_end_time = parse_config_time(data.get("active_end_time"), "active_end_time")
    if (active_start_time is None) != (active_end_time is None):
        raise ValueError("active_start_time and active_end_time must be configured together")
    if active_start_time is not None and active_start_time >= active_end_time:
        raise ValueError("active_start_time must be earlier than active_end_time")

    return TriggerConfig(
        enabled=data.get("enabled", True) is not False,
        poll_seconds=max(60, int(data.get("poll_seconds", 60))),
        active_start_time=active_start_time,
        active_end_time=active_end_time,
        cache_file=cache_file,
        quote_history_file=quote_history_path(cache_file, data.get("quote_history_file")),
        touch_log_file=touch_log_path(cache_file, data.get("touch_log_file")),
        triggers=triggers,
    )


class PriceTriggerWatcher:
    def __init__(self, config: Config, gateway: TelegramGateway) -> None:
        self.config = config
        self.gateway = gateway
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="price-trigger-watcher", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            wait_seconds = 60
            try:
                trigger_config = parse_price_trigger_config(
                    self.config.price_trigger_file,
                    self.config.state_dir,
                )
                wait_seconds = trigger_config.poll_seconds
                if trigger_config.enabled and is_active_time(trigger_config, datetime.now(KST)):
                    self._tick(trigger_config)
            except Exception:
                logging.exception("price trigger tick failed")
            self.stop_event.wait(wait_seconds)

    def _tick(self, trigger_config: TriggerConfig) -> None:
        cache = read_cache(trigger_config.cache_file)
        changed = False
        states = cache.setdefault("triggers", {})
        if not isinstance(states, dict):
            states = {}
            cache["triggers"] = states

        quotes: dict[tuple[str, str], Quote] = {}
        for trigger in trigger_config.triggers:
            if not trigger.enabled:
                continue
            quote_key = (trigger.source, trigger.symbol.upper())
            quote = quotes.get(quote_key)
            if quote is None:
                quote = fetch_quote(trigger, self.config)
                quotes[quote_key] = quote
            state = states.setdefault(trigger.trigger_id, {})
            if not isinstance(state, dict):
                state = {}
                states[trigger.trigger_id] = state
            changed = self._handle_quote(trigger_config, trigger, quote, state) or changed

        if changed:
            write_cache(trigger_config.cache_file, cache)
        try:
            write_quote_history(trigger_config.quote_history_file, quotes.items())
        except Exception:
            logging.exception("failed to write price trigger quote history")

    def _handle_quote(
        self,
        trigger_config: TriggerConfig,
        trigger: PriceTrigger,
        quote: Quote,
        state: dict[str, Any],
    ) -> bool:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if quote.value <= 0:
            logging.warning(
                "ignored non-positive price trigger quote id=%s value=%s observed_at=%s",
                trigger.trigger_id,
                quote.value,
                quote.observed_at,
            )
            return False

        reference = parse_float(state.get("reference_value"))
        if reference is None or reference <= 0:
            state.update(
                {
                    "reference_value": quote.value,
                    "reference_observed_at": quote.observed_at,
                    "last_checked_value": quote.value,
                    "last_checked_at": quote.observed_at,
                    "updated_at": now,
                }
            )
            logging.info(
                "initialized price trigger reference id=%s value=%s",
                trigger.trigger_id,
                quote.value,
            )
            return True

        percent = ((quote.value - reference) / reference) * 100
        if percent >= trigger.up_percent:
            self._send_touch(trigger_config, trigger, quote, reference, percent, "상승")
            update_touch_state(state, quote, reference, percent, "up")
            return True
        if percent <= trigger.down_percent:
            self._send_touch(trigger_config, trigger, quote, reference, percent, "하락")
            update_touch_state(state, quote, reference, percent, "down")
            return True
        return False

    def _send_touch(
        self,
        trigger_config: TriggerConfig,
        trigger: PriceTrigger,
        quote: Quote,
        reference: float,
        percent: float,
        direction_label: str,
    ) -> None:
        route = trigger.route
        chat_id = trigger.chat_id
        text = (
            f"<b>{html.escape(trigger.case_title)}</b>\n"
            "<b>가격 조건 터치</b>\n"
            f"아이디: <code>{html.escape(trigger.trigger_id)}</code>\n"
            f"대상: <code>{html.escape(trigger.name)}</code>\n"
            f"방향: {html.escape(direction_label)}\n"
            f"기준값: <code>{reference:,.2f}</code>\n"
            f"터치값: <code>{quote.value:,.2f}</code>\n"
            f"등락률: <code>{percent:+.2f}%</code>\n"
            f"관측시각: <code>{html.escape(quote.observed_at)}</code>"
        )
        if quote.market_status:
            text += f"\n시장상태: <code>{html.escape(quote.market_status)}</code>"
        write_touch_event(trigger_config.touch_log_file, trigger, quote, reference, percent, direction_label)
        if not trigger.send_telegram:
            logging.info("price trigger Telegram send disabled id=%s", trigger.trigger_id)
            return
        with TypingIndicator(
            self.gateway,
            chat_id,
            route,
            self.config.telegram_typing_interval_seconds,
        ):
            self.gateway.send_message(text, chat_id, route)


def fetch_quote(trigger: PriceTrigger, config: Config) -> Quote:
    if trigger.source == "kis_domestic_index":
        return fetch_kis_domestic_index(trigger.symbol, config)
    if trigger.source == "naver_domestic_index":
        return fetch_naver_domestic_index(trigger.symbol)
    raise ValueError(f"{trigger.trigger_id}: unsupported source: {trigger.source}")


def fetch_kis_domestic_index(symbol: str, config: Config) -> Quote:
    app_key, app_secret = kis_credentials()
    token = fetch_kis_token(app_key, app_secret, config)
    index_code = KIS_INDEX_CODES.get(symbol.upper(), symbol)
    body = kis_request_json(
        "GET",
        KIS_INDEX_PRICE_PATH,
        headers={
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": KIS_INDEX_PRICE_TR_ID,
            "custtype": "P",
        },
        params={
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": index_code,
        },
    )
    if not kis_response_success(body):
        message = body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "api_failure"
        raise RuntimeError(f"KIS index quote failed: {message}")

    row = first_output_row(body)
    value = first_float(
        row,
        (
            "bstp_nmix_prpr",
            "bstp_nmix_prpr_prc",
            "stck_prpr",
            "close",
        ),
    )
    if value is None:
        raise RuntimeError(f"KIS index quote did not include a numeric price for {symbol}")
    observed_at = kis_observed_at(row)
    return Quote(
        symbol=symbol.upper(),
        name=str(row.get("bstp_kor_isnm") or row.get("hts_kor_isnm") or symbol.upper()),
        value=value,
        observed_at=observed_at,
        market_status=str(row.get("mrkt_trtm_cls_name") or row.get("market_status") or "")
        or None,
        session_change_percent=first_float(
            row,
            (
                "bstp_nmix_prdy_ctrt",
                "bstp_nmix_prdy_ctrt_rate",
                "prdy_ctrt",
                "fluctuationsRatio",
            ),
        ),
    )


def kis_credentials() -> tuple[str, str]:
    app_key = os.environ.get("KIS_APP_KEY", "").strip().strip('"')
    app_secret = os.environ.get("KIS_APP_SECRET", "").strip().strip('"')
    if not app_key:
        raise RuntimeError("KIS_APP_KEY is required for kis_domestic_index price triggers")
    if not app_secret:
        raise RuntimeError("KIS_APP_SECRET is required for kis_domestic_index price triggers")
    return app_key, app_secret


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


def fetch_kis_token(app_key: str, app_secret: str, config: Config) -> str:
    return load_kis_token_module().get_token(app_key, app_secret, env_dv="real").token


def kis_request_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    url = KIS_BASE_URL + path
    if params:
        url = url + "?" + urlencode(params)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    delays = [1, 2, 4]
    last_error: Exception | None = None
    for attempt in range(len(delays) + 1):
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
            if not body.strip():
                return {}
            parsed = json.loads(body)
            if not isinstance(parsed, dict):
                raise RuntimeError("KIS response must be a JSON object")
            return parsed
        except HTTPError as exc:
            last_error = exc
            if exc.code in {400, 401, 403, 404}:
                raw = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"KIS request failed: HTTP {exc.code}: {raw}") from exc
        except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < len(delays):
            sleep(delays[attempt])
    raise RuntimeError(f"KIS request failed after retries: {last_error}")


def kis_response_success(body: dict[str, Any]) -> bool:
    rt_cd = str(body.get("rt_cd", "0"))
    return rt_cd in {"0", ""}


def first_output_row(body: dict[str, Any]) -> dict[str, Any]:
    output = body.get("output")
    if isinstance(output, dict):
        return output
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                return item
    raise RuntimeError("KIS index quote returned no output row")


def first_float(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = parse_float(row.get(key))
        if value is not None:
            return value
    return None


def kis_observed_at(row: dict[str, Any]) -> str:
    date = str(row.get("stck_bsop_date") or row.get("bsop_date") or "").strip()
    time_text = str(row.get("stck_cntg_hour") or row.get("cntg_hour") or "").strip()
    if len(date) == 8 and date.isdigit():
        if len(time_text) >= 6 and time_text[:6].isdigit():
            return (
                f"{date[0:4]}-{date[4:6]}-{date[6:8]}T"
                f"{time_text[0:2]}:{time_text[2:4]}:{time_text[4:6]}+09:00"
            )
        return f"{date[0:4]}-{date[4:6]}-{date[6:8]}"
    return datetime.now().astimezone().isoformat(timespec="seconds")


def fetch_naver_domestic_index(symbol: str) -> Quote:
    url = NAVER_INDEX_URL.format(symbol=symbol)
    request = Request(url, headers={"User-Agent": "codex-exec/price-trigger"})
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"naver index quote failed: HTTP {exc.code}: {raw}") from exc
    except URLError as exc:
        raise RuntimeError(f"naver index quote failed: {exc}") from exc

    datas = payload.get("datas")
    if not isinstance(datas, list) or not datas:
        raise RuntimeError(f"naver index quote returned no data for {symbol}")
    item = datas[0]
    if not isinstance(item, dict):
        raise RuntimeError(f"naver index quote returned malformed data for {symbol}")

    value = parse_float(item.get("closePriceRaw") or item.get("closePrice"))
    if value is None:
        raise RuntimeError(f"naver index quote did not include a numeric price for {symbol}")
    observed_at = str(item.get("localTradedAt") or payload.get("time") or datetime.now().isoformat())
    return Quote(
        symbol=str(item.get("symbolCode") or symbol),
        name=str(item.get("stockName") or symbol),
        value=value,
        observed_at=observed_at,
        market_status=str(item.get("marketStatus")) if item.get("marketStatus") else None,
        session_change_percent=parse_float(
            item.get("fluctuationsRatioRaw") or item.get("fluctuationsRatio")
        ),
    )


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text:
        return None
    return float(text)


def parse_config_time(value: Any, field_name: str) -> time | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must use HHMM string format")
    text = value
    if not CONFIG_TIME_RE.fullmatch(text):
        raise ValueError(f"{field_name} must use HHMM string format")
    try:
        return time(int(text[:2]), int(text[2:4]))
    except ValueError as exc:
        raise ValueError(f"{field_name} must use HHMM string format") from exc


def quoted_yaml_scalar_fields(text: str, field_names: set[str]) -> dict[str, bool]:
    node = yaml.compose(text)
    result: dict[str, bool] = {}
    if node is None or not isinstance(getattr(node, "value", None), list):
        return result
    for key_node, value_node in node.value:
        key = getattr(key_node, "value", None)
        if key in field_names:
            result[str(key)] = getattr(value_node, "style", None) in {"'", '"'}
    return result


def is_active_time(trigger_config: TriggerConfig, now: datetime) -> bool:
    start = trigger_config.active_start_time
    end = trigger_config.active_end_time
    if start is None or end is None:
        return True

    current = now.astimezone(KST).time().replace(second=0, microsecond=0)
    return start <= current <= end


def read_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "triggers": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("price trigger cache must contain a JSON object")
    data.setdefault("version", 1)
    data.setdefault("triggers", {})
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)


def write_cache(path: Path, data: dict[str, Any]) -> None:
    write_json(path, data)


def quote_history_path(cache_file: Path, configured: Any) -> Path:
    if configured:
        return Path(str(configured))
    return cache_file.with_name("quote-history.jsonl")


def touch_log_path(cache_file: Path, configured: Any) -> Path:
    if configured:
        return Path(str(configured))
    return cache_file.with_name("touch-events.jsonl")


def write_quote_history(path: Path, quote_items: Any) -> None:
    rows: list[str] = []
    recorded_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for quote_key, quote in quote_items:
        if not isinstance(quote, Quote) or quote.value <= 0:
            continue
        source = quote_key[0] if isinstance(quote_key, tuple) and quote_key else ""
        rows.append(
            json.dumps(
                {
                    "recorded_at": recorded_at,
                    "source": source,
                    "symbol": quote.symbol,
                    "name": quote.name,
                    "value": quote.value,
                    "observed_at": quote.observed_at,
                    "market_status": quote.market_status,
                    "session_change_percent": quote.session_change_percent,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        for row in rows:
            file.write(row + "\n")


def write_touch_event(
    path: Path,
    trigger: PriceTrigger,
    quote: Quote,
    reference: float,
    percent: float,
    direction_label: str,
) -> None:
    row = {
        "type": "price_trigger_touch",
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trigger_id": trigger.trigger_id,
        "case_title": trigger.case_title,
        "name": trigger.name,
        "symbol": trigger.symbol,
        "source": trigger.source,
        "direction": direction_label,
        "reference_value": reference,
        "touch_value": quote.value,
        "change_percent": round(percent, 4),
        "observed_at": quote.observed_at,
        "market_status": quote.market_status,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def update_touch_state(
    state: dict[str, Any],
    quote: Quote,
    reference: float,
    percent: float,
    direction: str,
) -> None:
    state.update(
        {
            "reference_value": quote.value,
            "reference_observed_at": quote.observed_at,
            "last_checked_value": quote.value,
            "last_checked_at": quote.observed_at,
            "last_checked_change_percent": round(percent, 4),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "last_touch": {
                "direction": direction,
                "previous_reference_value": reference,
                "touched_value": quote.value,
                "change_percent": round(percent, 4),
                "observed_at": quote.observed_at,
            },
        }
    )
