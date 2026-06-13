#!/usr/bin/env python3
"""Render holding quantity changes as Telegram-ready PNG time/quantity charts."""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - validated by runtime dependency checks
    Image = None
    ImageDraw = None
    ImageFont = None


KST = ZoneInfo("Asia/Seoul")
SYMBOLS_PER_IMAGE = 10
WIDTH = 2940
HEIGHT = 1640
POINT_RADIUS = 10
POINT_PADDING = POINT_RADIUS + 2
X_OVERLAP_OFFSET_STEP = 6
Y_OVERLAP_OFFSET_STEP = 4.5
PALETTE = [
    (37, 99, 235),
    (220, 38, 38),
    (22, 163, 74),
    (147, 51, 234),
    (234, 88, 12),
    (8, 145, 178),
    (190, 24, 93),
    (101, 163, 13),
    (79, 70, 229),
    (180, 83, 9),
]


def repo_root() -> Path | None:
    current = Path.cwd()
    for path in [current, *current.parents]:
        if (path / ".git").exists():
            return path
    return None


def default_csv_path() -> Path:
    configured = os.getenv("HOLDING_HISTORY_CSV", "").strip()
    if configured:
        return Path(configured)
    memory_root = os.getenv("DAILY_TRADING_MEMORY_DIR", "").strip()
    if memory_root:
        return Path(memory_root) / "show-holding-history" / "holding-changes.csv"
    root = repo_root()
    if root:
        return root / "memory" / "show-holding-history" / "holding-changes.csv"
    return Path.cwd() / "memory" / "show-holding-history" / "holding-changes.csv"


def parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def load_rows(path: Path, days: int, today: date) -> list[dict[str, str]]:
    if not path.exists():
        return []
    start = today - timedelta(days=days - 1)
    with path.open(newline="") as file:
        rows = []
        for row in csv.DictReader(file):
            row_date = parse_date(row.get("date", ""))
            if row_date and start <= row_date <= today:
                rows.append(row)
    return rows


def parse_datetime(row: dict[str, str]) -> datetime | None:
    value = row.get("timestamp_kst", "")
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    row_date = parse_date(row.get("date", ""))
    if row_date:
        return datetime.combine(row_date, time.min, tzinfo=KST)
    return None


def int_field(row: dict[str, str], name: str) -> int | None:
    value = row.get(name, "")
    if value == "":
        return None
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


def build_series(rows: list[dict[str, str]]) -> tuple[dict[str, list[tuple[datetime, int]]], dict[str, str]]:
    series: dict[str, list[tuple[datetime, int]]] = {}
    names: dict[str, str] = {}
    for row in sorted(rows, key=lambda item: (item.get("timestamp_kst", ""), item.get("symbol_id", ""))):
        observed_at = parse_datetime(row)
        symbol = row.get("symbol_id", "").strip()
        if not observed_at or not symbol:
            continue
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=KST)
        symbol_name = row.get("symbol_name", "").strip()
        if symbol_name:
            names[symbol] = symbol_name
        new_quantity = int_field(row, "new_quantity")
        if new_quantity is None:
            continue
        series.setdefault(symbol, []).append((observed_at, new_quantity))
    return series, names


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


def symbol_label(symbol: str, names: dict[str, str]) -> str:
    return f"{names[symbol]}({symbol})" if names.get(symbol) else symbol


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)] or [[]]


def symbol_max_quantity(series: dict[str, list[tuple[datetime, int]]], symbol: str) -> int:
    return max((quantity for _, quantity in series.get(symbol, [])), default=0)


def chart_path(base_path: Path, page_index: int, page_count: int) -> Path:
    if page_count == 1:
        return base_path
    return base_path.with_name(f"{base_path.stem}-{page_index:02d}{base_path.suffix}")


