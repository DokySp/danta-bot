from __future__ import annotations

import argparse
import html
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARTIFACT_NAME = "symbol-states.json"
DEFAULT_LIMIT = 30
TELEGRAM_HTML_SAFE_LIMIT = 3500
NORMAL_STATE = "normal_rebalance"
STATE_ORDER = (
    "user_no_trade",
    "order_state_locked",
    "recent_trade_cooldown",
    "profit_take_candidate",
    "risk_reduce_candidate",
    "missed_reduce_observe",
    "bottom_watch",
    "staged_rebuy_candidate",
    "normal_rebalance",
)
STATE_LABELS = {
    "user_no_trade": "사용자 거래금지",
    "order_state_locked": "주문/계좌 상태 잠금",
    "recent_trade_cooldown": "당일 체결 쿨다운",
    "profit_take_candidate": "익절/비중 축소 후보",
    "risk_reduce_candidate": "하락 초입 축소 후보",
    "missed_reduce_observe": "추격 매도 금지 관찰",
    "bottom_watch": "저점 감시",
    "staged_rebuy_candidate": "분할 추매 후보",
    "normal_rebalance": "일반 리밸런싱",
}
SEVERITY_LABELS = {
    "hard_full_block": "hard 전체 차단",
    "hard_directional": "hard 방향 차단",
    "soft": "soft 판단 보조",
}
EVIDENCE_LABELS = {
    "pnl_rate": "평가손익률",
    "concentration_pct": "비중",
    "day_change_pct": "당일등락",
    "daily_distance_ma20_pct": "MA20괴리",
    "daily_change_5_period_pct": "5일등락",
    "daily_range_position_pct": "범위위치",
    "intraday_change_pct": "분중등락",
    "same_day_fill_count": "당일체결",
    "same_day_has_non_bot_fill": "수동체결",
    "active_buy_quantity": "미체결매수",
    "active_sell_quantity": "미체결매도",
}
PERCENT_EVIDENCE_KEYS = {
    "pnl_rate",
    "concentration_pct",
    "day_change_pct",
    "daily_distance_ma20_pct",
    "daily_change_5_period_pct",
    "daily_range_position_pct",
    "intraday_change_pct",
}


class SymbolFlagsCommandError(RuntimeError):
    def __init__(self, log_message: str, html_message: str) -> None:
        super().__init__(log_message)
        self.html_message = html_message


@dataclass(frozen=True)
class SymbolFlagsRequest:
    include_normal: bool
    limit: int
    run_selector: str | None


@dataclass(frozen=True)
class SymbolFlagsCommandResult:
    html_message: str


def code_text(value: Any) -> str:
    return f"<code>{html.escape(str(value))}</code>"


def plain_text(value: Any) -> str:
    return html.escape(str(value))


def usage_html() -> str:
    return (
        "사용법: <code>/show_symbol_flags</code>, "
        "<code>/show_symbol_flags all</code>, "
        "<code>/show_symbol_flags RUN_ID</code>"
    )


def parse_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise SymbolFlagsCommandError("invalid symbol flags limit", usage_html()) from exc
    if value < 1 or value > 100:
        raise SymbolFlagsCommandError(
            "symbol flags limit out of range",
            "표시 개수는 <code>1</code>부터 <code>100</code> 사이로 입력해주세요.",
        )
    return value


def parse_symbol_flags_args(args: str) -> SymbolFlagsRequest:
    include_normal = False
    limit = DEFAULT_LIMIT
    run_selector: str | None = None
    tokens = args.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"all", "--all"}:
            include_normal = True
        elif token in {"flagged", "--flagged"}:
            include_normal = False
        elif token.startswith("--limit="):
            limit = parse_limit(token.partition("=")[2])
        elif token == "--limit":
            index += 1
            if index >= len(tokens):
                raise SymbolFlagsCommandError("missing symbol flags limit", usage_html())
            limit = parse_limit(tokens[index])
        elif token.startswith("-"):
            raise SymbolFlagsCommandError("unknown symbol flags option", usage_html())
        elif run_selector is None:
            run_selector = token
        else:
            raise SymbolFlagsCommandError("too many symbol flags arguments", usage_html())
        index += 1
    return SymbolFlagsRequest(include_normal=include_normal, limit=limit, run_selector=run_selector)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def artifact_sort_key(path: Path) -> tuple[str, str, int]:
    started_at = ""
    try:
        loaded = load_json(path)
    except Exception:
        loaded = {}
    if isinstance(loaded, dict):
        started_at = str(loaded.get("started_at") or loaded.get("created_at") or "")
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = 0
    return started_at, path.parent.name, mtime_ns


