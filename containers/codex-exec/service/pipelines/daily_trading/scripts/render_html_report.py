#!/usr/bin/env python3
"""Render a single-file cumulative daily-trading HTML report from run artifacts."""

from __future__ import annotations

import argparse
import html
import json
import math
import sys
from pathlib import Path
from typing import Any


ROLE_LABELS = {
    "analyst-momentum-cycle": "Momentum / Cycle",
    "analyst-news-flow": "News / Flow",
    "analyst-quality-value": "Quality / Value",
    "analyst-risk-allocation": "Risk / Allocation",
}
PHASE_LABELS = {
    "opening": "Opening",
    "rebuttal-1": "Rebuttal 1",
}
SIDE_LABELS = {"bull": "Bull", "bear": "Bear"}
REGIME_LABELS = {
    "insufficient_market_data": "시장 데이터 부족",
    "neutral": "중립",
    "risk_on": "강세",
    "weak_downside": "약세",
    "panic_downside": "급락",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def decimal(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def signed_decimal(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):+.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def analyst_score_sort_key(item: dict[str, Any]) -> tuple[float, str, str]:
    try:
        score = float(item.get("final_first_score"))
    except (TypeError, ValueError):
        score = math.inf
    return score, str(item.get("symbol_name") or ""), str(item.get("symbol_id") or "")


def analyst_score_class(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return ""
    if score <= 4:
        return " score-low"
    if score >= 6:
        return " score-high"
    return ""


def time_text(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else "-"


def status_badge(status: Any) -> str:
    value = str(status or "-")
    css = "ok" if value == "success" else "warn" if value == "partial" else "bad"
    return f'<span class="badge {css}">{esc(value)}</span>'


def find_runs(runs_root: Path, target_started_at: str) -> list[dict[str, Any]]:
    day = target_started_at[:10]
    runs: list[dict[str, Any]] = []
    for path in runs_root.iterdir():
        summary_path = path / "pipeline-summary.json"
        if not path.is_dir() or not summary_path.is_file():
            continue
        summary = load_json(summary_path)
        started_at = str(summary.get("started_at") or "")
        if started_at[:10] != day or started_at > target_started_at:
            continue
        runs.append(
            {
                "path": path,
                "summary": summary,
                "execution": load_json(path / "execution.json"),
                "lifecycle": load_json(path / "order-lifecycle.json"),
                "decision": load_json(path / "decision-brief.json"),
                "market": load_json(path / "market-index-snapshot.json"),
            }
        )
    ordered_runs = sorted(runs, key=lambda item: str(item["summary"].get("started_at") or ""))
    latest_broker_by_order_id: dict[str, dict[str, Any]] = {}
    for run in ordered_runs:
        lifecycle_orders = run["lifecycle"].get("previous_submitted_cash_orders", [])
        for item in lifecycle_orders if isinstance(lifecycle_orders, list) else []:
            if not isinstance(item, dict):
                continue
            order_id = str(item.get("order_id") or "").strip()
            broker = item.get("broker_reconciliation")
            if order_id and isinstance(broker, dict):
                latest_broker_by_order_id[order_id] = broker
    for run in ordered_runs:
        execution = run["execution"]
        orders = execution.get("orders") if isinstance(execution, dict) else None
        if not isinstance(orders, list):
            continue
        execution["orders"] = [
            {
                **item,
                **(
                    {"broker_reconciliation": latest_broker_by_order_id[str(item.get("order_or_reservation_id") or "").strip()]}
                    if str(item.get("order_or_reservation_id") or "").strip() in latest_broker_by_order_id
                    else {}
                ),
            }
            if isinstance(item, dict)
            else item
            for item in orders
        ]
    return ordered_runs


def execution_counts(execution: dict[str, Any]) -> tuple[int, int, int]:
    orders = execution.get("orders") if isinstance(execution.get("orders"), list) else []
    submitted = sum(1 for item in orders if isinstance(item, dict) and item.get("result") == "submitted")
    blocked = sum(1 for item in orders if isinstance(item, dict) and item.get("result") in {"blocked", "failed"})
    skipped = sum(1 for item in orders if isinstance(item, dict) and item.get("result") == "skipped")
    return submitted, blocked, skipped


def broker_reconciliation(order: dict[str, Any]) -> dict[str, Any]:
    value = order.get("broker_reconciliation")
    return value if isinstance(value, dict) else {}


def broker_status_text(order: dict[str, Any]) -> str:
    broker = broker_reconciliation(order)
    status = str(broker.get("status") or "")
    if status == "filled":
        return f"KIS 체결 {number(broker.get('filled_quantity'))}주"
    if status == "partially_filled":
        return f"KIS 일부 체결 {number(broker.get('filled_quantity'))}주 · 잔량 {number(broker.get('remaining_quantity'))}주"
    if status == "partially_filled_rejected":
        return f"KIS 일부 체결 {number(broker.get('filled_quantity'))}주 · 잔여 거절"
    if status == "partially_filled_canceled":
        return f"KIS 일부 체결 {number(broker.get('filled_quantity'))}주 · 잔여 취소"
    if status == "rejected":
        return f"KIS 거절 {number(broker.get('rejected_quantity'))}주"
    if status == "canceled":
        return f"KIS 취소 {number(broker.get('canceled_quantity'))}주"
    if status in {"pending", "accepted"}:
        return f"KIS 미체결 · 잔량 {number(broker.get('remaining_quantity'))}주"
    if status == "unconfirmed":
        return "KIS 상태 미확인"
    return ""


ADVERSE_TERMINAL_BROKER_STATUSES = {
    "rejected",
    "canceled",
    "partially_filled_rejected",
    "partially_filled_canceled",
}


def requested_order_quantity(order: dict[str, Any]) -> int:
    return int(order.get("validated_order_quantity") or order.get("quantity") or 0)


def fill_is_complete(order: dict[str, Any], fill: dict[str, Any] | None) -> bool:
    if not fill:
        return False
    requested_quantity = requested_order_quantity(order)
    filled_quantity = int(fill.get("filled_quantity") or 0)
    return requested_quantity > 0 and filled_quantity >= requested_quantity


def order_status_text(order: dict[str, Any], fill: dict[str, Any] | None = None) -> str:
    broker_status = str(broker_reconciliation(order).get("status") or "")
    if broker_status in ADVERSE_TERMINAL_BROKER_STATUSES:
        return broker_status_text(order)
    if fill:
        if fill_is_complete(order, fill):
            return f"체결 {time_text(fill.get('filled_at'))}"
        requested_quantity = requested_order_quantity(order)
        filled_quantity = int(fill.get("filled_quantity") or 0)
        if requested_quantity > 0:
            return f"일부 체결 {number(filled_quantity)}/{number(requested_quantity)}주 · {time_text(fill.get('filled_at'))}"
    return broker_status_text(order) or "주문 제출 · 체결 미확인"


def order_status_badge(order: dict[str, Any], fill: dict[str, Any] | None = None) -> str:
    broker_status = str(broker_reconciliation(order).get("status") or "")
    is_complete = fill_is_complete(order, fill) or (not fill and broker_status == "filled")
    css = "ok" if is_complete and broker_status not in ADVERSE_TERMINAL_BROKER_STATUSES else "warn"
    return f'<span class="badge {css}">{esc(order_status_text(order, fill))}</span>'


def index_map(market: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = market.get("indexes") if isinstance(market.get("indexes"), list) else []
    return {str(item.get("symbol")): item for item in rows if isinstance(item, dict)}


def cumulative_today_fills(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    latest_status = "unavailable"
    latest_scope = "unknown"
    for run in runs:
        payload = load_json(run["path"] / "today-fills.json")
        latest_status = str(payload.get("status") or "unavailable")
        if payload.get("skipped"):
            latest_status = "skipped"
        latest_scope = str(payload.get("fill_scope") or "universe")
        fills = payload.get("fills") if isinstance(payload.get("fills"), list) else []
        for item in fills:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("order_id") or ""),
                str(item.get("symbol_id") or ""),
                str(item.get("direction") or ""),
                str(item.get("filled_at") or ""),
                int(item.get("filled_quantity") or 0),
                int(item.get("filled_price") or 0),
            )
            by_key.setdefault(key, item)
    fills = sorted(by_key.values(), key=lambda item: (str(item.get("filled_at") or ""), str(item.get("order_id") or "")))
    return fills, latest_status, latest_scope


def render_header(summary: dict[str, Any], run_count: int, fills: list[dict[str, Any]], submitted_orders: list[dict[str, Any]]) -> str:
    account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
    asset = summary.get("account_asset_summary") if isinstance(summary.get("account_asset_summary"), dict) else {}
    started_at = str(summary.get("started_at") or "")
    cut_off = time_text(started_at)
    status = str(summary.get("status") or "-")
    run_id = str(summary.get("run_id") or "-")
    run_suffix = run_id.rpartition("-")[2]
    run_id_hint = ""
    if len(run_suffix) == 8 and all(character in "0123456789abcdefABCDEF" for character in run_suffix):
        run_id_hint = "<small>마지막 8자리는 해시가 아니라 같은 초에 시작한 실행을 구분하는 임의 식별자입니다.</small>"
    status_label = "실행 성공" if status == "success" else "부분 완료" if status == "partial" else "실행 실패"
    status_css = "success" if status == "success" else ""
    asset_metric = ""
    if asset.get("total_asset_amount") is not None:
        asset_metric = (
            f"<article><span>KIS 총자산</span><strong>{number(asset.get('total_asset_amount'))}원</strong>"
            "<small>account asset 조회값 · 원금 수익률 아님</small></article>"
        )
    return f"""
    <header class="hero">
      <div class="eyebrow">CUMULATIVE DAILY TRADING REPORT</div>
      <h1>당일 누적 거래·판단 리포트</h1>
      <p>{esc(cut_off)}까지 확인된 당일 run·주문·체결과 각 시간대의 Analyst/Judge 판단을 함께 보여줍니다.</p>
      <div class="chips">
        <span class="chip {status_css}">● {esc(status_label)}</span>
        <span class="chip">{esc(started_at)}</span>
        <span class="chip">run {run_count}회</span>
        <span class="chip">체결 {len(fills)}건</span>
        <span class="chip">봇 주문 제출 {len(submitted_orders)}건</span>
      </div>
      <div class="run-id"><span>실행 ID</span><code>{esc(run_id)}</code>{run_id_hint}</div>
    </header>
    <section class="metrics">
      <article><span>총평가</span><strong>{number(account.get('total_evaluation_amount'))}원</strong><small>주식 {number(account.get('securities_valuation_amount'))}원</small></article>
      <article><span>평가손익</span><strong class="negative">{number(account.get('total_pnl_amount'))}원</strong><small>전체 매입가 대비</small></article>
      <article><span>주문가능</span><strong>{number(account.get('orderable_cash_amount'))}원</strong><small>D+2 기준</small></article>
      <article><span>당일 누적 매수</span><strong>{number(((account.get('today_trade_amounts') or {}).get('buy_amount')))}원</strong><small>계좌 누계</small></article>
      {asset_metric}
    </section>
    """


def render_trade_ledger(
    runs: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    fill_status: str,
    fill_scope: str,
) -> tuple[str, list[dict[str, Any]]]:
    fill_by_order = {str(item.get("order_id")): item for item in fills if item.get("order_id")}
    submitted_orders: list[dict[str, Any]] = []
    for run in runs:
        started_at = run["summary"].get("started_at")
        orders = run["execution"].get("orders") if isinstance(run["execution"].get("orders"), list) else []
        for item in orders:
            if not isinstance(item, dict) or item.get("result") != "submitted":
                continue
            row = dict(item)
            row["run_started_at"] = started_at
            row["fill"] = fill_by_order.get(str(item.get("order_or_reservation_id") or ""))
            submitted_orders.append(row)

    ledger_rows: list[tuple[str, str]] = []
    linked_order_ids: set[str] = set()
    for item in submitted_orders:
        fill = item.get("fill") if isinstance(item.get("fill"), dict) else None
        result = order_status_badge(item, fill)
        direction = "매수" if item.get("direction") == "buy" else "매도"
        order_id = str(item.get("order_or_reservation_id") or "")
        if fill is not None:
            linked_order_ids.add(order_id)
        time_cell = f"주문 {time_text(item.get('run_started_at'))}"
        fill_cell = "-"
        if fill is not None:
            time_cell += f"<br><small>체결 {time_text(fill.get('filled_at'))}</small>"
            fill_cell = (
                f"{number(fill.get('filled_quantity'))}주<br>"
                f"<small>{number(fill.get('filled_price'))}원 · {number(fill.get('filled_amount'))}원</small>"
            )
        ledger_rows.append(
            (
                str(item.get("run_started_at") or ""),
                f"<tr><td>{time_cell}</td><td>봇</td>"
                f"<td><strong>{esc(item.get('symbol_name'))}</strong><br><code>{esc(item.get('symbol_id'))}</code></td>"
                f"<td>{direction}</td>"
                f"<td>{number(item.get('validated_order_quantity') or item.get('quantity'))}주<br><small>{number(item.get('order_price'))}원</small></td>"
                f"<td>{fill_cell}</td><td><code>{esc(order_id)}</code></td><td>{result}</td></tr>",
            )
        )

    for item in fills:
        order_id = str(item.get("order_id") or "")
        if order_id in linked_order_ids:
            continue
        actor = "사용자 직접" if item.get("source_actor") == "non_bot_user" else "봇"
        direction = "매수" if item.get("direction") == "buy" else "매도"
        ledger_rows.append(
            (
                str(item.get("filled_at") or ""),
                f"<tr><td>체결 {time_text(item.get('filled_at'))}</td><td>{esc(actor)}</td>"
                f"<td><strong>{esc(item.get('symbol_name'))}</strong><br><code>{esc(item.get('symbol_id'))}</code></td>"
                f"<td>{direction}</td><td>-</td>"
                f"<td>{number(item.get('filled_quantity'))}주<br><small>{number(item.get('filled_price'))}원 · {number(item.get('filled_amount'))}원</small></td>"
                f"<td><code>{esc(order_id)}</code></td><td><span class=\"badge ok\">체결 확인</span></td></tr>",
            )
        )
    ledger_html = "".join(row for _, row in sorted(ledger_rows, key=lambda item: item[0]))

    cut_off = time_text(runs[-1]["summary"].get("started_at")) if runs else "-"
    if fill_status == "success" and fill_scope == "account":
        fill_notice = "계좌 전체 일별 체결 조회를 기준으로 주문번호를 제출 run에 연결합니다. 보고서 시점 이후 체결은 추정하지 않습니다."
    else:
        fill_notice = (
            f"체결 수집 상태 {fill_status}, 범위 {fill_scope}이므로 계좌의 당일 전체 체결로 확정할 수 없습니다. "
            "확인된 artifact만 표시합니다."
        )
    content = f"""
    <section class="panel" id="trades">
      <div class="section-head"><div><p class="kicker">DAY LEDGER</p><h2>{esc(cut_off)}까지의 당일 전체 거래</h2></div><span class="badge info">체결 {len(fills)} · 봇 제출 {len(submitted_orders)}</span></div>
      <div class="notice">{esc(fill_notice)}</div>
      <h3>주문·체결 통합 원장</h3>
      <div class="table-wrap"><table><thead><tr><th>시각</th><th>주체</th><th>종목</th><th>방향</th><th>주문</th><th>체결</th><th>주문번호</th><th>{esc(cut_off)} 기준 상태</th></tr></thead><tbody>{ledger_html or '<tr><td colspan="8">확인된 주문·체결 없음</td></tr>'}</tbody></table></div>
    </section>
    """
    return content, submitted_orders


def render_time_symbol_inspector(runs: list[dict[str, Any]], fills: list[dict[str, Any]]) -> str:
    fill_by_order = {
        str(item.get("order_id")): item
        for item in fills
        if isinstance(item, dict) and item.get("order_id")
    }
    order_run_index: dict[str, int] = {}
    for index, run in enumerate(runs):
        execution_orders = run["execution"].get("orders") if isinstance(run["execution"].get("orders"), list) else []
        for order in execution_orders:
            if not isinstance(order, dict) or order.get("result") != "submitted":
                continue
            order_id = str(order.get("order_or_reservation_id") or "")
            if order_id:
                order_run_index[order_id] = index

    fills_by_run: dict[int, list[dict[str, Any]]] = {index: [] for index in range(len(runs))}
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        order_id = str(fill.get("order_id") or "")
        if order_id in order_run_index:
            target_index = order_run_index[order_id]
        else:
            fill_time = str(fill.get("filled_at") or "")
            target_index = next(
                (
                    index
                    for index, run in enumerate(runs)
                    if str(run["summary"].get("started_at") or "") >= fill_time
                ),
                len(runs) - 1,
            )
        fills_by_run[target_index].append(fill)

    time_buttons = []
    time_panels = []
    for run_index, run in enumerate(runs):
        run_dir = run["path"]
        summary = run["summary"]
        started_at = str(summary.get("started_at") or "")
        run_time = time_text(started_at)
        time_key = f"run-{run_index}-{run_time.replace(':', '')}"
        is_active_time = run_index == len(runs) - 1

        execution_orders = run["execution"].get("orders") if isinstance(run["execution"].get("orders"), list) else []
        submitted_orders = [
            item for item in execution_orders if isinstance(item, dict) and item.get("result") == "submitted"
        ]
        linked_fills = fills_by_run[run_index]
        submitted_order_ids = {
            str(item.get("order_or_reservation_id") or "") for item in submitted_orders
        }
        unmatched_fills = [
            item for item in linked_fills if str(item.get("order_id") or "") not in submitted_order_ids
        ]
        analyst = load_json(run_dir / "analyst-review.json")
        analyst_symbols = sorted(
            (item for item in analyst.get("symbols", []) if isinstance(item, dict)),
            key=analyst_score_sort_key,
        )
        debate = load_json(run_dir / "judge-debate.json")
        final = load_json(run_dir / "judge-review.json")
        final_by_symbol = {
            str(item.get("symbol_id")): item for item in final.get("symbols", []) if isinstance(item, dict)
        }
        decision_by_symbol = {
            str(item.get("symbol_id")): item for item in run["decision"].get("symbols", []) if isinstance(item, dict)
        }

        preferred_symbol = ""
        if submitted_orders:
            preferred_symbol = str(submitted_orders[0].get("symbol_id") or "")
        elif linked_fills:
            preferred_symbol = str(linked_fills[0].get("symbol_id") or "")
        elif final_by_symbol:
            preferred_symbol = next(iter(final_by_symbol))
        elif analyst_symbols:
            preferred_symbol = str(analyst_symbols[0].get("symbol_id") or "")

        activity_cards = []
        for order in submitted_orders:
            direction = "매수" if order.get("direction") == "buy" else "매도"
            order_id = str(order.get("order_or_reservation_id") or "")
            fill = fill_by_order.get(order_id)
            if fill_is_complete(order, fill) and str(broker_reconciliation(order).get("status") or "") not in ADVERSE_TERMINAL_BROKER_STATUSES:
                activity_cards.append(
                    f"<article class=\"activity-card filled\"><span>주문 후 체결</span><strong>{esc(order.get('symbol_name'))} {esc(direction)} {number(fill.get('filled_quantity'))}주</strong>"
                    f"<small>주문 {run_time} · {number(order.get('order_price'))}원 → 체결 {time_text(fill.get('filled_at'))} · {number(fill.get('filled_price'))}원 · <code>{esc(order_id)}</code></small></article>"
                )
            else:
                status_text = order_status_text(order, fill)
                activity_cards.append(
                    f"<article class=\"activity-card order\"><span>{esc(status_text)}</span><strong>{esc(order.get('symbol_name'))} {esc(direction)} {number(order.get('validated_order_quantity') or order.get('quantity'))}주</strong>"
                    f"<small>주문 {run_time} · {number(order.get('order_price'))}원 · <code>{esc(order_id)}</code></small></article>"
                )
        for fill in unmatched_fills:
            actor = "사용자 직접" if fill.get("source_actor") == "non_bot_user" else "연결 주문 없음"
            direction = "매수" if fill.get("direction") == "buy" else "매도"
            activity_cards.append(
                f"<article class=\"activity-card fill\"><span>{esc(actor)} 체결</span><strong>{esc(fill.get('symbol_name'))} {esc(direction)} {number(fill.get('filled_quantity'))}주</strong>"
                f"<small>체결 {time_text(fill.get('filled_at'))} · {number(fill.get('filled_price'))}원 · <code>{esc(fill.get('order_id'))}</code></small></article>"
            )
        if not activity_cards:
            activity_cards.append('<div class="empty-state">이 run에 연결된 주문 또는 체결이 없습니다.</div>')

        time_buttons.append(
            f'<button type="button" class="time-button{" active" if is_active_time else ""}" data-time-target="{esc(time_key)}">'
            f'<strong>{esc(run_time)}</strong><span>Analyst {len(analyst_symbols)} · Judge {len(final_by_symbol)}</span>'
            f'<small>주문 {len(submitted_orders)} · 체결 {len(linked_fills)}</small></button>'
        )

        symbol_buttons = []
        symbol_panels = []
        for analyst_item in analyst_symbols:
            symbol_id = str(analyst_item.get("symbol_id") or "")
            symbol_name = str(analyst_item.get("symbol_name") or symbol_id)
            composite_key = f"{time_key}-{symbol_id}"
            is_active_symbol = symbol_id == preferred_symbol
            final_item = final_by_symbol.get(symbol_id)
            related_orders = [item for item in submitted_orders if str(item.get("symbol_id") or "") == symbol_id]
            related_fills = [item for item in linked_fills if str(item.get("symbol_id") or "") == symbol_id]
            has_trade = bool(related_orders or related_fills)
            judge_label = "Judge 진행" if final_item else "Analyst only"
            symbol_buttons.append(
                f'<button type="button" class="trade-symbol-button{analyst_score_class(analyst_item.get("final_first_score"))}{" active" if is_active_symbol else ""}" data-symbol-target="{esc(composite_key)}">'
                f'<span class="symbol-button-left"><span class="symbol-button-status"><b class="mini-badge {"judge" if final_item else "analyst"}">{judge_label}</b>'
                f'{"<b class=\"mini-badge trade\">거래</b>" if has_trade else ""}</span>'
                f'<strong class="symbol-button-name" title="{esc(symbol_name)}">{esc(symbol_name)}</strong></span>'
                f'<span class="symbol-button-right"><b class="symbol-score">{decimal(analyst_item.get("final_first_score"))}</b><code>{esc(symbol_id)}</code></span></button>'
            )

            score_rows = []
            for score in analyst_item.get("agent_scores", []):
                if not isinstance(score, dict):
                    continue
                excluded = bool(score.get("excluded_from_aggregation"))
                missing = score.get("missing_data") if isinstance(score.get("missing_data"), list) else []
                score_rows.append(
                    f"<tr><td>{esc(ROLE_LABELS.get(str(score.get('agent_role')), score.get('agent_role')))}</td>"
                    f"<td><strong>{decimal(score.get('score'), 1)}</strong></td>"
                    f"<td>{'<span class=\"badge warn\">평균 제외</span>' if excluded else '<span class=\"badge ok\">평균 포함</span>'}</td>"
                    f"<td>{esc(score.get('reason_code'))}</td><td>{esc(score.get('one_line_reason'))}</td>"
                    f"<td>{esc(', '.join(str(value) for value in missing) or '-')}</td></tr>"
                )

            phase_blocks = []
            for phase in debate.get("phases", []):
                if not isinstance(phase, dict):
                    continue
                side_blocks = []
                for side in ("bull", "bear"):
                    payload = ((phase.get("sides") or {}).get(side) or {}).get("output") or {}
                    symbol_item = next(
                        (
                            item
                            for item in payload.get("symbols", [])
                            if isinstance(item, dict) and str(item.get("symbol_id")) == symbol_id
                        ),
                        None,
                    )
                    if symbol_item is not None:
                        side_blocks.append(render_debate_symbol(symbol_item, side))
                if side_blocks:
                    phase_blocks.append(
                        f"<section class=\"phase compact-phase\"><div class=\"phase-title\"><span>{esc(PHASE_LABELS.get(str(phase.get('phase')), phase.get('phase')))}</span>"
                        f"<small>{esc(symbol_name)} Bull/Bear 판단</small></div>{''.join(side_blocks)}</section>"
                    )

            if final_item:
                account_exposure = (decision_by_symbol.get(symbol_id) or {}).get("account_exposure") or {}
                judge_html = (
                    f"{''.join(phase_blocks)}<article class=\"final-card full\"><div><h3>Final Judge</h3><span class=\"badge info\">rank {number(final_item.get('relative_attractiveness_rank'))}</span></div>"
                    f"<div class=\"final-numbers\"><span>현재 {number(account_exposure.get('current_live_holding_quantity'))}주</span>"
                    f"<span>최종 {number(final_item.get('final_holding_quantity'))}주</span><span>목표 {number(final_item.get('target_position_value_krw'))}원</span></div>"
                    f"<p><code>{esc(final_item.get('reason_code'))}</code></p><p>{esc(final_item.get('one_line_reason'))}</p></article>"
                )
            else:
                judge_html = '<div class="empty-state">Analyst 평가는 완료됐지만 이 run의 Judge shortlist에는 선정되지 않았습니다.</div>'

            trade_notes = []
            related_order_ids = {
                str(order.get("order_or_reservation_id") or "") for order in related_orders
            }
            for order in related_orders:
                direction = "매수" if order.get("direction") == "buy" else "매도"
                order_id = str(order.get("order_or_reservation_id") or "")
                fill = fill_by_order.get(order_id)
                if fill_is_complete(order, fill) and str(broker_reconciliation(order).get("status") or "") not in ADVERSE_TERMINAL_BROKER_STATUSES:
                    trade_notes.append(
                        f"{run_time} 봇 {direction} 주문 · {time_text(fill.get('filled_at'))} {number(fill.get('filled_quantity'))}주 체결"
                    )
                else:
                    trade_notes.append(
                        f"{run_time} 봇 {direction} {number(order.get('validated_order_quantity') or order.get('quantity'))}주 주문 제출 · {order_status_text(order, fill)}"
                    )
            for fill in related_fills:
                if str(fill.get("order_id") or "") in related_order_ids:
                    continue
                actor = "사용자 직접" if fill.get("source_actor") == "non_bot_user" else "연결 주문 없는"
                direction = "매수" if fill.get("direction") == "buy" else "매도"
                trade_notes.append(
                    f"{time_text(fill.get('filled_at'))} {actor} {direction} {number(fill.get('filled_quantity'))}주 체결"
                )
            trade_note = " · ".join(trade_notes) or "이 run에 연결된 거래 없음 · Analyst 평가만 표시"
            symbol_panels.append(
                f"<section class=\"symbol-analysis-panel{' active' if is_active_symbol else ''}\" data-symbol-panel=\"{esc(composite_key)}\">"
                f"<div class=\"symbol-focus-head\"><div><p class=\"kicker\">RUN {esc(run_time)} · SYMBOL ANALYSIS</p><h2>{esc(symbol_name)} <code>{esc(symbol_id)}</code></h2>"
                f"<p>{esc(trade_note)}</p></div><div class=\"focus-badges\"><span class=\"badge info\">Analyst 평균 {decimal(analyst_item.get('final_first_score'))}</span>"
                f"<span class=\"badge {'ok' if final_item else 'muted'}\">{esc(judge_label)}</span></div></div>"
                f"<section class=\"inline-analysis\"><h3>Analyst 상세 점수</h3><div class=\"table-wrap\"><table><thead><tr><th>역할</th><th>점수</th><th>집계</th><th>코드</th><th>상세 근거</th><th>누락 데이터</th></tr></thead><tbody>{''.join(score_rows)}</tbody></table></div></section>"
                f"<section class=\"inline-judge\"><h3>Judge 단계별 판단</h3>{judge_html}</section></section>"
            )

        time_panels.append(
            f'<section class="time-analysis-panel{" active" if is_active_time else ""}" data-time-panel="{esc(time_key)}">'
            f'<div class="time-panel-head"><div><p class="kicker">TIME WINDOW</p><h2>{esc(run_time)} 거래·종목 판단</h2>'
            f'<p>{esc(run_time)} run의 주문과 연결 체결, 직접 체결, Analyst/Judge 결과입니다.</p></div>'
            f'<span class="badge info">Analyst {len(analyst_symbols)} · Judge {len(final_by_symbol)}</span></div>'
            f'<div class="run-activity">{"".join(activity_cards)}</div>'
            f'<h3>전체 Analyst 대상 종목</h3><div class="trade-symbol-selector">{"".join(symbol_buttons)}</div>'
            f'<div class="symbol-analysis-content">{"".join(symbol_panels)}</div></section>'
        )

    return f"""
    <section class="panel" id="trade-symbol-analysis">
      <div class="section-head"><div><p class="kicker">TIME &amp; SYMBOL FOCUS</p><h2>시간대별 거래·전체 종목 판단</h2></div><span class="badge info">시간 → 종목 순서로 선택</span></div>
      <p class="section-note">시간대를 선택하면 해당 run의 주문과 주문번호로 연결된 체결, Analyst 대상 종목 전체를 표시합니다. Judge 미진입 종목도 Analyst 상세 점수를 확인할 수 있습니다.</p>
      <div class="time-selector">{''.join(time_buttons)}</div>
      <div class="time-analysis-content">{''.join(time_panels)}</div>
    </section>
    """


def render_run_timeline(runs: list[dict[str, Any]]) -> str:
    rows = []
    total_tokens = 0
    for run in runs:
        summary = run["summary"]
        account = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
        tokens = (((summary.get("token_usage") or {}).get("total") or {}).get("total_tokens") or 0)
        total_tokens += int(tokens)
        submitted, blocked, skipped = execution_counts(run["execution"])
        strategy = run["decision"].get("strategy_context") if isinstance(run["decision"].get("strategy_context"), dict) else {}
        indexes = index_map(run["market"])
        kospi = indexes.get("KOSPI", {})
        kosdaq = indexes.get("KOSDAQ", {})
        rows.append(
            f"<tr><td>{time_text(summary.get('started_at'))}</td><td>{status_badge(summary.get('status'))}</td>"
            f"<td>{esc(strategy.get('regime') or '-')}</td>"
            f"<td>{decimal(kospi.get('change_percent'))}% / {decimal(kosdaq.get('change_percent'))}%</td>"
            f"<td>{submitted} / {blocked} / {skipped}</td>"
            f"<td>{number(account.get('total_evaluation_amount'))}원</td>"
            f"<td>{number(account.get('total_pnl_amount'))}원</td><td>{number(tokens)}</td></tr>"
        )
    return f"""
    <section class="panel" id="runs">
      <div class="section-head"><div><p class="kicker">RUN HISTORY</p><h2>당일 실행 타임라인</h2></div><span class="badge info">누적 토큰 {number(total_tokens)}</span></div>
      <div class="table-wrap"><table><thead><tr><th>시각</th><th>상태</th><th>regime</th><th>KOSPI / KOSDAQ</th><th>제출 / 차단 / 스킵</th><th>총평가</th><th>평가손익</th><th>토큰</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    </section>
    """


def normalized_positions(values: list[float], top: float, plot_height: float) -> list[float]:
    low = min(values) if values else 0.0
    high = max(values) if values else 1.0
    span = high - low
    if span == 0:
        return [top + plot_height / 2 for _ in values]
    return [top + (high - value) / span * plot_height for value in values]


def render_combined_chart(runs: list[dict[str, Any]]) -> str:
    width, height = 1100, 390
    left, right, top, bottom = 58, 28, 34, 70
    plot_width = width - left - right
    plot_height = height - top - bottom
    rows = []
    chart_runs: list[dict[str, Any]] = []
    total_values: list[float] = []
    pnl_values: list[float] = []
    kospi_values: list[float | None] = []
    kospi_change_values: list[float | None] = []
    for run in runs:
        account = run["summary"].get("account_display_summary")
        account = account if isinstance(account, dict) else {}
        indexes = index_map(run["market"])
        kospi = indexes.get("KOSPI") or {}
        try:
            total = float(account.get("total_evaluation_amount"))
            pnl = float(account.get("total_pnl_amount"))
        except (TypeError, ValueError):
            continue
        try:
            kospi_value = float(kospi.get("value"))
            kospi_change = float(kospi.get("change_percent"))
        except (TypeError, ValueError):
            kospi_value = None
            kospi_change = None
        chart_runs.append(run)
        total_values.append(total)
        pnl_values.append(pnl)
        kospi_values.append(kospi_value)
        kospi_change_values.append(kospi_change)
    if not chart_runs:
        return (
            '<section class="combined-chart-card"><div class="chart-head"><div>'
            '<p class="kicker">INTRADAY COMBINED CHART</p><h2>계좌·시장 통합 추이</h2>'
            '</div></div><div class="empty-state">총평가·평가손익 시계열을 그릴 수 있는 run이 없습니다.</div></section>'
        )
    total_y = normalized_positions(total_values, top, plot_height)
    pnl_y = normalized_positions(pnl_values, top, plot_height)
    kospi_indexes = [index for index, value in enumerate(kospi_values) if value is not None]
    available_kospi_values = [float(kospi_values[index]) for index in kospi_indexes]
    available_kospi_y = normalized_positions(available_kospi_values, top, plot_height)
    kospi_y_by_index = dict(zip(kospi_indexes, available_kospi_y))
    pnl_overlaps_total = len(total_y) == len(pnl_y) and all(
        abs(total_point - pnl_point) < 0.5 for total_point, pnl_point in zip(total_y, pnl_y)
    )
    xs = [left + plot_width * index / max(1, len(chart_runs) - 1) for index in range(len(chart_runs))]
    total_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, total_y))
    pnl_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, pnl_y))
    kospi_points = " ".join(f"{xs[index]:.2f},{kospi_y_by_index[index]:.2f}" for index in kospi_indexes)
    grid = []
    for step in range(5):
        y = top + plot_height * step / 4
        grid.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_width}" y2="{y:.2f}" class="chart-grid"/>')
    x_labels = []
    for index, (run, x) in enumerate(zip(chart_runs, xs)):
        strategy_context = run["decision"].get("strategy_context")
        strategy_context = strategy_context if isinstance(strategy_context, dict) else {}
        regime = str(strategy_context.get("regime") or "-")
        if index % 2 == 0 or index == len(chart_runs) - 1:
            x_labels.append(
                f'<text x="{x:.2f}" y="{height - 29}" text-anchor="middle" class="chart-x">{esc(time_text(run["summary"].get("started_at")))}</text>'
            )
        rows.append(
            {
                "time": time_text(run["summary"].get("started_at")),
                "x": round(x, 2),
                "total": int(total_values[index]),
                "pnl": int(pnl_values[index]),
                "kospi": round(kospi_values[index], 2) if kospi_values[index] is not None else None,
                "kospiChangePercent": round(kospi_change_values[index], 2)
                if kospi_change_values[index] is not None
                else None,
                "regime": regime,
                "regimeLabel": REGIME_LABELS.get(regime, regime),
                "totalY": round(total_y[index], 2),
                "pnlY": round(pnl_y[index], 2),
                "kospiY": round(kospi_y_by_index[index], 2) if index in kospi_y_by_index else None,
            }
        )
    points_json = esc(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
    pnl_line = "" if pnl_overlaps_total else f'<polyline points="{pnl_points}" class="series-line pnl-line"/>'
    pnl_marker = "" if pnl_overlaps_total else '<circle class="chart-marker pnl-marker" r="6"/>'
    kospi_line = f'<polyline points="{kospi_points}" class="series-line kospi-line"/>' if kospi_points else ""
    kospi_marker = '<circle class="chart-marker kospi-marker" r="6"/>' if kospi_points else ""
    if kospi_indexes:
        latest_kospi_index = kospi_indexes[-1]
        kospi_legend = (
            f"KOSPI {decimal(kospi_values[latest_kospi_index])} "
            f"({signed_decimal(kospi_change_values[latest_kospi_index])}%)"
        )
        kospi_range = (
            f"<span>KOSPI {decimal(min(available_kospi_values))}~{decimal(max(available_kospi_values))}</span>"
        )
    else:
        kospi_legend = "KOSPI 조회 실패"
        kospi_range = "<span>KOSPI 조회 실패</span>"
    return f"""
    <section class="combined-chart-card" data-chart-points="{points_json}">
      <div class="chart-head"><div><p class="kicker">INTRADAY COMBINED CHART</p><h2>계좌·시장 통합 추이</h2><p>서로 다른 단위는 각 series의 당일 범위로 정규화했습니다. 평가손익과 총평가의 궤적이 같으면 총평가 선만 표시하되 hover에는 두 값을 모두 제공합니다. 확인되지 않은 원금이나 계좌수익률은 추정하지 않습니다.</p></div></div>
      <div class="chart-legend"><span><i style="--legend:#4f6df5"></i>총평가</span><span><i style="--legend:#e14c68"></i>평가손익</span><span><i style="--legend:#0b9a86"></i>{kospi_legend}</span></div>
      <div class="interactive-chart">
        <svg class="interactive-line-chart" viewBox="0 0 {width} {height}" role="img" aria-label="총평가 평가손익 KOSPI 통합 시계열">
          {''.join(grid)}
          <polyline points="{total_points}" class="series-line total-line"/>
          {pnl_line}
          {kospi_line}
          <line class="chart-cursor" x1="{left}" x2="{left}" y1="{top}" y2="{top + plot_height}"/>
          <circle class="chart-marker total-marker" r="6"/>{pnl_marker}{kospi_marker}
          {''.join(x_labels)}
          <rect class="chart-hit-area" x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" data-left="{left}" data-width="{plot_width}"/>
        </svg>
        <div class="chart-tooltip" aria-live="polite"></div>
      </div>
      <div class="chart-scrubber">
        <div class="chart-scrubber-label"><span>조회 시점</span><output class="chart-scrubber-time">{esc(rows[-1]['time'])}</output></div>
        <input class="chart-range-slider" type="range" min="0" max="{len(rows) - 1}" step="1" value="{len(rows) - 1}" aria-label="차트 조회 시점">
        <div class="chart-scrubber-ends"><span>{esc(rows[0]['time'])}</span><span>{esc(rows[-1]['time'])}</span></div>
      </div>
      <div class="series-ranges"><span>총평가 {number(min(total_values))}~{number(max(total_values))}원</span><span>평가손익 {number(min(pnl_values))}~{number(max(pnl_values))}원</span>{kospi_range}</div>
    </section>
    """


def pie_slice_path(cx: float, cy: float, radius: float, start_angle: float, end_angle: float) -> str:
    start_radians = math.radians(start_angle - 90)
    end_radians = math.radians(end_angle - 90)
    start_x = cx + radius * math.cos(start_radians)
    start_y = cy + radius * math.sin(start_radians)
    end_x = cx + radius * math.cos(end_radians)
    end_y = cy + radius * math.sin(end_radians)
    large_arc = 1 if end_angle - start_angle > 180 else 0
    return (
        f"M {cx:.2f} {cy:.2f} L {start_x:.2f} {start_y:.2f} "
        f"A {radius:.2f} {radius:.2f} 0 {large_arc} 1 {end_x:.2f} {end_y:.2f} Z"
    )


def financial_industry_by_symbol(decision: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    symbols = decision.get("symbols") if isinstance(decision.get("symbols"), list) else []
    for item in symbols:
        if not isinstance(item, dict):
            continue
        financial = item.get("financial_summary") if isinstance(item.get("financial_summary"), dict) else {}
        details = financial.get("items") if isinstance(financial.get("items"), list) else []
        industry = next(
            (
                str(detail).removeprefix("업종 ").strip()
                for detail in details
                if str(detail).startswith("업종 ") and str(detail).removeprefix("업종 ").strip()
            ),
            "업종 미확인",
        )
        result[str(item.get("symbol_id") or "")] = industry
    return result


def render_holdings(target_dir: Path, summary: dict[str, Any]) -> str:
    account = load_json(target_dir / "account-before-order.json")
    decision = load_json(target_dir / "decision-brief.json")
    industry_by_symbol = financial_industry_by_symbol(decision)
    symbols = account.get("symbols") if isinstance(account.get("symbols"), list) else []
    positions = [item for item in symbols if isinstance(item, dict) and int(item.get("current_live_holding_quantity") or 0) > 0]
    positions.sort(key=lambda item: int(item.get("valuation_amount") or 0), reverse=True)
    pie_total = sum(int(item.get("valuation_amount") or 0) for item in positions)
    palette = ("#5367E8", "#0B9A86", "#E66786", "#E69B36", "#7B5ED7", "#3583D8", "#A46A4E", "#6B7A90", "#B864B9", "#D05B50")
    industries = sorted({industry_by_symbol.get(str(item.get("symbol_id") or ""), "업종 미확인") for item in positions})
    industry_colors = {industry: palette[index % len(palette)] for index, industry in enumerate(industries)}
    industry_totals = {industry: 0 for industry in industries}
    for item in positions:
        symbol_id = str(item.get("symbol_id") or "")
        industry_totals[industry_by_symbol.get(symbol_id, "업종 미확인")] += int(item.get("valuation_amount") or 0)
    industry_order = sorted(industries, key=lambda industry: (-industry_totals[industry], industry))
    industry_rank = {industry: index for index, industry in enumerate(industry_order)}
    pie_positions = sorted(
        positions,
        key=lambda item: (
            industry_rank[industry_by_symbol.get(str(item.get("symbol_id") or ""), "업종 미확인")],
            -int(item.get("valuation_amount") or 0),
        ),
    )

    rows = []
    slices = []
    start_angle = 0.0
    for item in pie_positions:
        symbol_id = str(item.get("symbol_id") or "")
        symbol_name = str(item.get("symbol_name") or symbol_id)
        valuation = int(item.get("valuation_amount") or 0)
        pie_weight = (valuation / pie_total * 100) if pie_total else 0
        industry = industry_by_symbol.get(symbol_id, "업종 미확인")
        end_angle = start_angle + (valuation / pie_total * 360 if pie_total else 0)
        path = pie_slice_path(180, 180, 142, start_angle, end_angle)
        color = industry_colors[industry]
        slices.append(
            f'<path class="pie-slice" d="{path}" fill="{color}" tabindex="0" '
            f'data-symbol-name="{esc(symbol_name)}" data-symbol-id="{esc(symbol_id)}" data-industry="{esc(industry)}" '
            f'data-valuation="{valuation}" data-weight="{pie_weight:.4f}" data-quantity="{number(item.get("current_live_holding_quantity"))}" '
            f'data-pnl="{number(item.get("pnl_amount"))}" data-pnl-rate="{decimal(item.get("pnl_rate"))}" '
            f'aria-label="{esc(symbol_name)} {esc(industry)} 평가액 {number(valuation)}원 비중 {decimal(pie_weight)}%">'
            f'<title>{esc(symbol_name)} · {esc(industry)} · {number(valuation)}원 · {decimal(pie_weight)}%</title></path>'
        )
        start_angle = end_angle
    for item in positions:
        symbol_id = str(item.get("symbol_id") or "")
        symbol_name = str(item.get("symbol_name") or symbol_id)
        valuation = int(item.get("valuation_amount") or 0)
        weight = (valuation / pie_total * 100) if pie_total else 0
        industry = industry_by_symbol.get(symbol_id, "업종 미확인")
        pnl_css = "positive" if float(item.get("pnl_amount") or 0) >= 0 else "negative"
        rows.append(
            f"<tr><td><strong>{esc(symbol_name)}</strong><br><code>{esc(symbol_id)}</code></td><td>{esc(industry)}</td>"
            f"<td>{number(item.get('current_live_holding_quantity'))}주</td><td>{number(valuation)}원</td>"
            f"<td>{decimal(weight)}%</td><td class=\"{pnl_css}\">{number(item.get('pnl_amount'))}원</td>"
            f"<td class=\"{pnl_css}\">{decimal(item.get('pnl_rate'))}%</td></tr>"
        )
    legend = []
    for industry in industry_order:
        total = industry_totals[industry]
        weight = (total / pie_total * 100) if pie_total else 0
        legend.append(
            f'<article class="sector-legend-item"><i style="--sector-color:{industry_colors[industry]}"></i><div>'
            f'<strong>{esc(industry)}</strong><small>{number(total)}원 · {decimal(weight)}%</small></div></article>'
        )
    cut_off = time_text(summary.get("started_at"))
    return f"""
    <section class="panel" id="holdings">
      <div class="section-head"><div><p class="kicker">PORTFOLIO</p><h2>{esc(cut_off)} 주문 제출 직전 보유 현황</h2></div><span class="badge info">보유 {len(positions)}종목</span></div>
      <div class="notice">이 표는 해당 run의 주문 전 계좌 조회 기준입니다. 제출 주문은 체결이 확인되기 전까지 보유수량 변화로 반영하지 않습니다.</div>
      <div class="portfolio-chart-layout">
        <div class="portfolio-pie" data-pie-total="{pie_total}">
          <svg class="portfolio-pie-svg" viewBox="0 0 360 360" role="img" aria-label="보유 종목 평가액 비중 파이차트">{''.join(slices)}</svg>
          <div class="pie-tooltip" aria-live="polite"></div>
        </div>
        <div class="sector-legend"><div><p class="kicker">SECTOR COLORS</p><h3>업종별 색상</h3><p>같은 업종 종목은 같은 색상으로 표시합니다.</p></div>{''.join(legend)}</div>
      </div>
      <p class="source-note">업종은 {esc(cut_off)} decision-brief의 종목별 financial_summary를 사용했습니다. 파이는 현금 제외 주식 평가액 {number(pie_total)}원을 기준으로 합니다.</p>
      <div class="table-wrap holdings-table"><table><thead><tr><th>종목</th><th>업종</th><th>수량</th><th>평가액</th><th>주식 내 비중</th><th>평가손익</th><th>수익률</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    </section>
    """


def render_market_and_quality(target_dir: Path, summary: dict[str, Any]) -> str:
    decision = load_json(target_dir / "decision-brief.json")
    market = load_json(target_dir / "market-index-snapshot.json")
    strategy = decision.get("strategy_context") if isinstance(decision.get("strategy_context"), dict) else {}
    index_rows = []
    for item in market.get("indexes", []):
        if not isinstance(item, dict):
            continue
        badge = status_badge(item.get("status"))
        index_rows.append(
            f"<tr><td><strong>{esc(item.get('name'))}</strong><br><code>{esc(item.get('symbol'))}</code></td>"
            f"<td>{number(item.get('value'))}</td><td class=\"{'negative' if float(item.get('change_percent') or 0) < 0 else 'positive'}\">{decimal(item.get('change_percent'))}%</td>"
            f"<td>{esc(item.get('source'))}</td><td>{badge}</td></tr>"
        )
    evidence = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
    financial = evidence.get("financial") if isinstance(evidence.get("financial"), dict) else {}
    news = evidence.get("news") if isinstance(evidence.get("news"), dict) else {}
    financial_counts = financial.get("cache_counts") if isinstance(financial.get("cache_counts"), dict) else {}
    news_counts = news.get("cache_counts") if isinstance(news.get("cache_counts"), dict) else {}
    lifecycle = summary.get("order_lifecycle") if isinstance(summary.get("order_lifecycle"), dict) else {}
    partial_stages = [item for item in summary.get("stages", []) if isinstance(item, dict) and item.get("status") != "success"]
    stage_rows = "".join(
        f"<tr><td>{esc(item.get('stage'))}</td><td>{status_badge(item.get('status'))}</td><td>{esc(item.get('detail'))}</td></tr>"
        for item in partial_stages
    )
    warnings = []
    normal_evidence_statuses = {None, "", "success", "complete", "supplied"}
    if news.get("status") not in normal_evidence_statuses:
        warnings.append(
            f'<div class="warning"><strong>뉴스 수집 {esc(news.get("status"))}</strong><p>{esc(news.get("display_text") or "일부 뉴스 근거를 사용할 수 없습니다.")}</p></div>'
        )
    if financial.get("status") not in normal_evidence_statuses:
        warnings.append(
            f'<div class="warning"><strong>재무 수집 {esc(financial.get("status"))}</strong><p>{esc(financial.get("display_text") or "일부 재무 근거를 사용할 수 없습니다.")}</p></div>'
        )
    if lifecycle.get("status") not in {None, "", "not_run"}:
        issue_count = number(lifecycle.get("holding_state_issue_count"))
        warning_class = "warning bad-border" if int(lifecycle.get("holding_state_issue_count") or 0) > 0 else "warning"
        warnings.append(
            f'<div class="{warning_class}"><strong>주문 생명주기 사전조회 {esc(lifecycle.get("status"))}</strong>'
            f'<p>현재 미체결 {number(lifecycle.get("active_order_count"))}건 · 같은 날 이전 제출 '
            f'{number(lifecycle.get("previous_submitted_cash_order_count"))}건 · 보유수량 확인 필요 {issue_count}건</p></div>'
        )
    if partial_stages:
        warnings.append(
            '<div class="warning"><strong>부분·실패 stage 존재</strong><p>아래 stage 표의 상태와 설명을 확인하세요. 주문 상태와 수집 상태는 별도로 해석합니다.</p></div>'
        )
    warning_html = "".join(warnings) or '<div class="empty-state">추가 데이터 품질 경고 없음</div>'
    return f"""
    <section class="panel" id="quality">
      <div class="section-head"><div><p class="kicker">MARKET &amp; DATA</p><h2>시장·데이터 품질</h2></div><span class="badge warn">regime {esc(strategy.get('regime'))}</span></div>
      <p>{esc(strategy.get('advisory_reason'))}</p>
      <div class="table-wrap"><table><thead><tr><th>지수</th><th>값</th><th>등락률</th><th>출처</th><th>판정</th></tr></thead><tbody>{''.join(index_rows)}</tbody></table></div>
      <div class="coverage">
        <article><span>재무 coverage</span><strong>{number(financial_counts.get('usable_symbol_count'))} / {number(financial_counts.get('wanted_symbol_count'))}</strong><small>{esc(financial.get('status'))}</small></article>
        <article><span>뉴스 coverage</span><strong>{number(news_counts.get('usable_symbol_count'))} / {number(news_counts.get('wanted_symbol_count'))}</strong><small>{esc(news.get('status'))}</small></article>
        <article><span>가격 전용 종목</span><strong>{number(evidence.get('price_only_symbol_count'))}</strong><small>전체 {number(evidence.get('symbol_count'))}종목</small></article>
      </div>
      <h3>완전 성공이 아닌 stage</h3>
      <div class="table-wrap"><table><thead><tr><th>stage</th><th>상태</th><th>설명</th></tr></thead><tbody>{stage_rows or '<tr><td colspan="3">없음</td></tr>'}</tbody></table></div>
      <div class="warning-list">
        {warning_html}
      </div>
    </section>
    """


def render_financial_details(target_dir: Path) -> str:
    decision = load_json(target_dir / "decision-brief.json")
    symbols = decision.get("symbols") if isinstance(decision.get("symbols"), list) else []
    cards = []
    for index, item in enumerate(symbols, start=1):
        if not isinstance(item, dict):
            continue
        financial = item.get("financial_summary") if isinstance(item.get("financial_summary"), dict) else {}
        financial_items = financial.get("items") if isinstance(financial.get("items"), list) else []
        financial_html = "".join(f"<li>{esc(value)}</li>" for value in financial_items) or "<li>사용 가능한 재무 상세 없음</li>"
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        cards.append(
            f"<article class=\"financial-card\"><div class=\"evidence-title\"><span class=\"index\">{index}</span><div>"
            f"<h3>{esc(item.get('symbol_name'))} <code>{esc(item.get('symbol_id'))}</code></h3>"
            f"<p>현재/최근가 {number(price.get('current_or_last'))}원 · evidence {esc(item.get('evidence_mode'))}</p></div></div>"
            f"<div class=\"financial-body\"><h4>재무·가치 정보</h4><ul>{financial_html}</ul>"
            f"<p class=\"source-note\">cache 상태: {esc(financial.get('cache_status') or '-')} · quality/value 사용 가능: {esc(financial.get('quality_value_usable'))}</p></div></article>"
        )
    cut_off = time_text(load_json(target_dir / "pipeline-summary.json").get("started_at"))
    return f"""
    <section class="panel" id="financial-details">
      <div class="section-head"><div><p class="kicker">FINANCIAL EVIDENCE</p><h2>전체 {len(cards)}종목 재무 상세</h2></div><span class="badge info">{esc(cut_off)} 기준</span></div>
      <p class="section-note">{esc(cut_off)} Analyst 입력에 포함된 compact 재무·가치 항목을 종목별로 표시합니다.</p>
      <div class="financial-list">{''.join(cards)}</div>
    </section>
    """


def render_news_timeline(runs: list[dict[str, Any]]) -> str:
    seen: set[tuple[str, str, str, str, str]] = set()
    blocks = []
    for run in runs:
        summary = run["summary"]
        decision = run["decision"]
        current: set[tuple[str, str, str, str, str]] = set()
        for symbol in decision.get("symbols", []):
            if not isinstance(symbol, dict):
                continue
            for news in symbol.get("news_summary", []):
                if not isinstance(news, dict):
                    continue
                current.add(
                    (
                        str(symbol.get("symbol_id") or ""),
                        str(symbol.get("symbol_name") or ""),
                        str(news.get("article_date") or ""),
                        str(news.get("content") or ""),
                        str(news.get("sentiment") or "unknown"),
                    )
                )
        new_items = sorted(current - seen, key=lambda item: (item[2], item[0], item[3]))
        seen.update(current)
        news_summary = ((summary.get("evidence_summary") or {}).get("news") or {})
        counts = news_summary.get("cache_counts") if isinstance(news_summary.get("cache_counts"), dict) else {}
        articles = []
        for symbol_id, symbol_name, article_date, content, sentiment in new_items:
            articles.append(
                f"<article class=\"news-item\"><div><strong>{esc(symbol_name)}</strong> <code>{esc(symbol_id)}</code>"
                f"<time>{esc(article_date)}</time><span class=\"sentiment {esc(sentiment if sentiment in {'positive', 'negative', 'neutral'} else 'unknown')}\">{esc(sentiment)}</span></div><p>{esc(content)}</p></article>"
            )
        if not articles:
            articles.append('<div class="empty-state">직전 run 이후 새로 관측된 기사 없음</div>')
        blocks.append(
            f"<section class=\"news-run\"><div class=\"news-run-head\"><div><span class=\"news-time\">{esc(time_text(summary.get('started_at')))}</span>"
            f"<strong>coverage {number(counts.get('usable_symbol_count'))}/{number(counts.get('wanted_symbol_count'))}</strong></div>"
            f"<div><span>해당 run 기사 {len(current)}건</span><span class=\"badge {'ok' if new_items else 'info'}\">신규 관측 {len(new_items)}건</span></div></div>"
            f"<div class=\"news-run-body\">{''.join(articles)}</div></section>"
        )
    return f"""
    <section class="panel" id="news-timeline">
      <div class="section-head"><div><p class="kicker">NEWS COLLECTION TIMELINE</p><h2>시간별 뉴스 수집 이력</h2></div><span class="badge info">고유 신규 관측 {len(seen)}건</span></div>
      <p class="section-note">각 run의 수집 시각, 종목 coverage, 해당 run의 기사 수와 이전 run들에서 관측되지 않았던 신규 기사를 구분합니다. 기사 발행시각과 pipeline 수집시각을 함께 볼 수 있습니다.</p>
      <div class="news-timeline">{''.join(blocks)}</div>
    </section>
    """


def list_items(values: Any, empty: str = "없음") -> str:
    if not isinstance(values, list) or not values:
        return f"<li>{esc(empty)}</li>"
    return "".join(f"<li>{esc(value)}</li>" for value in values)


def render_debate_symbol(item: dict[str, Any], side: str) -> str:
    arguments = item.get("arguments") if isinstance(item.get("arguments"), list) else []
    argument_rows = []
    for argument in arguments:
        if not isinstance(argument, dict):
            continue
        refs = argument.get("evidence_refs") if isinstance(argument.get("evidence_refs"), list) else []
        targets = argument.get("targets") if isinstance(argument.get("targets"), list) else []
        argument_rows.append(
            f"<article class=\"argument\"><div><code>{esc(argument.get('argument_id'))}</code> <span class=\"badge info\">{esc(argument.get('kind'))}</span></div>"
            f"<p>{esc(argument.get('statement'))}</p><small>근거: {esc(' · '.join(str(value) for value in refs) or '-')}</small>"
            f"<small>대상: {esc(' · '.join(str(value) for value in targets) or '-')}</small></article>"
        )
    recommended_action = str(item.get("recommended_action") or "미제공")
    target_quantity = (
        f"{number(item.get('target_holding_quantity'))}주"
        if item.get("target_holding_quantity") is not None
        else "미제공"
    )
    return f"""
    <article class="debate-symbol {esc(side)}">
      <div class="card-title"><div><h4>{esc(item.get('symbol_name'))} <code>{esc(item.get('symbol_id'))}</code></h4><p>{esc(SIDE_LABELS.get(side, side))} 코멘트 전체</p></div></div>
      <div class="arguments">{''.join(argument_rows)}</div>
      <div class="debate-meta"><div><strong>양보한 근거</strong><ul>{list_items(item.get('concessions'))}</ul></div><div><strong>미해결 충돌</strong><ul>{list_items(item.get('unresolved_conflicts'))}</ul></div></div>
      <div class="position"><strong>단계 결론</strong><p>{esc(item.get('final_position'))}</p>
        <div class="final-numbers"><span>권고 행동 {esc(recommended_action)}</span><span>목표 보유 {esc(target_quantity)}</span></div>
      </div>
    </article>
    """


def build_html(runs_root: Path, target_run: str) -> str:
    target_dir = runs_root / target_run
    summary = load_json(target_dir / "pipeline-summary.json")
    if not summary:
        raise ValueError(f"pipeline-summary.json is missing or invalid: {target_dir}")
    target_started_at = str(summary.get("started_at") or "")
    if not target_started_at:
        raise ValueError(f"pipeline summary started_at is missing: {target_dir}")
    runs = find_runs(runs_root, target_started_at)
    if not runs:
        raise ValueError(f"no daily-trading runs found through {target_started_at}")
    fills, fill_status, fill_scope = cumulative_today_fills(runs)
    trade_html, submitted_orders = render_trade_ledger(runs, fills, fill_status, fill_scope)
    overview = render_header(summary, len(runs), fills, submitted_orders) + render_combined_chart(runs)
    trades = trade_html + render_time_symbol_inspector(runs, fills)
    evidence = render_news_timeline(runs) + render_financial_details(target_dir)
    operations = render_run_timeline(runs) + render_holdings(target_dir, summary) + render_market_and_quality(target_dir, summary)

    tab_buttons = [
        '<button type="button" class="tab-button active" data-tab="overview" role="tab" aria-selected="true">개요</button>',
        '<button type="button" class="tab-button" data-tab="trading" role="tab" aria-selected="false">거래·종목판단</button>',
        '<button type="button" class="tab-button" data-tab="evidence" role="tab" aria-selected="false">재무·뉴스</button>',
        '<button type="button" class="tab-button" data-tab="operations" role="tab" aria-selected="false">실행 기록</button>',
    ]
    tab_pages = [
        f'<section class="tab-page active" id="overview" role="tabpanel">{overview}</section>',
        f'<section class="tab-page" id="trading" role="tabpanel">{trades}</section>',
        f'<section class="tab-page" id="evidence" role="tabpanel">{evidence}</section>',
        f'<section class="tab-page" id="operations" role="tabpanel">{operations}</section>',
    ]

    body = f"""
    <div class="app-header">
      <div class="brand"><span class="brand-mark">D</span><div><strong>Danta Report</strong><small>cumulative trading intelligence</small></div></div>
      <div class="report-date"><span>REPORT CUT-OFF</span><strong>{esc(target_started_at[:16].replace('T', ' '))} KST</strong></div>
    </div>
    <nav class="tab-bar" role="tablist" aria-label="리포트 페이지">{''.join(tab_buttons)}</nav>
    {''.join(tab_pages)}
    """
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>당일 누적 daily-trading 리포트 · {esc(target_started_at)}</title>
  <style>
    :root {{ color-scheme:light; --bg:#f1f4f9; --surface:rgba(255,255,255,.96); --subtle:#f7f9fc; --text:#172033; --muted:#68758b; --line:#dce4ef; --accent:#4e5ce8; --accent-2:#0b8d81; --accent-bg:#eef0ff; --ok:#087a55; --ok-bg:#e8f7f1; --warn:#9a5b00; --warn-bg:#fff7df; --bad:#bd2c3a; --bad-bg:#fff0f1; --bull:#176b4d; --bear:#a33342; --shadow:0 16px 45px rgba(31,47,77,.09); }}
    * {{ box-sizing:border-box; }}
    html {{ scroll-behavior:smooth; }}
    body {{ margin:0; background:radial-gradient(circle at 4% 2%,rgba(78,92,232,.10),transparent 30%),radial-gradient(circle at 96% 4%,rgba(11,141,129,.10),transparent 26%),var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans KR",sans-serif; line-height:1.55; }}
    button {{ font:inherit; }}
    main {{ width:min(1240px,calc(100% - 28px)); margin:18px auto 64px; }}
    h1,h2,h3,h4,p {{ margin-top:0; }} h1 {{ margin-bottom:10px; font-size:clamp(27px,4vw,42px); letter-spacing:-.045em; }} h2 {{ margin-bottom:5px; font-size:23px; letter-spacing:-.03em; }} h3 {{ margin:24px 0 10px; font-size:17px; }} h4 {{ margin-bottom:4px; font-size:16px; }}
    code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; overflow-wrap:anywhere; }}
    .app-header {{ display:flex; align-items:center; justify-content:space-between; gap:14px; padding:8px 4px 15px; }} .brand {{ display:flex; align-items:center; gap:10px; }} .brand-mark {{ display:grid; width:38px; height:38px; place-items:center; border-radius:12px; background:linear-gradient(145deg,var(--accent),var(--accent-2)); color:#fff; font-weight:900; box-shadow:0 8px 20px rgba(78,92,232,.26); }} .brand strong,.brand small,.report-date span,.report-date strong {{ display:block; }} .brand small,.report-date span {{ color:var(--muted); font-size:11px; }} .report-date {{ text-align:right; }}
    .tab-bar {{ position:sticky; z-index:20; top:8px; display:flex; gap:7px; padding:7px; margin-bottom:15px; overflow-x:auto; border:1px solid rgba(220,228,239,.9); border-radius:16px; background:rgba(255,255,255,.82); box-shadow:0 10px 30px rgba(31,47,77,.08); backdrop-filter:blur(16px); }} .tab-button {{ flex:0 0 auto; padding:10px 14px; border:0; border-radius:11px; background:transparent; color:var(--muted); cursor:pointer; font-size:13px; font-weight:800; transition:.18s ease; }} .tab-button:hover {{ background:var(--subtle); color:var(--text); }} .tab-button.active {{ background:linear-gradient(135deg,var(--accent),#6d55de); color:#fff; box-shadow:0 7px 18px rgba(78,92,232,.25); }}
    .tab-page {{ display:none; animation:page-in .22s ease; }} .tab-page.active {{ display:block; }} @keyframes page-in {{ from {{ opacity:.3; transform:translateY(5px); }} to {{ opacity:1; transform:none; }} }}
    .hero {{ position:relative; overflow:hidden; padding:36px; border-radius:26px; background:linear-gradient(135deg,#15234f 0%,#3f4fc4 57%,#087d76 100%); color:#fff; box-shadow:0 24px 60px rgba(30,47,90,.18); }} .hero::after {{ content:""; position:absolute; width:260px; height:260px; right:-80px; top:-120px; border-radius:50%; background:rgba(255,255,255,.10); }}
    .hero p {{ max-width:850px; color:#e3eaff; }} .eyebrow,.kicker {{ margin-bottom:9px; color:#94a3d8; font-size:12px; font-weight:800; letter-spacing:.1em; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:8px; margin:18px 0 10px; }} .chip {{ padding:6px 10px; border:1px solid rgba(255,255,255,.24); border-radius:999px; background:rgba(255,255,255,.1); font-size:13px; }} .chip.success {{ color:#b7f7dc; }} .run-id {{ display:flex; align-items:baseline; flex-wrap:wrap; gap:7px; color:#cbd7ff; }} .run-id span {{ font-size:12px; font-weight:800; }} .run-id small {{ width:100%; color:#aebce9; font-size:11px; }}
    .metrics,.coverage {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0; }}
    .metrics article,.coverage article {{ padding:17px; border:1px solid var(--line); border-radius:16px; background:var(--surface); box-shadow:var(--shadow); }}
    .metrics span,.coverage span,.metrics small,.coverage small {{ display:block; color:var(--muted); font-size:12px; }} .metrics strong,.coverage strong {{ display:block; margin:4px 0; font-size:20px; letter-spacing:-.025em; }}
    .panel,.decision-hero {{ padding:25px; margin-top:16px; border:1px solid rgba(220,228,239,.95); border-radius:21px; background:var(--surface); box-shadow:var(--shadow); }}
    .section-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:16px; }} .section-note {{ color:var(--muted); }}
    .badge {{ display:inline-flex; padding:4px 8px; border-radius:999px; font-size:11px; font-weight:800; white-space:nowrap; }} .badge.ok {{ color:var(--ok); background:var(--ok-bg); }} .badge.warn {{ color:var(--warn); background:var(--warn-bg); }} .badge.bad {{ color:var(--bad); background:var(--bad-bg); }} .badge.info {{ color:var(--accent); background:var(--accent-bg); }} .badge.muted {{ color:var(--muted); background:#edf1f6; }}
    .notice {{ margin:10px 0 18px; padding:13px 15px; border-left:4px solid var(--accent); border-radius:10px; background:var(--accent-bg); font-size:13px; }}
    .table-wrap {{ width:100%; overflow-x:auto; border:1px solid var(--line); border-radius:12px; }} table {{ width:100%; border-collapse:collapse; min-width:760px; }} th,td {{ padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:13px; }} th {{ position:sticky; top:0; background:#eef3f9; color:#475569; font-size:12px; }} tr:last-child td {{ border-bottom:0; }}
    .positive {{ color:var(--ok); }} .negative {{ color:var(--bad); }}
    .warning-list {{ display:grid; gap:9px; margin-top:18px; }} .warning {{ padding:14px; border-left:4px solid var(--warn); border-radius:10px; background:var(--warn-bg); }} .warning.bad-border {{ border-left-color:var(--bad); background:var(--bad-bg); }} .warning p {{ margin:4px 0 0; font-size:13px; }}
    .combined-chart-card {{ padding:24px; margin-top:16px; border:1px solid var(--line); border-radius:22px; background:var(--surface); box-shadow:var(--shadow); }} .chart-legend {{ display:flex; flex-wrap:wrap; gap:14px; margin:13px 0 4px; color:var(--muted); font-size:12px; }} .chart-legend span {{ display:flex; align-items:center; gap:6px; }} .chart-legend i {{ width:22px; height:4px; border-radius:999px; background:var(--legend); }} .interactive-chart {{ position:relative; }} .interactive-line-chart {{ display:block; width:100%; height:auto; overflow:visible; }} .series-line {{ fill:none; stroke-width:4; stroke-linecap:round; stroke-linejoin:round; }} .total-line {{ stroke:#4f6df5; }} .pnl-line {{ stroke:#e14c68; stroke-dasharray:11 7; }} .kospi-line {{ stroke:#0b9a86; }} .chart-cursor {{ stroke:#617089; stroke-width:1.5; stroke-dasharray:5 5; opacity:0; }} .chart-marker {{ stroke:#fff; stroke-width:3; opacity:0; }} .total-marker {{ fill:#4f6df5; }} .pnl-marker {{ fill:#e14c68; }} .kospi-marker {{ fill:#0b9a86; }} .chart-hit-area {{ fill:transparent; cursor:crosshair; pointer-events:all; touch-action:none; }} .chart-tooltip {{ position:absolute; z-index:5; min-width:190px; padding:11px 13px; border:1px solid rgba(220,228,239,.9); border-radius:12px; background:rgba(20,29,52,.94); color:#fff; box-shadow:0 12px 32px rgba(18,27,50,.25); opacity:0; pointer-events:none; transform:translate(-50%,-112%); transition:opacity .12s ease; font-size:12px; }} .chart-tooltip.visible {{ opacity:1; }} .chart-tooltip strong,.chart-tooltip span {{ display:block; }} .chart-tooltip strong {{ margin-bottom:5px; }} .chart-tooltip span {{ color:#d8e0f3; }} .chart-scrubber {{ padding:8px 5px 0; }} .chart-scrubber-label,.chart-scrubber-ends {{ display:flex; align-items:center; justify-content:space-between; gap:12px; }} .chart-scrubber-label {{ margin-bottom:3px; color:var(--muted); font-size:12px; font-weight:800; }} .chart-scrubber-time {{ color:var(--accent); font-weight:900; }} .chart-range-slider {{ width:100%; accent-color:var(--accent); cursor:ew-resize; touch-action:pan-x; }} .chart-scrubber-ends {{ color:var(--muted); font-size:10px; }} .series-ranges {{ display:flex; justify-content:flex-end; flex-wrap:wrap; gap:13px; color:var(--muted); font-size:11px; }}
    .portfolio-chart-layout {{ display:grid; grid-template-columns:minmax(300px,.85fr) minmax(320px,1.15fr); align-items:center; gap:28px; padding:20px; margin-bottom:12px; border:1px solid var(--line); border-radius:16px; background:var(--subtle); }} .portfolio-pie {{ position:relative; width:min(100%,430px); margin:auto; }} .portfolio-pie-svg {{ display:block; width:100%; height:auto; filter:drop-shadow(0 12px 22px rgba(24,36,64,.12)); }} .pie-slice {{ stroke:#fff; stroke-width:2; cursor:pointer; outline:none; transition:opacity .15s ease,stroke-width .15s ease; }} .pie-slice:hover,.pie-slice.active,.pie-slice:focus {{ opacity:.8; stroke-width:5; }} .pie-tooltip {{ position:absolute; z-index:6; min-width:205px; padding:11px 13px; border-radius:12px; background:rgba(20,29,52,.95); color:#fff; box-shadow:0 12px 32px rgba(18,27,50,.25); opacity:0; pointer-events:none; transform:translate(-50%,-112%); transition:opacity .1s ease; font-size:12px; }} .pie-tooltip.visible {{ opacity:1; }} .pie-tooltip strong,.pie-tooltip span {{ display:block; }} .pie-tooltip strong {{ margin-bottom:4px; }} .pie-tooltip span {{ color:#d8e0f3; }} .sector-legend {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; }} .sector-legend>div:first-child {{ grid-column:1/-1; }} .sector-legend h3 {{ margin:0; }} .sector-legend>div:first-child p:last-child {{ margin:3px 0 5px; color:var(--muted); font-size:12px; }} .sector-legend-item {{ display:flex; align-items:center; gap:9px; padding:10px; border:1px solid var(--line); border-radius:11px; background:#fff; }} .sector-legend-item i {{ width:13px; height:34px; flex:0 0 13px; border-radius:999px; background:var(--sector-color); }} .sector-legend-item strong,.sector-legend-item small {{ display:block; }} .sector-legend-item small {{ color:var(--muted); font-size:10px; }} .holdings-table {{ margin-top:14px; }}
    .chart-grid-wrap {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin-top:16px; }} .chart-card {{ padding:20px; overflow:hidden; border:1px solid var(--line); border-radius:20px; background:var(--surface); box-shadow:var(--shadow); }} .chart-head {{ display:flex; justify-content:space-between; gap:12px; }} .chart-head h3 {{ margin:0; font-size:19px; }} .chart-head p {{ margin:5px 0 0; color:var(--muted); font-size:12px; }} .chart-stat {{ min-width:100px; text-align:right; }} .chart-stat span,.chart-stat strong {{ display:block; }} .chart-stat span {{ color:var(--muted); font-size:11px; }} .line-chart {{ display:block; width:100%; height:auto; margin-top:5px; overflow:visible; }} .chart-grid {{ stroke:#e5eaf2; stroke-width:1; }} .chart-y,.chart-x {{ fill:#738096; font-size:15px; }} .chart-point {{ stroke:#fff; stroke-width:3; }} .chart-range {{ display:flex; justify-content:flex-end; flex-wrap:wrap; gap:14px; color:var(--muted); font-size:11px; }} .chart-range strong {{ color:var(--text); }}
    .financial-list {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }} .financial-card {{ padding:17px; border:1px solid var(--line); border-radius:15px; background:var(--subtle); }} .financial-body {{ padding:13px; border-radius:11px; background:#fff; }} .financial-body ul {{ margin:0; padding-left:20px; }} .evidence-title {{ display:flex; gap:10px; align-items:flex-start; margin-bottom:12px; }} .evidence-title h3 {{ margin:0; }} .evidence-title p {{ margin:4px 0 0; color:var(--muted); font-size:12px; }} .source-note {{ margin:12px 0 0; color:var(--muted); font-size:11px; }}
    .news-timeline {{ position:relative; display:grid; gap:12px; padding-left:22px; }} .news-timeline::before {{ content:""; position:absolute; left:7px; top:9px; bottom:9px; width:2px; background:linear-gradient(var(--accent),var(--accent-2)); }} .news-run {{ position:relative; padding:16px; border:1px solid var(--line); border-radius:15px; background:var(--subtle); }} .news-run::before {{ content:""; position:absolute; left:-22px; top:22px; width:11px; height:11px; border:3px solid var(--bg); border-radius:50%; background:var(--accent); }} .news-run-head {{ display:flex; justify-content:space-between; gap:10px; margin-bottom:9px; }} .news-run-head>div {{ display:flex; align-items:center; flex-wrap:wrap; gap:8px; }} .news-time {{ display:grid; width:50px; height:28px; place-items:center; border-radius:8px; background:var(--accent); color:#fff; font-size:12px; font-weight:900; }} .news-run-head span:not(.news-time):not(.badge) {{ color:var(--muted); font-size:11px; }} .news-run-body {{ padding:4px 12px; border-radius:11px; background:#fff; }} .news-item {{ padding:10px 0; border-bottom:1px solid var(--line); }} .news-item:last-child {{ border-bottom:0; }} .news-item time {{ margin-left:8px; color:var(--muted); font-size:11px; }} .news-item p {{ margin:5px 0 0; }} .sentiment {{ float:right; padding:3px 7px; border-radius:999px; font-size:10px; font-weight:900; }} .sentiment.positive {{ color:#087a55; background:#dff5eb; }} .sentiment.negative {{ color:#b42335; background:#ffe8eb; }} .sentiment.neutral {{ color:#536176; background:#e9eef5; }} .sentiment.unknown {{ color:#815400; background:#fff3ce; }} .empty-state {{ padding:12px; border-radius:9px; background:var(--subtle); color:var(--muted); font-size:12px; }}
    .time-selector {{ display:flex; gap:9px; padding:3px 2px 12px; margin:14px 0 16px; overflow-x:auto; }} .time-button {{ display:flex; flex:0 0 146px; padding:12px; border:1px solid var(--line); border-radius:13px; background:var(--subtle); color:var(--text); cursor:pointer; flex-direction:column; text-align:left; transition:.16s ease; }} .time-button:hover {{ border-color:#aab4ff; transform:translateY(-1px); }} .time-button.active {{ border-color:var(--accent); background:linear-gradient(145deg,var(--accent-bg),#eefaf7); box-shadow:0 8px 22px rgba(78,92,232,.12); }} .time-button strong {{ font-size:18px; }} .time-button span,.time-button small {{ color:var(--muted); font-size:10px; }} .time-analysis-panel {{ display:none; }} .time-analysis-panel.active {{ display:block; animation:page-in .18s ease; }} .time-panel-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; padding-top:4px; }} .time-panel-head p {{ color:var(--muted); }} .run-activity {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:9px; margin:12px 0 19px; }} .activity-card {{ padding:13px; border:1px solid var(--line); border-radius:12px; background:#fff; }} .activity-card.order {{ border-left:4px solid var(--accent); }} .activity-card.filled {{ border-left:4px solid var(--ok); background:linear-gradient(145deg,#fff,var(--ok-bg)); }} .activity-card.fill {{ border-left:4px solid var(--accent-2); }} .activity-card span,.activity-card strong,.activity-card small {{ display:block; }} .activity-card span,.activity-card small {{ color:var(--muted); font-size:11px; }}
    .trade-symbol-selector {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:9px; margin-bottom:16px; }} .trade-symbol-button {{ display:grid; min-width:0; padding:12px; border:1px solid var(--line); border-radius:13px; background:var(--subtle); color:var(--text); text-align:left; cursor:pointer; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:10px; transition:.16s ease; }} .trade-symbol-button.score-low {{ border-color:#f3bbc2; background:var(--bad-bg); }} .trade-symbol-button.score-high {{ border-color:#a9ddcc; background:var(--ok-bg); }} .trade-symbol-button:hover {{ border-color:#aab4ff; transform:translateY(-1px); }} .trade-symbol-button.active {{ border-color:var(--accent); box-shadow:0 8px 22px rgba(78,92,232,.12); }} .trade-symbol-button.score-low.active {{ background:linear-gradient(145deg,var(--bad-bg),#fff4f5); }} .trade-symbol-button.score-high.active {{ background:linear-gradient(145deg,var(--ok-bg),#eefaf7); }} .symbol-button-left {{ display:flex; min-width:0; flex-direction:column; }} .symbol-button-status {{ display:flex; min-height:19px; flex-wrap:wrap; gap:4px; }} .symbol-button-name {{ display:-webkit-box; min-width:0; min-height:2.6em; margin-top:4px; overflow:hidden; color:var(--text); font-size:13px; line-height:1.3; text-overflow:ellipsis; white-space:normal; -webkit-box-orient:vertical; -webkit-line-clamp:2; }} .symbol-button-right {{ display:flex; flex:0 0 auto; align-items:flex-end; flex-direction:column; gap:3px; text-align:right; }} .symbol-button-right code {{ color:var(--muted); font-size:10px; }} .symbol-score {{ display:block; padding:0; background:transparent; color:var(--text); font-size:13px; font-weight:800; line-height:1.2; }} .trade-symbol-button.active .symbol-score {{ background:transparent; color:var(--accent); }} .mini-badge {{ padding:2px 5px; border-radius:999px; font-size:9px; }} .mini-badge.judge {{ color:var(--ok); background:var(--ok-bg); }} .mini-badge.analyst {{ color:var(--muted); background:#e9eef5; }} .mini-badge.trade {{ color:var(--accent); background:var(--accent-bg); }} .symbol-analysis-panel {{ display:none; padding:20px; border:1px solid var(--line); border-radius:16px; background:linear-gradient(145deg,#fff,var(--subtle)); }} .symbol-analysis-panel.active {{ display:block; animation:page-in .18s ease; }} .symbol-focus-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }} .symbol-focus-head h2 {{ margin-bottom:5px; }} .symbol-focus-head p {{ color:var(--muted); }} .focus-badges {{ display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; }} .inline-analysis,.inline-judge {{ margin-top:20px; }} .compact-phase {{ margin-top:18px; padding-top:15px; }} .final-card.full {{ margin-top:12px; }}
    .decision-hero {{ background:linear-gradient(145deg,#fff,#f4f6ff); }} .decision-hero>div:first-child p:last-child {{ color:var(--muted); }} .decision-meta {{ display:flex; flex-wrap:wrap; gap:8px; }} .decision-meta>span {{ padding:7px 9px; border:1px solid var(--line); border-radius:9px; background:#fff; font-size:12px; }} .decision-orders {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:14px; }} .decision-orders article {{ padding:13px; border-radius:12px; background:linear-gradient(135deg,var(--accent-bg),#eefaf7); }} .decision-orders span,.decision-orders strong,.decision-orders small {{ display:block; }} .decision-orders span,.decision-orders small {{ color:var(--muted); font-size:11px; }}
    .analyst-list {{ display:grid; gap:14px; }} .analyst-card {{ padding:17px; border:1px solid var(--line); border-radius:14px; background:var(--subtle); }} .card-title {{ display:flex; align-items:flex-start; gap:10px; margin-bottom:12px; }} .card-title h3,.card-title h4 {{ margin:0; }} .card-title p {{ margin:3px 0 0; color:var(--muted); font-size:13px; }} .index {{ display:grid; flex:0 0 30px; height:30px; place-items:center; border-radius:9px; background:var(--accent-bg); color:var(--accent); font-weight:900; }}
    .phase,.final-section {{ margin-top:26px; padding-top:22px; border-top:3px solid var(--line); }} .phase-title {{ display:flex; align-items:baseline; gap:12px; margin-bottom:12px; }} .phase-title span {{ font-size:22px; font-weight:900; }} .phase-title small {{ color:var(--muted); }}
    .debate-side {{ padding:17px; margin:12px 0; border:1px solid var(--line); border-radius:16px; background:var(--subtle); }} .debate-side>h3 {{ margin-top:0; }} .bull-text {{ color:var(--bull); }} .bear-text {{ color:var(--bear); }}
    .debate-symbol {{ padding:16px; margin-top:12px; border:1px solid var(--line); border-left:5px solid var(--bull); border-radius:12px; background:#fff; }} .debate-symbol.bear {{ border-left-color:var(--bear); }}
    .arguments {{ display:grid; gap:9px; }} .argument {{ padding:12px; border:1px solid var(--line); border-radius:10px; background:var(--subtle); }} .argument p {{ margin:7px 0; }} .argument small {{ display:block; color:var(--muted); overflow-wrap:anywhere; }}
    .debate-meta {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:10px; }} .debate-meta>div,.position {{ padding:12px; border-radius:10px; background:var(--accent-bg); }} .debate-meta ul {{ margin:7px 0 0; padding-left:20px; }} .position {{ margin-top:10px; }} .position p {{ margin:5px 0 0; }}
    .final-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }} .final-card {{ padding:15px; border:1px solid var(--line); border-radius:12px; background:var(--subtle); }} .final-card h3 {{ display:inline; }} .final-card p {{ margin:8px 0 0; }} .final-numbers {{ display:flex; flex-wrap:wrap; gap:7px; margin-top:10px; }} .final-numbers span {{ padding:5px 8px; border-radius:8px; background:var(--accent-bg); font-size:12px; font-weight:700; }}
    footer {{ padding:24px 4px 0; color:var(--muted); font-size:12px; text-align:center; }}
    @media(max-width:1000px) {{ .trade-symbol-selector {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} }}
    @media(max-width:900px) {{ .chart-grid-wrap,.financial-list,.portfolio-chart-layout {{ grid-template-columns:1fr; }} .trade-symbol-selector {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} .run-activity {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media(max-width:800px) {{ main {{ width:min(100% - 16px,720px); margin-top:8px; }} .app-header {{ padding-inline:4px; }} .hero,.panel,.decision-hero,.combined-chart-card {{ padding:19px; border-radius:17px; }} .metrics,.coverage,.trade-symbol-selector {{ grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }} .section-head,.symbol-focus-head,.time-panel-head {{ display:block; }} .section-head>.badge,.symbol-focus-head>.badge,.time-panel-head>.badge {{ margin-top:8px; }} .focus-badges {{ justify-content:flex-start; margin-top:8px; }} .debate-meta,.final-grid,.decision-orders {{ grid-template-columns:1fr; }} .news-run-head {{ display:block; }} .news-run-head>div+div {{ margin-top:7px; }} }}
    @media(max-width:480px) {{ .metrics strong,.coverage strong {{ font-size:16px; }} .phase-title {{ display:block; }} .phase-title small {{ display:block; margin-top:4px; }} .trade-symbol-selector,.run-activity,.sector-legend {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .portfolio-chart-layout {{ padding:12px; }} }}
    @media print {{ body {{ background:#fff; }} main {{ width:100%; margin:0; }} .app-header,.tab-bar {{ display:none; }} .tab-page {{ display:block !important; }} .hero,.panel,.metrics article,.chart-card {{ box-shadow:none; break-inside:avoid; }} .table-wrap {{ overflow:visible; }} }}
  </style>
</head>
<body><main>{body}<footer>daily-trading 실행 artifact에서 생성한 단일 파일 리포트입니다. raw prompt, 세션 ID, 절대 경로와 비밀값은 포함하지 않습니다.</footer></main>
<script>
  (() => {{
    const setTooltipRows = (tooltip, rows) => {{
      tooltip.replaceChildren();
      rows.forEach((value, index) => {{
        const row = document.createElement(index === 0 ? 'strong' : 'span');
        row.textContent = value;
        tooltip.appendChild(row);
      }});
    }};
    const buttons = [...document.querySelectorAll('.tab-button')];
    const pages = [...document.querySelectorAll('.tab-page')];
    const activate = (id, updateHash = true) => {{
      const target = pages.find((page) => page.id === id) || pages[0];
      pages.forEach((page) => page.classList.toggle('active', page === target));
      buttons.forEach((button) => {{
        const active = button.dataset.tab === target.id;
        button.classList.toggle('active', active);
        button.setAttribute('aria-selected', String(active));
      }});
      if (updateHash) history.replaceState(null, '', `#${{target.id}}`);
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }};
    buttons.forEach((button) => button.addEventListener('click', () => activate(button.dataset.tab)));

    document.querySelectorAll('.time-selector').forEach((selector) => {{
      const timeButtons = [...selector.querySelectorAll('.time-button')];
      const container = selector.closest('#trade-symbol-analysis');
      const timePanels = [...container.querySelectorAll('.time-analysis-panel')];
      const selectTime = (timeId) => {{
        timeButtons.forEach((button) => {{
          const active = button.dataset.timeTarget === timeId;
          button.classList.toggle('active', active);
          button.setAttribute('aria-pressed', String(active));
        }});
        timePanels.forEach((panel) => panel.classList.toggle('active', panel.dataset.timePanel === timeId));
      }};
      timeButtons.forEach((button) => button.addEventListener('click', () => selectTime(button.dataset.timeTarget)));
      const initialTime = timeButtons.find((button) => button.classList.contains('active')) || timeButtons[0];
      if (initialTime) selectTime(initialTime.dataset.timeTarget);
    }});

    document.querySelectorAll('.trade-symbol-selector').forEach((selector) => {{
      const symbolButtons = [...selector.querySelectorAll('.trade-symbol-button')];
      const container = selector.closest('.time-analysis-panel');
      const symbolPanels = [...container.querySelectorAll('.symbol-analysis-panel')];
      const selectSymbol = (symbolId) => {{
        symbolButtons.forEach((button) => {{
          const active = button.dataset.symbolTarget === symbolId;
          button.classList.toggle('active', active);
          button.setAttribute('aria-pressed', String(active));
        }});
        symbolPanels.forEach((panel) => panel.classList.toggle('active', panel.dataset.symbolPanel === symbolId));
      }};
      symbolButtons.forEach((button) => button.addEventListener('click', () => selectSymbol(button.dataset.symbolTarget)));
      const initialSymbol = symbolButtons.find((button) => button.classList.contains('active')) || symbolButtons[0];
      if (initialSymbol) selectSymbol(initialSymbol.dataset.symbolTarget);
    }});

    document.querySelectorAll('.combined-chart-card').forEach((card) => {{
      const points = JSON.parse(card.dataset.chartPoints || '[]');
      const svg = card.querySelector('.interactive-line-chart');
      const hitArea = card.querySelector('.chart-hit-area');
      const tooltip = card.querySelector('.chart-tooltip');
      const cursor = card.querySelector('.chart-cursor');
      const slider = card.querySelector('.chart-range-slider');
      const sliderValue = card.querySelector('.chart-scrubber-time');
      const markers = {{
        total: card.querySelector('.total-marker'),
        pnl: card.querySelector('.pnl-marker'),
        kospi: card.querySelector('.kospi-marker'),
      }};
      let dragging = false;

      const hidePoint = () => {{
        if (dragging) return;
        cursor.style.opacity = '0';
        Object.values(markers).filter(Boolean).forEach((marker) => marker.style.opacity = '0');
        tooltip.classList.remove('visible');
      }};
      const showPointAtIndex = (requestedIndex, event = null) => {{
        const matrix = svg.getScreenCTM();
        if (!points.length || !matrix) return;
        const index = Math.max(0, Math.min(points.length - 1, requestedIndex));
        const point = points[index];
        if (slider) {{
          slider.value = String(index);
          slider.setAttribute('aria-valuetext', point.time);
        }}
        if (sliderValue) sliderValue.textContent = point.time;

        cursor.setAttribute('x1', point.x);
        cursor.setAttribute('x2', point.x);
        cursor.style.opacity = '1';
        for (const key of Object.keys(markers)) {{
          if (!markers[key]) continue;
          if (point[key + 'Y'] === null) {{
            markers[key].style.opacity = '0';
            continue;
          }}
          markers[key].setAttribute('cx', point.x);
          markers[key].setAttribute('cy', point[key + 'Y']);
          markers[key].style.opacity = '1';
        }}
        const kospiText = point.kospi === null
          ? 'KOSPI 조회 실패'
          : 'KOSPI ' + Number(point.kospi).toLocaleString('ko-KR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) + ' (' + (Number(point.kospiChangePercent) >= 0 ? '+' : '') + Number(point.kospiChangePercent).toLocaleString('ko-KR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) + '%)';
        setTooltipRows(tooltip, [
          point.time,
          '총평가 ' + Number(point.total).toLocaleString('ko-KR') + '원',
          '평가손익 ' + Number(point.pnl).toLocaleString('ko-KR') + '원',
          kospiText,
          'regime ' + point.regimeLabel + ' (' + point.regime + ')',
        ]);
        const chartRect = card.querySelector('.interactive-chart').getBoundingClientRect();
        let clientX = event ? event.clientX : null;
        let clientY = event ? event.clientY : null;
        if (!event) {{
          const selectedPoint = svg.createSVGPoint();
          selectedPoint.x = point.x;
          selectedPoint.y = point.totalY;
          const screenPoint = selectedPoint.matrixTransform(matrix);
          clientX = screenPoint.x;
          clientY = screenPoint.y;
        }}
        const tooltipX = Math.max(105, Math.min(chartRect.width - 105, clientX - chartRect.left));
        const tooltipY = Math.max(90, Math.min(chartRect.height - 10, clientY - chartRect.top));
        tooltip.style.left = tooltipX + 'px';
        tooltip.style.top = tooltipY + 'px';
        tooltip.classList.add('visible');
      }};
      const showPoint = (event) => {{
        if (!points.length || !svg.getScreenCTM()) return;
        const svgPoint = svg.createSVGPoint();
        svgPoint.x = event.clientX;
        svgPoint.y = event.clientY;
        const local = svgPoint.matrixTransform(svg.getScreenCTM().inverse());
        const left = Number(hitArea.dataset.left);
        const width = Number(hitArea.dataset.width);
        const ratio = Math.max(0, Math.min(1, (local.x - left) / width));
        const index = Math.round(ratio * (points.length - 1));
        showPointAtIndex(index, event);
      }};

      hitArea.addEventListener('pointerdown', (event) => {{
        dragging = true;
        hitArea.setPointerCapture(event.pointerId);
        showPoint(event);
      }});
      hitArea.addEventListener('pointermove', showPoint);
      hitArea.addEventListener('pointerup', (event) => {{
        showPoint(event);
        dragging = false;
        if (hitArea.hasPointerCapture(event.pointerId)) hitArea.releasePointerCapture(event.pointerId);
      }});
      hitArea.addEventListener('pointercancel', () => {{ dragging = false; hidePoint(); }});
      hitArea.addEventListener('pointerleave', hidePoint);
      if (slider) slider.addEventListener('input', () => showPointAtIndex(Number(slider.value)));
    }});

    document.querySelectorAll('.portfolio-pie').forEach((chart) => {{
      const slices = [...chart.querySelectorAll('.pie-slice')];
      const tooltip = chart.querySelector('.pie-tooltip');
      const hideSlice = () => {{
        slices.forEach((slice) => slice.classList.remove('active'));
        tooltip.classList.remove('visible');
      }};
      const showSlice = (slice, event) => {{
        slices.forEach((item) => item.classList.toggle('active', item === slice));
        setTooltipRows(tooltip, [
          slice.dataset.symbolName + ' ' + slice.dataset.symbolId,
          '업종 ' + slice.dataset.industry,
          '평가액 ' + Number(slice.dataset.valuation).toLocaleString('ko-KR') + '원 · ' + Number(slice.dataset.weight).toLocaleString('ko-KR', {{ minimumFractionDigits: 2, maximumFractionDigits: 2 }}) + '%',
          '보유 ' + slice.dataset.quantity + '주 · 평가손익 ' + slice.dataset.pnl + '원 (' + slice.dataset.pnlRate + '%)',
        ]);
        const rect = chart.getBoundingClientRect();
        const hasPointer = event && Number.isFinite(event.clientX) && Number.isFinite(event.clientY);
        const x = hasPointer ? event.clientX - rect.left : rect.width / 2;
        const y = hasPointer ? event.clientY - rect.top : rect.height / 2;
        tooltip.style.left = Math.max(112, Math.min(rect.width - 112, x)) + 'px';
        tooltip.style.top = Math.max(90, Math.min(rect.height - 8, y)) + 'px';
        tooltip.classList.add('visible');
      }};
      slices.forEach((slice) => {{
        slice.addEventListener('pointerenter', (event) => showSlice(slice, event));
        slice.addEventListener('pointermove', (event) => showSlice(slice, event));
        slice.addEventListener('pointerdown', (event) => showSlice(slice, event));
        slice.addEventListener('pointerleave', hideSlice);
        slice.addEventListener('focus', () => showSlice(slice));
        slice.addEventListener('blur', hideSlice);
      }});
    }});

    activate(location.hash.slice(1) || 'overview', false);
  }})();
</script></body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--target-run")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        codex_exec_root = Path(__file__).resolve().parents[4]
        codex_exec_root_text = str(codex_exec_root)
        if codex_exec_root_text not in sys.path:
            sys.path.insert(0, codex_exec_root_text)
        from service.pipelines.daily_trading.tests.test_render_html_report import self_test

        return self_test()
    if not args.runs_root or not args.target_run or not args.output:
        parser.error("--runs-root, --target-run, and --output are required unless --self-test is used")
    raw_rendered = build_html(args.runs_root, args.target_run)
    rendered = "\n".join(line.rstrip() for line in raw_rendered.splitlines()) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output), "bytes": len(rendered.encode('utf-8'))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
