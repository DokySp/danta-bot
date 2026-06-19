#!/usr/bin/env python3
"""Render price-trigger touch alerts on top of the configured indicator chart."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import yaml

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - validated in the runtime image
    Image = None
    ImageDraw = None
    ImageFont = None


KST = ZoneInfo("Asia/Seoul")
WIDTH = 2200
HEIGHT = 1260
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_TOKEN_PATH = "/oauth2/tokenP"
KIS_INDEX_MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-indexchartprice"
KIS_INDEX_MINUTE_TR_ID = "FHKUP03500200"
KIS_CHART_INTERVAL_SECONDS = "1800"
KIS_INDEX_CODES = {
    "KOSPI": "0001",
    "KOSDAQ": "1001",
    "KOSPI200": "2001",
}


@dataclass(frozen=True)
class Trigger:
    trigger_id: str
    case_title: str
    name: str
    symbol: str
    source: str


@dataclass(frozen=True)
class Touch:
    observed_at: datetime
    direction: str
    reference_value: float
    value: float
    change_percent: float


@dataclass(frozen=True)
class Candle:
    observed_at: datetime
    open: float
    high: float
    low: float
    close: float


def default_config_path() -> Path:
    configured = os.getenv("PRICE_TRIGGER_FILE", "").strip()
    if configured:
        return Path(configured)
    return Path("/app/config/touch-points.yaml")


def load_triggers(path: Path) -> dict[str, Trigger]:
    if not path.exists():
        raise RuntimeError(f"price trigger config not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise RuntimeError("price trigger config must contain a YAML object")
    raw_triggers = data.get("touch_points", data.get("triggers", []))
    if not isinstance(raw_triggers, list):
        raise RuntimeError("price trigger config must contain a touch_points list")

    triggers: dict[str, Trigger] = {}
    for item in raw_triggers:
        if not isinstance(item, dict):
            continue
        trigger_id = str(item.get("id", "")).strip()
        symbol = str(item.get("symbol", "")).strip()
        if not trigger_id or not symbol:
            continue
        triggers[trigger_id] = Trigger(
            trigger_id=trigger_id,
            case_title=str(item.get("case_title") or trigger_id),
            name=str(item.get("name") or symbol),
            symbol=symbol,
            source=str(item.get("source") or "naver_domestic_index"),
        )
    return triggers


def quote_history_path(config_path: Path, state_dir: Path) -> Path:
    if not config_path.exists():
        return state_dir / "touch-points" / "quote-history.jsonl"
    data = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(data, dict):
        return state_dir / "touch-points" / "quote-history.jsonl"
    configured = data.get("quote_history_file")
    if configured:
        return Path(str(configured))
    cache_file = Path(data.get("cache_file") or state_dir / "touch-points" / "triggers.json")
    return cache_file.with_name("quote-history.jsonl")


def touch_log_path(config_path: Path, state_dir: Path) -> Path:
    if not config_path.exists():
        return state_dir / "touch-points" / "touch-events.jsonl"
    data = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(data, dict):
        return state_dir / "touch-points" / "touch-events.jsonl"
    configured = data.get("touch_log_file")
    if configured:
        return Path(str(configured))
    cache_file = Path(data.get("cache_file") or state_dir / "touch-points" / "triggers.json")
    return cache_file.with_name("touch-events.jsonl")


def candidate_touch_log_paths(args: argparse.Namespace, config_path: Path, state_dir: Path) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        expanded = path.expanduser()
        if expanded not in seen and expanded.exists():
            paths.append(expanded)
            seen.add(expanded)

    if args.touch_log:
        add(Path(args.touch_log))
        return paths

    configured = os.getenv("SHOW_TOUCH_POINT_TOUCH_LOG", "").strip()
    if configured:
        add(Path(configured))

    add(touch_log_path(config_path, state_dir))
    return paths


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def first_number(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = parse_number(str(row.get(key) or ""))
        if value is not None:
            return value
    return None


def parse_observed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def parse_touch_record(record: dict[str, Any], trigger: Trigger) -> Touch | None:
    if record.get("type") != "price_trigger_touch":
        return None
    if str(record.get("trigger_id") or "") != trigger.trigger_id:
        return None
    source = str(record.get("source") or "")
    symbol = str(record.get("symbol") or "").upper()
    if source and source != trigger.source:
        return None
    if symbol and symbol != trigger.symbol.upper():
        return None
    observed_at = parse_observed_at(str(record.get("observed_at") or ""))
    reference_value = parse_number(str(record.get("reference_value") or ""))
    value = parse_number(str(record.get("touch_value") or record.get("value") or ""))
    change_percent = parse_number(str(record.get("change_percent") or ""))
    direction = str(record.get("direction") or "")
    if observed_at is None or reference_value is None or value is None or change_percent is None:
        return None
    return Touch(
        observed_at=observed_at,
        direction=direction,
        reference_value=reference_value,
        value=value,
        change_percent=change_percent,
    )


def load_touches(
    paths: list[Path],
    trigger: Trigger,
) -> tuple[list[Touch], list[Path], int]:
    skipped_non_positive = 0
    touches: list[Touch] = []
    used_paths: list[Path] = []
    for path in paths:
        file_skipped = 0
        file_matched = False
        with path.open() as file:
            for raw_line in file:
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                touch = parse_touch_record(record, trigger)
                if touch is None:
                    continue
                if touch.value <= 0:
                    file_skipped += 1
                    continue
                touches.append(touch)
                file_matched = True
        if file_matched:
            used_paths.append(path)
        skipped_non_positive += file_skipped
    return sorted(touches, key=lambda item: item.observed_at), used_paths, skipped_non_positive


def env_credentials() -> tuple[str, str]:
    app_key = os.getenv("KIS_APP_KEY", "").strip().strip('"')
    app_secret = os.getenv("KIS_APP_SECRET", "").strip().strip('"')
    if not app_key:
        raise RuntimeError("KIS_APP_KEY is required to render the indicator series")
    if not app_secret:
        raise RuntimeError("KIS_APP_SECRET is required to render the indicator series")
    return app_key, app_secret


def token_cache_path(state_dir: Path) -> Path:
    configured = os.getenv("PRICE_TRIGGER_KIS_TOKEN_CACHE", "").strip()
    if configured:
        return Path(configured).expanduser()
    env = os.getenv("CODEX_MCP_TRADING_ENV", "paper").strip() or "paper"
    return state_dir / "touch-points" / f"kis-token-{env}.json"


def parse_kis_expiry(value: Any) -> datetime | None:
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


def cached_kis_token(state_dir: Path) -> str | None:
    path = token_cache_path(state_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    token = str(payload.get("access_token", "")).strip()
    expires_at = parse_kis_expiry(payload.get("expires_at"))
    if not token or expires_at is None:
        return None
    if datetime.now(timezone.utc) + timedelta(minutes=30) >= expires_at:
        return None
    return token


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(path)


def kis_request_json(
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    payload: Any = None,
    params: dict[str, str] | None = None,
    max_attempts: int = 4,
) -> tuple[dict[str, Any], dict[str, str]]:
    url = KIS_BASE_URL + path
    if params:
        url = url + "?" + urlencode(params)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    delays = [1, 2, 4][: max(0, max_attempts - 1)]
    last_error: Exception | None = None
    for attempt in range(len(delays) + 1):
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
                response_headers = {key.lower(): value for key, value in response.headers.items()}
            parsed = json.loads(body) if body.strip() else {}
            if not isinstance(parsed, dict):
                raise RuntimeError("KIS response must be a JSON object")
            return parsed, response_headers
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code in {400, 401, 403, 404}:
                raise RuntimeError(f"KIS request failed: HTTP {exc.code}: {raw}") from exc
            last_error = exc
        except (TimeoutError, URLError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < len(delays):
            time.sleep(delays[attempt])
    raise RuntimeError(f"KIS request failed after retries: {last_error}")


def fetch_kis_token(state_dir: Path) -> str:
    cached = cached_kis_token(state_dir)
    if cached:
        return cached
    app_key, app_secret = env_credentials()
    body, _headers = kis_request_json(
        "POST",
        KIS_TOKEN_PATH,
        headers={"content-type": "application/json; charset=utf-8"},
        payload={"grant_type": "client_credentials", "appkey": app_key, "appsecret": app_secret},
    )
    token = str(body.get("access_token", "")).strip()
    if not token:
        raise RuntimeError("KIS token response did not include access_token")
    expires_at = parse_kis_expiry(body.get("access_token_token_expired") or body.get("expires_at"))
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(hours=23)
    write_json(token_cache_path(state_dir), {"access_token": token, "expires_at": expires_at.isoformat()})
    return token


def kis_success(body: dict[str, Any]) -> bool:
    return str(body.get("rt_cd", "0")) in {"0", ""}


def output_rows(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = body.get(key)
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def parse_kis_minute(row: dict[str, Any]) -> Candle | None:
    row_date = str(row.get("stck_bsop_date") or "").strip()
    row_time = str(row.get("stck_cntg_hour") or "").strip()
    close = first_number(row, ("bstp_nmix_prpr", "stck_prpr", "close", "ovrs_nmix_prpr"))
    open_value = first_number(row, ("bstp_nmix_oprc", "stck_oprc", "open", "ovrs_nmix_oprc"))
    high = first_number(row, ("bstp_nmix_hgpr", "stck_hgpr", "high", "ovrs_nmix_hgpr"))
    low = first_number(row, ("bstp_nmix_lwpr", "stck_lwpr", "low", "ovrs_nmix_lwpr"))
    if len(row_date) != 8 or not row_date.isdigit() or close is None:
        return None
    if len(row_time) < 6 or not row_time[:6].isdigit():
        return None
    if int(row_time[0:2]) > 23 or int(row_time[2:4]) > 59 or int(row_time[4:6]) > 59:
        return None
    open_value = open_value if open_value is not None else close
    high = high if high is not None else max(open_value, close)
    low = low if low is not None else min(open_value, close)
    observed_at = datetime(
        int(row_date[0:4]),
        int(row_date[4:6]),
        int(row_date[6:8]),
        int(row_time[0:2]),
        int(row_time[2:4]),
        int(row_time[4:6]),
        tzinfo=KST,
    )
    return Candle(observed_at=observed_at, open=open_value, high=max(high, low), low=min(high, low), close=close)


def fetch_kis_index_series_once(
    trigger: Trigger,
    state_dir: Path,
) -> list[Candle]:
    app_key, app_secret = env_credentials()
    token = fetch_kis_token(state_dir)
    index_code = KIS_INDEX_CODES.get(trigger.symbol.upper(), trigger.symbol)
    params = {
        "FID_COND_MRKT_DIV_CODE": "U",
        "FID_ETC_CLS_CODE": "0",
        "FID_INPUT_ISCD": index_code,
        "FID_INPUT_HOUR_1": KIS_CHART_INTERVAL_SECONDS,
        "FID_PW_DATA_INCU_YN": "Y",
    }

    body, _response_headers = kis_request_json(
        "GET",
        KIS_INDEX_MINUTE_PATH,
        headers={
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": app_key,
            "appsecret": app_secret,
            "tr_id": KIS_INDEX_MINUTE_TR_ID,
            "custtype": "P",
        },
        params=params,
        max_attempts=1,
    )
    if not kis_success(body):
        message = body.get("msg1") or body.get("msg_cd") or body.get("rt_cd") or "api_failure"
        raise RuntimeError(f"KIS index minute chart failed: {message}")

    rows = output_rows(body, "output2")
    points = [point for row in rows if (point := parse_kis_minute(row)) is not None]
    deduped = {point.observed_at: point for point in points}
    return [deduped[key] for key in sorted(deduped)]


def fetch_kis_index_series(
    trigger: Trigger,
    state_dir: Path,
) -> list[Candle]:
    return fetch_kis_index_series_once(
        trigger,
        state_dir,
    )


def load_quote_history_series(path: Path, trigger: Trigger) -> list[Candle]:
    if not path.exists():
        return []
    points: dict[datetime, Candle] = {}
    with path.open() as file:
        for raw_line in file:
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            source = str(row.get("source") or "").strip()
            symbol = str(row.get("symbol") or "").strip().upper()
            if source != trigger.source or symbol != trigger.symbol.upper():
                continue
            observed_at = parse_observed_at(str(row.get("observed_at") or ""))
            close = first_number(row, ("close", "value"))
            open_value = first_number(row, ("open", "open_value", "value"))
            high = first_number(row, ("high", "high_value", "value"))
            low = first_number(row, ("low", "low_value", "value"))
            if observed_at is None or close is None or close <= 0:
                continue
            open_value = open_value if open_value is not None else close
            high = high if high is not None else max(open_value, close)
            low = low if low is not None else min(open_value, close)
            points[observed_at] = Candle(
                observed_at=observed_at,
                open=open_value,
                high=max(high, low),
                low=min(high, low),
                close=close,
            )
    return [points[key] for key in sorted(points)]


def filter_touches_to_candle_range(touches: list[Touch], candles: list[Candle]) -> tuple[list[Touch], int]:
    if not candles:
        return [], len(touches)
    start = candles[0].observed_at
    end = candles[-1].observed_at
    filtered = [touch for touch in touches if start <= touch.observed_at <= end]
    return filtered, len(touches) - len(filtered)


def expected_market_start(target_date: date) -> datetime:
    return datetime.combine(target_date, datetime.min.time(), tzinfo=KST) + timedelta(hours=9)


def default_output_dir() -> Path:
    configured = os.getenv("SHOW_TOUCH_POINT_OUTPUT_DIR", "").strip()
    if configured:
        return Path(configured)
    memory_root = os.getenv("DAILY_TRADING_MEMORY_DIR", "").strip()
    if memory_root:
        return Path(memory_root) / "show-touch-point"
    return Path("/workspace/memory/show-touch-point")


def output_path(args: argparse.Namespace, trigger_id: str) -> Path:
    if args.out:
        return Path(args.out)
    return default_output_dir() / f"show-touch-point-{trigger_id}.png"


def find_font(size: int):
    if ImageFont is None:
        return None
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def text_size(draw: Any, text: str, font: Any) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def nice_number(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:,.2f}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def draw_chart(
    path: Path,
    trigger: Trigger,
    touches: list[Touch],
    candles: list[Candle],
    warnings: list[str],
) -> None:
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required to render show-touch-point output")
    if not candles:
        raise RuntimeError("no indicator candles to render")

    image = Image.new("RGB", (WIDTH, HEIGHT), (248, 249, 250))
    draw = ImageDraw.Draw(image)
    title_font = find_font(40)
    subtitle_font = find_font(25)
    axis_font = find_font(22)
    label_font = find_font(24)
    small_font = find_font(19)

    text_color = (31, 41, 55)
    muted = (107, 114, 128)
    grid = (229, 231, 235)
    wick_color = (55, 65, 81)
    up_color = (220, 38, 38)
    down_color = (37, 99, 235)
    neutral_color = (147, 51, 234)

    left = 130
    right = 80
    top = 235
    bottom = 150
    plot_width = WIDTH - left - right
    plot_height = HEIGHT - top - bottom
    plot_bottom = top + plot_height

    values = [value for candle in candles for value in (candle.high, candle.low)] + [
        touch.value for touch in touches
    ]
    y_min = min(values)
    y_max = max(values)
    y_padding = max((y_max - y_min) * 0.08, y_max * 0.002, 1.0)
    y_min -= y_padding
    y_max += y_padding
    y_span = max(1.0, y_max - y_min)

    def x_for_index(index: int) -> int:
        if len(candles) == 1:
            return left + plot_width // 2
        return left + round((index / (len(candles) - 1)) * plot_width)

    def y_for_value(value: float) -> int:
        return top + plot_height - round(((value - y_min) / y_span) * plot_height)

    def nearest_candle_index(observed_at: datetime) -> int:
        return min(
            range(len(candles)),
            key=lambda index: abs((candles[index].observed_at - observed_at).total_seconds()),
        )

    title = f"{trigger.case_title} touch points"
    data_start = candles[0].observed_at
    data_end = candles[-1].observed_at
    subtitle = (
        f"{trigger.name} / {trigger.source} / 30-minute candles / "
        f"data {data_start.strftime('%m/%d %H:%M')} to {data_end.strftime('%m/%d %H:%M')}"
    )
    draw.text((34, 28), title, fill=text_color, font=title_font)
    draw.text((36, 80), subtitle, fill=muted, font=subtitle_font)

    for index in range(6):
        ratio = index / 5
        y = top + round(plot_height * ratio)
        value = y_max - y_span * ratio
        draw.line((left, y, left + plot_width, y), fill=grid, width=2)
        draw.text((30, y - 14), nice_number(value), fill=muted, font=axis_font)

    previous_date: date | None = None
    for index, candle in enumerate(candles):
        candle_date = candle.observed_at.date()
        if previous_date is not None and candle_date != previous_date:
            x = x_for_index(index)
            draw.line((x, top, x, plot_bottom), fill=(209, 213, 219), width=3)
            draw.text((x + 8, top + 8), candle.observed_at.strftime("%m/%d"), fill=muted, font=small_font)
        previous_date = candle_date

    tick_count = min(8, len(candles))
    for index in range(tick_count):
        candle_index = round(index * (len(candles) - 1) / max(1, tick_count - 1))
        tick_dt = candles[candle_index].observed_at
        x = x_for_index(candle_index)
        draw.line((x, top, x, plot_bottom), fill=grid, width=1)
        tick_label = tick_dt.strftime("%m/%d %H:%M")
        draw.text((x - 60, plot_bottom + 18), tick_label, fill=muted, font=axis_font)

    draw.line((left, top, left, plot_bottom), fill=text_color, width=4)
    draw.line((left, plot_bottom, left + plot_width, plot_bottom), fill=text_color, width=4)

    candle_width = max(5, min(24, round((plot_width / max(1, len(candles))) * 0.55)))
    for index, candle in enumerate(candles):
        x = x_for_index(index)
        high_y = y_for_value(candle.high)
        low_y = y_for_value(candle.low)
        open_y = y_for_value(candle.open)
        close_y = y_for_value(candle.close)
        color = up_color if candle.close >= candle.open else down_color
        draw.line((x, high_y, x, low_y), fill=wick_color, width=3)
        body_top = min(open_y, close_y)
        body_bottom = max(open_y, close_y)
        if body_bottom - body_top < 4:
            body_top -= 2
            body_bottom += 2
        draw.rectangle(
            (x - candle_width // 2, body_top, x + candle_width // 2, body_bottom),
            fill=color,
            outline=color,
        )

    used_label_boxes: list[tuple[int, int, int, int]] = []
    for index, touch in enumerate(touches, start=1):
        x = x_for_index(nearest_candle_index(touch.observed_at))
        y = y_for_value(touch.value)
        color = up_color if "상승" in touch.direction else down_color if "하락" in touch.direction else neutral_color
        draw.ellipse((x - 15, y - 15, x + 15, y + 15), fill=(255, 255, 255), outline=color, width=6)
        draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color)

        label = f"{index}. {touch.observed_at.strftime('%H:%M')} {nice_number(touch.value)} ({touch.change_percent:+.2f}%)"
        label_width, label_height = text_size(draw, label, label_font)
        label_y_base = 123 + ((index - 1) % 3) * (label_height + 18)
        label_x_base = x + 120
        if label_x_base + label_width > WIDTH - right:
            label_x_base = x - label_width - 120
        candidates = [(label_x_base, label_y_base), (x - label_width - 120, label_y_base), (x + 120, label_y_base)]
        label_x, label_y = left + 4, label_y_base
        label_box = (left - 4, label_y - 5, left + label_width + 12, label_y + label_height + 5)
        for candidate_x, candidate_y in candidates:
            candidate_x = min(max(left + 4, candidate_x), WIDTH - right - label_width)
            candidate_y = min(max(112, candidate_y), top - label_height - 14)
            box = (candidate_x - 8, candidate_y - 5, candidate_x + label_width + 8, candidate_y + label_height + 5)
            overlaps = any(
                not (box[2] < used[0] or used[2] < box[0] or box[3] < used[1] or used[3] < box[1])
                for used in used_label_boxes
            )
            if not overlaps:
                label_x, label_y = candidate_x, candidate_y
                label_box = box
                break
        used_label_boxes.append(label_box)
        connector_x = min(max(x, label_box[0]), label_box[2])
        connector_y = min(max(y, label_box[1]), label_box[3])
        draw.line((x, y, connector_x, connector_y), fill=color, width=1)
        draw.rectangle(label_box, fill=(255, 255, 255), outline=color, width=2)
        draw.text((label_x, label_y), label, fill=color, font=label_font)

    footer = f"touches={len(touches)} candles={len(candles)}"
    if warnings:
        footer += " / " + " / ".join(warnings[:3])
    draw.text((34, HEIGHT - 58), footer, fill=muted, font=small_font)

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def command_self_test() -> int:
    trigger = Trigger(
        trigger_id="kospi-case-1",
        case_title="case 1 - 기본 민감도",
        name="KOSPI",
        symbol="KOSPI",
        source="kis_domestic_index",
    )
    touch_record = {
        "type": "price_trigger_touch",
        "trigger_id": "kospi-case-1",
        "case_title": "case 1 - 기본 민감도",
        "name": "KOSPI",
        "symbol": "KOSPI",
        "source": "kis_domestic_index",
        "direction": "상승",
        "reference_value": 8864.24,
        "touch_value": 8963.76,
        "change_percent": 1.12,
        "observed_at": "2026-06-18T09:06:35+09:00",
    }
    parsed_touch = parse_touch_record(touch_record, trigger)
    wrong_touch = parse_touch_record({**touch_record, "trigger_id": "kospi-case-2"}, trigger)
    if parsed_touch is None or parsed_touch.value != 8963.76:
        raise RuntimeError("structured touch parsing failed")
    if parsed_touch.change_percent != 1.12:
        raise RuntimeError("structured touch change percent parsing failed")
    if wrong_touch is not None:
        raise RuntimeError("wrong trigger id should not match")

    with tempfile.TemporaryDirectory() as tmpdir:
        history_path = Path(tmpdir) / "history.jsonl"
        history_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "recorded_at": "2026-06-18T09:00:00+09:00",
                            "source": "kis_domestic_index",
                            "symbol": "KOSPI",
                            "open": 8950.0,
                            "high": 8970.0,
                            "low": 8940.0,
                            "close": 8960.0,
                            "value": 8960.0,
                            "observed_at": "2026-06-18T09:00:00+09:00",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "recorded_at": "2026-06-18T09:01:00+09:00",
                            "source": "naver_domestic_index",
                            "symbol": "KOSPI",
                            "value": 1.0,
                            "observed_at": "2026-06-18T09:01:00+09:00",
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n"
        )
        candles = load_quote_history_series(history_path, trigger)
        if (
            len(candles) != 1
            or candles[0].open != 8950.0
            or candles[0].high != 8970.0
            or candles[0].low != 8940.0
            or candles[0].close != 8960.0
        ):
            raise RuntimeError("quote history loading failed")

        parsed = parse_kis_minute(
            {
                "stck_bsop_date": "20260618",
                "stck_cntg_hour": "090000",
                "bstp_nmix_oprc": "8950.00",
                "bstp_nmix_hgpr": "8970.00",
                "bstp_nmix_lwpr": "8940.00",
                "bstp_nmix_prpr": "8960.00",
            }
        )
        if parsed is None or parsed.open != 8950.0 or parsed.high != 8970.0 or parsed.low != 8940.0:
            raise RuntimeError("KIS minute candle parsing failed")
        summary_row = parse_kis_minute(
            {
                "stck_bsop_date": "20260618",
                "stck_cntg_hour": "999999",
                "bstp_nmix_prpr": "9063.84",
            }
        )
        if summary_row is not None:
            raise RuntimeError("KIS summary row should be skipped")

        calls: list[str] = []
        original_fetch_once = fetch_kis_index_series_once

        def fake_fetch_once(
            fake_trigger: Trigger,
            fake_state_dir: Path,
        ) -> list[Candle]:
            calls.append(str(fake_state_dir))
            return [
                Candle(
                    observed_at=expected_market_start(date(2026, 6, 18)),
                    open=100.0,
                    high=102.0,
                    low=99.0,
                    close=101.0,
                )
            ]

        try:
            globals()["fetch_kis_index_series_once"] = fake_fetch_once
            merged = fetch_kis_index_series(
                trigger,
                Path(tmpdir),
            )
        finally:
            globals()["fetch_kis_index_series_once"] = original_fetch_once

        if calls != [tmpdir]:
            raise RuntimeError(f"KIS single-call lookup failed: {calls}")
        if len(merged) != 1 or merged[0].observed_at != expected_market_start(date(2026, 6, 18)):
            raise RuntimeError("KIS single-call result handling failed")

    print("self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Render price-trigger touch points to PNG.")
    parser.add_argument("trigger_id", nargs="?")
    parser.add_argument("--config")
    parser.add_argument("--state-dir", default=os.getenv("STATE_DIR", "/state"))
    parser.add_argument("--touch-log")
    parser.add_argument("--out")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        return command_self_test()
    if not args.trigger_id:
        raise SystemExit("trigger_id is required")

    config_path = Path(args.config) if args.config else default_config_path()
    triggers = load_triggers(config_path)
    trigger = triggers.get(args.trigger_id)
    if trigger is None:
        valid = ", ".join(sorted(triggers)) or "(none)"
        raise SystemExit(f"unknown price trigger id: {args.trigger_id}; valid ids: {valid}")

    state_dir = Path(args.state_dir)
    touch_log_paths = candidate_touch_log_paths(args, config_path, state_dir)
    if not touch_log_paths:
        raise SystemExit("no price-trigger touch log found")
    touches, touch_log_paths_used, skipped_non_positive = load_touches(touch_log_paths, trigger)
    if not touches:
        detail = "0 이하 터치값은 그래프에서 제외했습니다." if skipped_non_positive else "matching touch alerts not found."
        raise SystemExit(f"no valid touch alerts for {trigger.trigger_id}: {detail}")

    warnings: list[str] = []
    if skipped_non_positive:
        warnings.append(f"skipped_non_positive={skipped_non_positive}")

    history_path = quote_history_path(config_path, state_dir)
    series_source = f"kis_index_chart_{KIS_CHART_INTERVAL_SECONDS}s"
    candles: list[Candle] = []
    fetch_error: Exception | None = None
    if not args.no_fetch and trigger.source == "kis_domestic_index":
        try:
            candles = fetch_kis_index_series(trigger, state_dir)
            series_source = f"kis_index_chart_{KIS_CHART_INTERVAL_SECONDS}s"
            if not candles:
                raise RuntimeError("KIS returned no intraday indicator series")
        except Exception as exc:
            fetch_error = exc
    elif args.no_fetch:
        fetch_error = RuntimeError("--no-fetch enabled")
    else:
        fetch_error = RuntimeError(f"{trigger.source} requires quote_history_file for indicator series")

    if not candles:
        candles = load_quote_history_series(history_path, trigger)
        if candles:
            series_source = "quote_history"
            if fetch_error is not None:
                warnings.append(f"kis_fetch_failed={fetch_error}")
    if not candles:
        detail = f"KIS fetch failed: {fetch_error}" if fetch_error is not None else "KIS returned no indicator series"
        raise SystemExit(f"indicator series is required; {detail}")

    touches, touches_outside_chart_range = filter_touches_to_candle_range(touches, candles)
    if touches_outside_chart_range:
        warnings.append(f"touches_outside_chart_range={touches_outside_chart_range}")

    image_path = output_path(args, trigger.trigger_id)
    draw_chart(image_path, trigger, touches, candles, warnings)
    data_start = candles[0].observed_at.isoformat()
    data_end = candles[-1].observed_at.isoformat()
    print(
        json.dumps(
            {
                "image_path": str(image_path),
                "touch_log_path": str(touch_log_paths_used[0]) if touch_log_paths_used else None,
                "touch_log_paths": [str(path) for path in touch_log_paths_used],
                "config_path": str(config_path),
                "quote_history_path": str(history_path),
                "trigger_id": trigger.trigger_id,
                "case_title": trigger.case_title,
                "name": trigger.name,
                "symbol": trigger.symbol,
                "source": trigger.source,
                "data_start": data_start,
                "data_end": data_end,
                "touch_count": len(touches),
                "skipped_non_positive": skipped_non_positive,
                "series_count": len(candles),
                "candle_count": len(candles),
                "interval_seconds": int(KIS_CHART_INTERVAL_SECONDS),
                "series_source": series_source,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