def latest_symbol_states_path(workspace_dir: Path) -> Path | None:
    runs_dir = workspace_dir / "reports" / "runs"
    if not runs_dir.is_dir():
        return None
    candidates = [path for path in runs_dir.glob(f"*/{ARTIFACT_NAME}") if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=artifact_sort_key)


def selected_symbol_states_path(workspace_dir: Path, selector: str) -> Path | None:
    raw = Path(selector)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(workspace_dir / "reports" / "runs" / selector / ARTIFACT_NAME)
        candidates.append(workspace_dir / selector)
        candidates.append(workspace_dir / selector / ARTIFACT_NAME)

    for candidate in candidates:
        path = candidate / ARTIFACT_NAME if candidate.is_dir() else candidate
        if path.is_file():
            return path
    return None


def resolve_symbol_states_path(workspace_dir: Path, request: SymbolFlagsRequest) -> Path:
    if request.run_selector:
        selected = selected_symbol_states_path(workspace_dir, request.run_selector)
        if selected is not None:
            return selected
        raise SymbolFlagsCommandError(
            "selected symbol states artifact not found",
            (
                "<b>종목 상태 플래그</b>\n"
                f"{code_text(request.run_selector)}에 해당하는 <code>{ARTIFACT_NAME}</code>을 찾지 못했습니다.\n"
                f"{usage_html()}"
            ),
        )

    latest = latest_symbol_states_path(workspace_dir)
    if latest is not None:
        return latest
    raise SymbolFlagsCommandError(
        "symbol states artifact not found",
        (
            "<b>종목 상태 플래그</b>\n"
            f"<code>{workspace_dir / 'reports' / 'runs' / '*' / ARTIFACT_NAME}</code> 파일을 찾지 못했습니다.\n"
            "daily-trading 실행 후 다시 확인해주세요."
        ),
    )


def load_symbol_states_artifact(path: Path) -> dict[str, Any]:
    try:
        loaded = load_json(path)
    except Exception as exc:
        raise SymbolFlagsCommandError(
            "symbol states artifact unreadable",
            f"<code>{plain_text(path)}</code> 파일을 읽지 못했습니다: {plain_text(exc)}",
        ) from exc
    if not isinstance(loaded, dict):
        raise SymbolFlagsCommandError(
            "symbol states artifact has invalid root",
            f"<code>{plain_text(path)}</code> 파일 형식이 올바르지 않습니다.",
        )
    if not isinstance(loaded.get("symbols"), list):
        raise SymbolFlagsCommandError(
            "symbol states artifact has invalid symbols",
            f"<code>{plain_text(path)}</code> 파일에 <code>symbols</code> 배열이 없습니다.",
        )
    return loaded


def state_counts(symbols: list[dict[str, Any]]) -> dict[str, int]:
    counts = {state: 0 for state in STATE_ORDER}
    for item in symbols:
        state = str(item.get("state") or "")
        counts[state] = counts.get(state, 0) + 1
    return counts


def format_counts(counts: dict[str, Any], include_normal: bool) -> str:
    parts = []
    ordered_states = list(STATE_ORDER)
    for state in counts:
        if state not in ordered_states:
            ordered_states.append(state)
    for state in ordered_states:
        if state == NORMAL_STATE and not include_normal:
            continue
        try:
            count = int(counts.get(state) or 0)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        parts.append(f"{code_text(state)} {code_text(count)}")
    return ", ".join(parts)


def numeric_priority(item: dict[str, Any]) -> int:
    try:
        return int(item.get("priority") or 999)
    except (TypeError, ValueError):
        return 999


def format_action_list(actions: Any) -> str:
    if not isinstance(actions, list):
        return ""
    values = [str(action).strip() for action in actions if str(action).strip()]
    return ", ".join(code_text(action) for action in values[:8])


def format_constraints(item: dict[str, Any]) -> str:
    hard_constraints = item.get("hard_constraints")
    if isinstance(hard_constraints, dict):
        parts = []
        blocked = format_action_list(hard_constraints.get("blocked_actions"))
        allowed = format_action_list(hard_constraints.get("allowed_actions"))
        if blocked:
            parts.append(f"차단 {blocked}")
        if allowed:
            parts.append(f"허용 {allowed}")
        return "; ".join(parts)

    soft_guidance = item.get("soft_guidance")
    if isinstance(soft_guidance, dict):
        parts = []
        preferred = format_action_list(soft_guidance.get("preferred_actions"))
        discouraged = format_action_list(soft_guidance.get("discouraged_actions"))
        if preferred:
            parts.append(f"선호 {preferred}")
        if discouraged:
            parts.append(f"비선호 {discouraged}")
        return "; ".join(parts)
    return ""


