import csv
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import Config
from .errors import UserFacingError
from .daily_trading import CodexRunContext

HOLDING_HISTORY_HEADER = [
    "timestamp_kst",
    "date",
    "run_id",
    "symbol_id",
    "symbol_name",
    "direction",
    "old_quantity",
    "new_quantity",
    "delta_quantity",
    "submitted_quantity",
    "order_or_reservation_id",
    "row_id",
    "source_artifact",
]


def holding_history_csv_path(workspace_dir: Path) -> Path:
    configured = os.getenv("HOLDING_HISTORY_CSV", "").strip()
    if configured:
        return Path(configured)
    memory_root = os.getenv("DAILY_TRADING_MEMORY_DIR", "").strip()
    if memory_root:
        return Path(memory_root) / "show-holding-history" / "holding-changes.csv"
    return workspace_dir / "memory" / "show-holding-history" / "holding-changes.csv"


def append_holding_history_from_run(workspace_dir: Path, context: CodexRunContext) -> int:
    run_dir = workspace_dir / "reports" / "runs" / context.run_id
    execution_path = run_dir / "execution.json"
    if not execution_path.is_file():
        return 0
    try:
        payload = json.loads(execution_path.read_text())
    except (OSError, json.JSONDecodeError):
        logging.exception("failed to read execution artifact path=%s", execution_path)
        return 0

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = payload if isinstance(payload, dict) else {}
    request_type = str(data.get("request_type", "")).lower()
    if "resv" in request_type or "reservation" in request_type or "예약" in request_type:
        return 0
    orders = data.get("orders", [])
    if not isinstance(orders, list):
        return 0

    rows: list[dict[str, str]] = []
    for index, order in enumerate(orders):
        if not isinstance(order, dict):
            continue
        if str(order.get("result", "")).lower() != "submitted":
            continue
        direction = str(order.get("direction", "")).lower()
        if direction not in {"buy", "sell"}:
            continue
        order_type = str(order.get("order_type", "")).lower()
        if "resv" in order_type or "reservation" in order_type or "예약" in order_type:
            continue
        quantity = int_value(order.get("validated_order_quantity"))
        if quantity <= 0:
            continue
        current_quantity = int_value(order.get("current_live_holding_quantity"))
        delta = quantity if direction == "buy" else -quantity
        rows.append(
            {
                "timestamp_kst": context.started_at,
                "date": context.started_at[:10],
                "run_id": context.run_id,
                "symbol_id": str(order.get("symbol_id", "")).strip(),
                "symbol_name": str(order.get("symbol_name", "")).strip(),
                "direction": direction,
                "old_quantity": str(current_quantity),
                "new_quantity": str(current_quantity + delta),
                "delta_quantity": str(delta),
                "submitted_quantity": str(quantity),
                "order_or_reservation_id": str(order.get("order_or_reservation_id", "")).strip(),
                "row_id": str(index),
                "source_artifact": str(execution_path.relative_to(workspace_dir)),
            }
        )
    if not rows:
        return 0

    csv_path = holding_history_csv_path(workspace_dir)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    existing_keys: set[tuple[str, str, str, str, str]] = set()
    if csv_path.exists():
        try:
            with csv_path.open(newline="") as file:
                for row in csv.DictReader(file):
                    existing_keys.add(
                        (
                            row.get("run_id", ""),
                            row.get("symbol_id", ""),
                            row.get("direction", ""),
                            row.get("order_or_reservation_id", ""),
                            row.get("row_id", ""),
                        )
                    )
        except OSError:
            logging.exception("failed to read holding history path=%s", csv_path)

    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    written = 0
    with csv_path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=HOLDING_HISTORY_HEADER)
        if write_header:
            writer.writeheader()
        for row in rows:
            key = (
                row["run_id"],
                row["symbol_id"],
                row["direction"],
                row["order_or_reservation_id"],
                row["row_id"],
            )
            if key in existing_keys:
                continue
            writer.writerow(row)
            existing_keys.add(key)
            written += 1
    if written:
        logging.info("appended holding history rows=%s path=%s", written, csv_path)
    return written


def int_value(value: Any) -> int:
    try:
        return int(str(value or "0").replace(",", ""))
    except ValueError:
        return 0


def parse_show_holding_history_command(text: str) -> int | None:
    parts = text.strip().split()
    if not parts or parts[0] != "$show-holding-history":
        return None
    if len(parts) == 1:
        return 7
    if len(parts) != 2:
        raise UserFacingError(
            "invalid show-holding-history arguments",
            "사용법: <code>$show-holding-history</code> 또는 <code>$show-holding-history 7</code>",
        )
    try:
        days = int(parts[1])
    except ValueError as exc:
        raise UserFacingError(
            "invalid show-holding-history days",
            "일수는 숫자로 입력해주세요. 예: <code>$show-holding-history 7</code>",
        ) from exc
    if days <= 0:
        raise UserFacingError(
            "invalid show-holding-history days",
            "일수는 1 이상의 숫자로 입력해주세요.",
        )
    return days


def render_holding_history(config: Config, days: int) -> dict[str, Any]:
    script = (
        config.workspace_dir
        / "containers"
        / "codex-exec"
        / "profiles"
        / "base"
        / "skills"
        / "show-holding-history"
        / "scripts"
        / "render_holding_history.py"
    )
    if not script.exists():
        script = config.codex_home / "skills" / "show-holding-history" / "scripts" / "render_holding_history.py"
    if not script.exists():
        raise RuntimeError(f"show-holding-history renderer not found: {script}")
    result = subprocess.run(
        ["python3", str(script), "--days", str(days)],
        cwd=config.workspace_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"show-holding-history failed: {result.stderr.strip() or result.stdout.strip()}")
    return json.loads(result.stdout)