def text_size(draw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def connector_target(anchor: tuple[int, int], box: tuple[int, int, int, int]) -> tuple[int, int]:
    anchor_x, anchor_y = anchor
    if anchor_x < box[0]:
        target_x = box[0]
    elif anchor_x > box[2]:
        target_x = box[2]
    else:
        target_x = round((box[0] + box[2]) / 2)

    if anchor_y < box[1]:
        target_y = box[1]
    elif anchor_y > box[3]:
        target_y = box[3]
    else:
        target_y = round((box[1] + box[3]) / 2)
    return target_x, target_y


def major_y_ticks(max_y: int) -> list[int]:
    if max_y <= 10:
        return list(range(max_y, -1, -1))
    ticks = {0, max_y}
    for i in range(1, 5):
        ticks.add(round(max_y * i / 5))
    return sorted(ticks, reverse=True)


def point_for(
    observed_at: datetime,
    quantity: int,
    start_dt: datetime,
    span: float,
    left: int,
    plot_width: int,
    x_pad: int,
    top: int,
    plot_bottom: int,
    plot_height: int,
    max_y: int,
    y_pad: int,
    x_offset: int = 0,
    y_offset: int = 0,
) -> tuple[int, int]:
    inner_left = left + x_pad
    inner_right = left + plot_width - x_pad
    inner_width = inner_right - inner_left
    x = inner_left + round(((observed_at - start_dt).total_seconds() / span) * inner_width) + x_offset
    inner_top = top + y_pad
    inner_bottom = plot_bottom - y_pad
    inner_height = inner_bottom - inner_top
    y = inner_bottom - round((quantity / max_y) * inner_height) + y_offset
    return max(left, min(left + plot_width, x)), max(top, min(plot_bottom, y))


def place_inline_label(draw, label: str, points: list[tuple[int, int]], color, left: int, top: int, right: int, plot_bottom: int, font, used_boxes) -> None:
    label_width, label_height = text_size(draw, label, font)
    sample_indices = sorted(
        {
            0,
            max(0, len(points) // 3),
            max(0, len(points) // 2),
            max(0, min(len(points) - 1, (2 * len(points)) // 3)),
        }
    )
    offsets = [
        (86, -label_height - 58),
        (86, 58),
        (-label_width - 86, -label_height - 58),
        (-label_width - 86, 58),
        (132, -label_height - 96),
        (-label_width - 132, -label_height - 96),
        (132, 96),
        (-label_width - 132, 96),
    ]
    chosen_box = None
    chosen_position = None
    chosen_anchor = None
    for sample_index in sample_indices:
        x, y = points[sample_index]
        for offset_x, offset_y in offsets:
            label_x = min(max(left + 6, x + offset_x), WIDTH - right - label_width)
            label_y = min(max(top + 6, y + offset_y), plot_bottom - label_height - 8)
            box = (label_x - 8, label_y - 5, label_x + label_width + 8, label_y + label_height + 5)
            overlaps = any(
                not (box[2] < used[0] or used[2] < box[0] or box[3] < used[1] or used[3] < box[1])
                for used in used_boxes
            )
            if not overlaps:
                chosen_box = box
                chosen_position = (label_x, label_y)
                chosen_anchor = (x, y)
                break
        if chosen_position:
            break
    if chosen_position is None:
        x, y = points[min(len(points) - 1, len(points) // 2)]
        label_x = min(max(left + 6, x + 90), WIDTH - right - label_width)
        label_y = min(max(top + 6, y - label_height - 58), plot_bottom - label_height - 8)
        chosen_box = (label_x - 8, label_y - 5, label_x + label_width + 8, label_y + label_height + 5)
        chosen_position = (label_x, label_y)
        chosen_anchor = (x, y)
    used_boxes.append(chosen_box)
    label_x, label_y = chosen_position
    box = draw.textbbox((label_x, label_y), label, font=font)
    draw.line((chosen_anchor, connector_target(chosen_anchor, (box[0] - 8, box[1] - 5, box[2] + 8, box[3] + 5))), fill=color, width=2)
    draw.rectangle((box[0] - 8, box[1] - 5, box[2] + 8, box[3] + 5), fill=(248, 249, 250), outline=color, width=2)
    draw.text((label_x, label_y), label, fill=color, font=font)


def draw_legend(draw, symbols: list[str], names: dict[str, str], page_offset: int, top: int, left: int, font, text_color) -> None:
    col_width = 1320
    row_height = 34
    for index, symbol in enumerate(symbols):
        color = PALETTE[(page_offset + index) % len(PALETTE)]
        col = index % 2
        row = index // 2
        x = left + col * col_width
        y = top + row * row_height
        draw.rectangle((x, y + 7, x + 32, y + 23), fill=color)
        draw.text((x + 46, y), symbol_label(symbol, names), fill=text_color, font=font)


def write_chart(
    path: Path,
    series: dict[str, list[tuple[datetime, int]]],
    names: dict[str, str],
    symbols: list[str],
    page_index: int,
    page_count: int,
    days: int,
    today: date,
    max_y: int,
) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), (248, 249, 250))
    draw = ImageDraw.Draw(image)
    title_font = find_font(36)
    axis_font = find_font(22)
    label_font = find_font(24)
    legend_font = find_font(22)
    text_color = (31, 41, 55)
    muted = (107, 114, 128)
    grid = (209, 213, 219)
    unit_grid = (235, 237, 240)
    midnight_grid = (229, 231, 235)
    top = 95
    left = 110
    right = 80
    plot_height = 1220
    plot_width = WIDTH - left - right
    plot_bottom = top + plot_height
    legend_top = plot_bottom + 78

    title = f"Holding History {days}D to {today.isoformat()}"
    if page_count > 1:
        title += f" ({page_index}/{page_count})"
    draw.text((30, 24), title, fill=text_color, font=title_font)

    if not symbols:
        draw.text((left, top + 40), "No confirmed holding quantity data", fill=muted, font=label_font)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return

    start_dt = datetime.combine(today - timedelta(days=days - 1), time.min, tzinfo=KST)
    end_dt = datetime.combine(today, time.max, tzinfo=KST)
    span = max(1.0, (end_dt - start_dt).total_seconds())

    current_midnight = start_dt
    while current_midnight <= end_dt:
        x = left + round(((current_midnight - start_dt).total_seconds() / span) * plot_width)
        draw.line((x, top, x, plot_bottom), fill=midnight_grid, width=2)
        current_midnight += timedelta(days=1)

    for value in range(max_y + 1):
        y = plot_bottom - round((value / max_y) * plot_height)
        draw.line((left, y, left + plot_width, y), fill=unit_grid, width=1)

    for value in major_y_ticks(max_y):
        y = plot_bottom - round((value / max_y) * plot_height)
        draw.line((left, y, left + plot_width, y), fill=grid, width=2)
        draw.text((28, y - 14), str(value), fill=muted, font=axis_font)
    draw.line((left, top, left, plot_bottom), fill=text_color, width=4)
    draw.line((left, plot_bottom, left + plot_width, plot_bottom), fill=text_color, width=4)

    tick_count = min(10, max(4, days * 2))
    for i in range(tick_count):
        tick_dt = start_dt + (end_dt - start_dt) * (i / max(1, tick_count - 1))
        x = left + round(plot_width * i / max(1, tick_count - 1))
        draw.line((x, plot_bottom, x, plot_bottom + 10), fill=text_color, width=2)
        draw.text((x - 58, plot_bottom + 18), tick_dt.strftime("%m/%d %H:%M"), fill=muted, font=axis_font)

    used_boxes: list[tuple[int, int, int, int]] = []
    page_offset = (page_index - 1) * SYMBOLS_PER_IMAGE
    offset_center = (len(symbols) - 1) / 2
    x_pad = max(0, round(offset_center * X_OVERLAP_OFFSET_STEP) + POINT_PADDING)
    y_pad = max(0, round(offset_center * Y_OVERLAP_OFFSET_STEP) + POINT_RADIUS)
    for index, symbol in enumerate(symbols):
        color = PALETTE[(page_offset + index) % len(PALETTE)]
        x_offset = round((index - offset_center) * X_OVERLAP_OFFSET_STEP)
        y_offset = round((index - offset_center) * Y_OVERLAP_OFFSET_STEP)
        point_values = [
            (
                point_for(
                    observed_at,
                    quantity,
                    start_dt,
                    span,
                    left,
                    plot_width,
                    x_pad,
                    top,
                    plot_bottom,
                    plot_height,
                    max_y,
                    y_pad,
                    x_offset,
                    y_offset,
                ),
                quantity,
            )
            for observed_at, quantity in series[symbol]
        ]
        points = [point for point, _ in point_values]
        for point_index, (point, quantity) in enumerate(point_values):
            x, y = point
            if point_index:
                previous_point, _previous_quantity = point_values[point_index - 1]
                _, previous_y = previous_point
                elbow = (x, previous_y)
                draw.line((previous_point, elbow), fill=color, width=5)
                draw.line((elbow, point), fill=color, width=5)
            draw.rectangle((x - POINT_RADIUS, y - POINT_RADIUS, x + POINT_RADIUS, y + POINT_RADIUS), fill=color)
        if points:
            place_inline_label(draw, symbol_label(symbol, names), points, color, left, top, right, plot_bottom, label_font, used_boxes)

    draw_legend(draw, symbols, names, page_offset, legend_top, left, legend_font, text_color)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def write_png(path: Path, rows: list[dict[str, str]], days: int, today: date) -> list[Path]:
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required to render show-holding-history output with Korean labels")

    series, names = build_series(rows)
    symbols = sorted(series, key=lambda symbol: (-symbol_max_quantity(series, symbol), symbol))
    pages = chunked(symbols, SYMBOLS_PER_IMAGE)
    output_paths: list[Path] = []
    for page_index, page_symbols in enumerate(pages, start=1):
        page_max_qty = max((symbol_max_quantity(series, symbol) for symbol in page_symbols), default=1)
        max_y = max(1, page_max_qty)
        output_path = chart_path(path, page_index, len(pages))
        write_chart(output_path, series, names, page_symbols, page_index, len(pages), days, today, max_y)
        output_paths.append(output_path)
    return output_paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Render holding history CSV to PNG.")
    parser.add_argument("--days", type=int, required=True)
    parser.add_argument("--csv", dest="csv_path")
    parser.add_argument("--out")
    parser.add_argument("--today")
    args = parser.parse_args()
    if args.days <= 0:
        raise SystemExit("--days must be positive")
    today = date.fromisoformat(args.today) if args.today else datetime.now(KST).date()
    csv_path = Path(args.csv_path) if args.csv_path else default_csv_path()
    output_path = Path(args.out) if args.out else csv_path.parent / f"show-holding-history-{args.days}d.png"
    rows = load_rows(csv_path, args.days, today)
    image_paths = write_png(output_path, rows, args.days, today)
    print(
        json.dumps(
            {
                "csv_path": str(csv_path),
                "image_path": str(image_paths[0]),
                "image_paths": [str(path) for path in image_paths],
                "image_count": len(image_paths),
                "row_count": len(rows),
                "days": args.days,
                "today": today.isoformat(),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