def format_reasons(item: dict[str, Any]) -> str:
    reasons = item.get("reasons")
    if not isinstance(reasons, list):
        return ""
    texts = [plain_text(reason) for reason in reasons[:3] if str(reason).strip()]
    return "; ".join(texts)


def format_evidence_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, (int, float)):
        if key in PERCENT_EVIDENCE_KEYS:
            return f"{value:.2f}%"
        return str(value)
    return str(value)


def format_evidence(item: dict[str, Any]) -> str:
    evidence = item.get("evidence")
    if not isinstance(evidence, dict):
        return ""
    parts = []
    for key, label in EVIDENCE_LABELS.items():
        if key not in evidence:
            continue
        value = evidence.get(key)
        if value in (None, "", [], {}):
            continue
        parts.append(f"{plain_text(label)} {code_text(format_evidence_value(key, value))}")
    return ", ".join(parts[:8])


def format_symbol_row(index: int, item: dict[str, Any]) -> list[str]:
    symbol_id = str(item.get("symbol_id") or item.get("symbol") or "")
    symbol_name = str(item.get("symbol_name") or "")
    state = str(item.get("state") or "unknown")
    severity = str(item.get("severity") or ("hard" if item.get("hard") else "soft"))
    header_name = f" {plain_text(symbol_name)}" if symbol_name and symbol_name != symbol_id else ""
    lines = [
        f"{index}. {code_text(symbol_id)}{header_name}",
        f"플래그: {code_text(state)} ({plain_text(STATE_LABELS.get(state, state))})",
        f"강도: {code_text(severity)} ({plain_text(SEVERITY_LABELS.get(severity, severity))})",
    ]
    reasons = format_reasons(item)
    if reasons:
        lines.append(f"근거: {reasons}")
    constraints = format_constraints(item)
    if constraints:
        lines.append(f"제약/가이드: {constraints}")
    evidence = format_evidence(item)
    if evidence:
        lines.append(f"수치: {evidence}")
    return lines


def compact_message(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line != "")


def fits_telegram_safe_limit(lines: list[str], footer: str = "") -> bool:
    rendered = compact_message(lines + ([footer] if footer else []))
    return len(rendered) <= TELEGRAM_HTML_SAFE_LIMIT


def render_symbol_flags(workspace_dir: Path, args: str) -> SymbolFlagsCommandResult:
    request = parse_symbol_flags_args(args)
    artifact_path = resolve_symbol_states_path(workspace_dir, request)
    artifact = load_symbol_states_artifact(artifact_path)
    symbols = [item for item in artifact["symbols"] if isinstance(item, dict)]
    counts = artifact.get("state_counts") if isinstance(artifact.get("state_counts"), dict) else state_counts(symbols)
    flagged = [item for item in symbols if str(item.get("state") or "") != NORMAL_STATE]
    rows = symbols if request.include_normal else flagged
    rows = sorted(rows, key=lambda item: (numeric_priority(item), str(item.get("symbol_id") or "")))
    run_id = str(artifact.get("run_id") or artifact_path.parent.name)
    started_at = str(artifact.get("started_at") or "")
    status = str(artifact.get("status") or "")
    mode = "전체 상태" if request.include_normal else "플래그 종목만"

    lines = [
        "<b>종목 상태 플래그</b>",
        f"run_id: {code_text(run_id)}",
        f"source: {code_text(artifact_path)}",
        f"표시: {code_text(mode)}",
        f"플래그 종목: {code_text(len(flagged))} / 전체 {code_text(len(symbols))}",
    ]
    if started_at:
        lines.append(f"started_at: {code_text(started_at)}")
    if status:
        lines.append(f"status: {code_text(status)}")
    counts_text = format_counts(counts, request.include_normal)
    if counts_text:
        lines.append(f"상태 수: {counts_text}")

    if not rows:
        lines.append("")
        lines.append("플래그가 붙은 종목이 없습니다.")
        return SymbolFlagsCommandResult("\n".join(lines))

    lines.append("")
    displayed_count = 0
    limit_rows = rows[: request.limit]
    for item in limit_rows:
        next_count = displayed_count + 1
        row_lines = format_symbol_row(next_count, item)
        remaining_after_row = len(rows) - next_count
        footer = ""
        if remaining_after_row:
            footer = f"외 {code_text(remaining_after_row)}개는 <code>--limit</code> 값을 늘려 확인할 수 있습니다."
        candidate = lines + row_lines + [""]
        if not fits_telegram_safe_limit(candidate, footer):
            break
        lines = candidate
        displayed_count = next_count

    hidden_count = len(rows) - displayed_count
    if hidden_count:
        footer = f"외 {code_text(hidden_count)}개는 <code>--limit</code> 값을 늘려 확인할 수 있습니다."
        if not fits_telegram_safe_limit(lines, footer):
            footer = f"외 {code_text(hidden_count)}개는 다음 조회에서 확인해주세요."
        lines.append(footer)
    return SymbolFlagsCommandResult(compact_message(lines))


def write_self_test_artifact(run_dir: Path, artifact: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ARTIFACT_NAME).write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")


def self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw:
        workspace_dir = Path(raw)
        write_self_test_artifact(
            workspace_dir / "reports" / "runs" / "20260706T090000",
            {
                "run_id": "run-old",
                "started_at": "2026-07-06T00:00:00+00:00",
                "status": "success",
                "symbols": [
                    {
                        "symbol_id": "035420",
                        "symbol_name": "NAVER",
                        "state": "bottom_watch",
                        "priority": 7,
                        "severity": "soft",
                        "reasons": ["near-low or damaged trend requires stabilization watch"],
                        "evidence": {"daily_range_position_pct": 12.5},
                        "soft_guidance": {"preferred_actions": ["observe", "hold"]},
                    }
                ],
            },
        )
        write_self_test_artifact(
            workspace_dir / "reports" / "runs" / "20260707T090000",
            {
                "run_id": "run-new",
                "started_at": "2026-07-07T00:00:00+00:00",
                "status": "success",
                "state_counts": {"profit_take_candidate": 1, "normal_rebalance": 1},
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "state": "profit_take_candidate",
                        "priority": 4,
                        "severity": "soft",
                        "reasons": ["positive pnl with overextension/concentration signal"],
                        "evidence": {"pnl_rate": 6.2, "concentration_pct": 11.5},
                        "soft_guidance": {
                            "preferred_actions": ["hold", "partial_trim"],
                            "discouraged_actions": ["increase"],
                        },
                    },
                    {
                        "symbol_id": "000660",
                        "symbol_name": "SK하이닉스",
                        "state": NORMAL_STATE,
                        "priority": 9,
                        "severity": "soft",
                        "reasons": ["no higher-priority symbol state matched"],
                        "evidence": {},
                    },
                ],
            },
        )

        default_message = render_symbol_flags(workspace_dir, "").html_message
        if "run-new" not in default_message:
            failures.append("latest artifact was not selected")
        if "profit_take_candidate" not in default_message or "삼성전자" not in default_message:
            failures.append("flagged symbol was not rendered")
        if "SK하이닉스" in default_message:
            failures.append("normal_rebalance symbol was rendered by default")

        all_message = render_symbol_flags(workspace_dir, "all --limit 10").html_message
        if "SK하이닉스" not in all_message or NORMAL_STATE not in all_message:
            failures.append("all mode did not include normal_rebalance")

        selected_message = render_symbol_flags(workspace_dir, "20260706T090000").html_message
        if "run-old" not in selected_message or "bottom_watch" not in selected_message:
            failures.append("run selector did not select requested artifact")

        large_symbols = []
        for index in range(80):
            large_symbols.append(
                {
                    "symbol_id": f"{index:06d}",
                    "symbol_name": f"테스트종목{index}",
                    "state": "risk_reduce_candidate",
                    "priority": 5,
                    "severity": "soft",
                    "reasons": ["early downside signal with holding exposure and long explanatory context"],
                    "evidence": {
                        "pnl_rate": -1.25,
                        "concentration_pct": 9.5,
                        "day_change_pct": -3.1,
                        "daily_distance_ma20_pct": -4.2,
                    },
                    "soft_guidance": {"preferred_actions": ["hold", "partial_reduce"]},
                }
            )
        write_self_test_artifact(
            workspace_dir / "reports" / "runs" / "20260708T090000",
            {
                "run_id": "run-large",
                "started_at": "2026-07-08T00:00:00+00:00",
                "status": "success",
                "symbols": large_symbols,
            },
        )
        large_message = render_symbol_flags(workspace_dir, "20260708T090000 --all --limit 100").html_message
        if len(large_message) > TELEGRAM_HTML_SAFE_LIMIT:
            failures.append(f"large message exceeded safe limit: {len(large_message)}")
        if "외 " not in large_message:
            failures.append("large message did not include hidden-count footer")

        try:
            render_symbol_flags(workspace_dir, "--limit 0")
            failures.append("invalid limit was accepted")
        except SymbolFlagsCommandError:
            pass

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("symbol_flags self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("command_args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()
    if parsed.self_test:
        return self_test()
    try:
        result = render_symbol_flags(Path(parsed.workspace), " ".join(parsed.command_args))
    except SymbolFlagsCommandError as exc:
        print(exc.html_message)
        return 2
    print(result.html_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
