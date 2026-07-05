#!/usr/bin/env python3
"""Analyze copied daily-trading Docker bundles and prepare issue fork prompts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import posixpath
import pty
import re
import select
import shlex
import subprocess
import tempfile
import time
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


RUN_RE_TEMPLATE = r"^codex-exec/reports/runs/({ymd}[^/]+)/"
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
SUBMITTED_RESULTS = {"submitted", "success", "accepted", "filled", "partially_filled"}
BLOCKED_RESULTS = {"blocked"}
FAILED_RESULTS = {"failed", "error"}
SKIPPED_RESULTS = {"skipped"}
GENERATED_FILES = {
    "analysis.md",
    "evidence.json",
    "issues.json",
    "fork-commands.sh",
    "fork-results.jsonl",
}
GENERATED_GLOBS = ("fork-*-stdout.txt", "fork-*-stderr.txt")
DEFAULT_FORK_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class ZipText:
    path: str
    text: str


class SafeZip:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._zip = zipfile.ZipFile(path)
        self.names = sorted(self._zip.namelist())
        self._validate_names()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "SafeZip":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _validate_names(self) -> None:
        for name in self.names:
            normalized = name.replace("\\", "/")
            path = PurePosixPath(normalized)
            if normalized.startswith("/") or path.is_absolute() or ".." in path.parts:
                raise ValueError(f"unsafe zip member path: {name}")

    def exists(self, path: str) -> bool:
        return normalize_zip_path(path) in self.names

    def read_text(self, path: str) -> ZipText | None:
        normalized = normalize_zip_path(path)
        if normalized not in self.names:
            return None
        with self._zip.open(normalized) as handle:
            return ZipText(normalized, handle.read().decode("utf-8", errors="replace"))

    def read_json(self, path: str) -> tuple[str, Any] | None:
        item = self.read_text(path)
        if item is None:
            return None
        try:
            return item.path, json.loads(item.text)
        except json.JSONDecodeError as exc:
            return item.path, {"_json_error": str(exc), "_raw_excerpt": item.text[:1000]}


def normalize_zip_path(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/")).lstrip("./")


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD date, got {value!r}") from exc


def json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def discover_run_ids(bundle: SafeZip, date: dt.date) -> list[str]:
    ymd = date.strftime("%Y%m%d")
    pattern = re.compile(RUN_RE_TEMPLATE.format(ymd=re.escape(ymd)))
    run_ids = {match.group(1) for name in bundle.names for match in [pattern.match(name)] if match}
    return sorted(run_ids)


def read_json_artifact(bundle: SafeZip, run_id: str, filename: str) -> dict[str, Any]:
    path = f"codex-exec/reports/runs/{run_id}/{filename}"
    item = bundle.read_json(path)
    if item is None:
        return {"present": False, "path": path}
    actual_path, data = item
    return {"present": True, "path": actual_path, "data": data}


def read_text_artifact(bundle: SafeZip, path: str) -> dict[str, Any]:
    item = bundle.read_text(path)
    if item is None:
        return {"present": False, "path": path}
    return {"present": True, "path": item.path, "text": item.text}


def compact_text(text: str, limit: int = 1400) -> str:
    stripped = "\n".join(line.rstrip() for line in text.strip().splitlines())
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 20].rstrip() + "\n... truncated ..."


def as_dict(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


def as_list(data: Any) -> list[Any]:
    return data if isinstance(data, list) else []


def summarize_execution(artifact: dict[str, Any]) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    data = as_dict(artifact.get("data"))
    orders = as_list(data.get("orders"))
    result_counts = Counter(str(as_dict(order).get("result", "unknown")) for order in orders)
    direction_counts = Counter(str(as_dict(order).get("direction", "unknown")) for order in orders)
    submitted_orders = [
        compact_order(order)
        for order in orders
        if str(as_dict(order).get("result", "")).lower() in SUBMITTED_RESULTS
    ]
    return {
        "present": True,
        "path": artifact["path"],
        "status": data.get("status"),
        "run_id": data.get("run_id"),
        "started_at": data.get("started_at"),
        "order_count": len(orders),
        "result_counts": dict(sorted(result_counts.items())),
        "direction_counts": dict(sorted(direction_counts.items())),
        "submitted_count": len(submitted_orders),
        "blocked_count": sum(result_counts.get(k, 0) for k in BLOCKED_RESULTS),
        "failed_count": sum(result_counts.get(k, 0) for k in FAILED_RESULTS),
        "skipped_count": sum(result_counts.get(k, 0) for k in SKIPPED_RESULTS),
        "submitted_orders": submitted_orders[:20],
        "errors": data.get("errors") or [],
    }


def compact_order(order: Any) -> dict[str, Any]:
    item = as_dict(order)
    fields = (
        "symbol_id",
        "symbol_name",
        "direction",
        "result",
        "reason",
        "validated_order_quantity",
        "final_holding_quantity",
        "expected_holding_quantity",
        "order_api",
        "order_path",
        "order_price",
    )
    return {field: item.get(field) for field in fields if field in item}


def summarize_fills(artifact: dict[str, Any]) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    data = as_dict(artifact.get("data"))
    fills = as_list(data.get("fills"))
    symbols: set[str] = set()
    side_counts: Counter[str] = Counter()
    for fill in fills:
        item = as_dict(fill)
        symbol = item.get("symbol_id") or item.get("pdno") or item.get("ticker")
        if symbol:
            symbols.add(str(symbol))
        side = item.get("direction") or item.get("side") or item.get("trade_type") or item.get("sll_buy_dvsn_cd")
        side_counts[str(side or "unknown")] += 1
    return {
        "present": True,
        "path": artifact["path"],
        "status": data.get("status"),
        "fill_count": len(fills),
        "symbol_count": len(symbols),
        "symbols": sorted(symbols)[:50],
        "side_counts": dict(sorted(side_counts.items())),
        "errors": data.get("errors") or [],
    }


def summarize_account(artifact: dict[str, Any]) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    data = as_dict(artifact.get("data"))
    return {
        "present": True,
        "path": artifact["path"],
        "status": data.get("status"),
        "account_summary": as_dict(data.get("account_summary")),
        "warnings": data.get("warnings") or [],
        "errors": data.get("errors") or [],
    }


def summarize_pipeline(artifact: dict[str, Any]) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    data = as_dict(artifact.get("data"))
    execution = as_dict(data.get("execution"))
    account = as_dict(data.get("account_summary"))
    return {
        "present": True,
        "path": artifact["path"],
        "status": data.get("status"),
        "run_id": data.get("run_id"),
        "started_at": data.get("started_at"),
        "execution": {
            "status": execution.get("status"),
            "order_count": execution.get("order_count"),
            "request_type": execution.get("request_type"),
            "requires_main_agent_order_execution": execution.get("requires_main_agent_order_execution"),
            "errors": execution.get("errors") or [],
        },
        "account_summary": account,
        "telegram_summary_path": data.get("telegram_summary_path"),
        "report_path": data.get("report_path"),
    }


def summarize_decision(artifact: dict[str, Any]) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    data = as_dict(artifact.get("data"))
    market = as_dict(data.get("market_index_snapshot"))
    indexes = []
    for index in as_list(market.get("indexes")):
        item = as_dict(index)
        indexes.append(
            {
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "change_percent": item.get("change_percent"),
                "status": item.get("status"),
            }
        )
    return {
        "present": True,
        "path": artifact["path"],
        "status": data.get("status"),
        "symbol_count": len(as_dict(data.get("symbols")) or as_list(data.get("symbols"))),
        "market_indexes": indexes,
        "errors": data.get("errors") or [],
    }


def summarize_telegram_text(artifact: dict[str, Any]) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    text = str(artifact.get("text") or "")
    order_count_mentions = re.findall(r"주문\s*수[:：]?\s*(\d+)", text)
    return {
        "present": True,
        "path": artifact["path"],
        "excerpt": compact_text(text),
        "order_count_mentions": [int(value) for value in order_count_mentions],
    }


def summarize_log_text(artifact: dict[str, Any], max_excerpt: int = 1200) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    text = str(artifact.get("text") or "")
    lower = text.lower()
    return {
        "present": True,
        "path": artifact["path"],
        "line_count": len(text.splitlines()),
        "char_count": len(text),
        "error_mentions": lower.count("error") + lower.count("exception") + lower.count("traceback"),
        "warning_mentions": lower.count("warning") + lower.count("warn"),
        "excerpt": compact_text(text, max_excerpt),
    }


def summarize_json_log(artifact: dict[str, Any]) -> dict[str, Any]:
    if not artifact.get("present"):
        return {"present": False, "path": artifact["path"]}
    data = artifact.get("data")
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    summary = summarize_log_text({"present": True, "path": artifact["path"], "text": text})
    if isinstance(data, dict):
        summary["top_level_keys"] = sorted(str(key) for key in data.keys())[:40]
        summary["status"] = data.get("status")
    elif isinstance(data, list):
        summary["item_count"] = len(data)
    return summary


def summarize_run_logs(bundle: SafeZip, run_id: str) -> dict[str, Any]:
    return {
        "pipeline_command_log": summarize_json_log(read_json_artifact(bundle, run_id, "pipeline-command-log.json")),
        "main_events": summarize_log_text(read_text_artifact(bundle, f"codex-exec/reports/runs/{run_id}/main-events.jsonl"), max_excerpt=800),
    }


def summarize_portfolio_report(bundle: SafeZip, date: dt.date) -> dict[str, Any]:
    path = f"codex-exec/reports/{date.isoformat()}_포트폴리오.md"
    return summarize_telegram_text(read_text_artifact(bundle, path))


def summarize_telegram_jsonl(bundle: SafeZip, date: dt.date) -> dict[str, Any]:
    path = f"telegram-gateway/memory/telegram-conversations/{date.isoformat()}.jsonl"
    item = bundle.read_text(path)
    if item is None:
        return {"present": False, "path": path}
    direction_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    commands: Counter[str] = Counter()
    outbound_texts: list[str] = []
    parse_errors = 0
    for line in item.text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(record, dict):
            continue
        direction_counts[str(record.get("direction", "unknown"))] += 1
        type_counts[str(record.get("type", "unknown"))] += 1
        text = str(record.get("text") or record.get("caption") or "")
        if record.get("direction") == "inbound" and text.startswith("/"):
            commands[text.split()[0]] += 1
        if record.get("direction") == "outbound" and text:
            outbound_texts.append(text)
    touch_like = [text for text in outbound_texts if "터치" in text or "touch" in text.lower()]
    daily_like = [text for text in outbound_texts if "daily-trading" in text or "거래" in text or "주문" in text]
    return {
        "present": True,
        "path": item.path,
        "line_count": len(item.text.splitlines()),
        "parse_errors": parse_errors,
        "direction_counts": dict(sorted(direction_counts.items())),
        "type_counts": dict(sorted(type_counts.items())),
        "commands": dict(sorted(commands.items())),
        "touch_message_count": len(touch_like),
        "daily_trading_message_count": len(daily_like),
        "daily_trading_excerpts": [compact_text(text, 500) for text in daily_like[-5:]],
    }


def summarize_run(bundle: SafeZip, run_id: str) -> dict[str, Any]:
    execution = summarize_execution(read_json_artifact(bundle, run_id, "execution.json"))
    fills = summarize_fills(read_json_artifact(bundle, run_id, "today-fills.json"))
    account = summarize_account(read_json_artifact(bundle, run_id, "account-before-order.json"))
    pipeline = summarize_pipeline(read_json_artifact(bundle, run_id, "pipeline-summary.json"))
    decision = summarize_decision(read_json_artifact(bundle, run_id, "decision-brief.json"))
    telegram = summarize_telegram_text(read_text_artifact(bundle, f"codex-exec/reports/runs/{run_id}/telegram-summary.txt"))
    return {
        "run_id": run_id,
        "execution": execution,
        "fills": fills,
        "account": account,
        "pipeline": pipeline,
        "decision": decision,
        "telegram_summary": telegram,
        "logs": summarize_run_logs(bundle, run_id),
    }


def collect_day(bundle: SafeZip, date: dt.date, run_id: str | None) -> dict[str, Any]:
    run_ids = discover_run_ids(bundle, date)
    if run_id and run_id not in run_ids:
        raise ValueError(f"run id {run_id!r} not found for {date.isoformat()}")
    primary = run_id or (run_ids[-1] if run_ids else None)
    runs = [summarize_run(bundle, item) for item in run_ids]
    primary_run = next((run for run in runs if run["run_id"] == primary), None)
    return {
        "date": date.isoformat(),
        "run_ids": run_ids,
        "primary_run_id": primary,
        "primary_run": primary_run,
        "intraday_runs": runs,
        "portfolio_report": summarize_portfolio_report(bundle, date),
        "telegram_conversation": summarize_telegram_jsonl(bundle, date),
    }


def collect_previous_days(bundle: SafeZip, date: dt.date, days: int) -> list[dict[str, Any]]:
    previous = []
    for offset in range(1, max(days, 0) + 1):
        day = date - dt.timedelta(days=offset)
        day_summary = collect_day(bundle, day, None)
        if day_summary["run_ids"] or day_summary["telegram_conversation"].get("present"):
            previous.append(day_summary)
    return previous


def collect_git_context(repo: Path) -> dict[str, Any]:
    context: dict[str, Any] = {"repo": str(repo)}
    if not repo.exists():
        context["available"] = False
        context["error"] = "repo path does not exist"
        return context
    context["available"] = True
    context["log"] = run_git(repo, ["log", "--oneline", "--decorate", "-n", "25"])
    context["status"] = run_git(repo, ["status", "--short"])
    status_out = str(context["status"].get("stdout") or "")
    if status_out.strip():
        context["diff_stat"] = run_git(repo, ["diff", "--stat"])
    else:
        context["diff_stat"] = {"returncode": 0, "stdout": "", "stderr": ""}
    return context


def run_git(repo: Path, args: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic helper
        return {"returncode": None, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def extract_issues(evidence: dict[str, Any], issue_count: int) -> tuple[list[dict[str, Any]], str | None]:
    candidates: list[dict[str, Any]] = []
    target = as_dict(evidence.get("target_date"))
    primary = as_dict(target.get("primary_run"))
    if primary:
        candidates.extend(issue_candidates_from_run(primary, target, evidence))
    candidates.extend(issue_candidates_from_telegram(target))
    ranked = sorted(candidates, key=lambda item: (severity_score(item), item.get("impact_rank", 99), item.get("id", "")))
    issues = []
    seen: set[str] = set()
    for item in ranked:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        issues.append(item)
        if len(issues) >= issue_count:
            break
    for index, issue in enumerate(issues, start=1):
        issue["rank"] = index
        issue["question_prompt"] = build_issue_prompt(index, issue)
    shortfall = None
    if len(issues) < issue_count:
        shortfall = f"requested {issue_count} issues but only {len(issues)} evidence-backed issues were found"
    return issues, shortfall


def severity_score(issue: dict[str, Any]) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(issue.get("severity")), 3)


def issue_candidates_from_run(run: dict[str, Any], target: dict[str, Any], evidence: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    execution = as_dict(run.get("execution"))
    fills = as_dict(run.get("fills"))
    account = as_dict(run.get("account"))
    pipeline = as_dict(run.get("pipeline"))
    decision = as_dict(run.get("decision"))
    telegram = as_dict(run.get("telegram_summary"))

    pipeline_order_count = as_dict(pipeline.get("execution")).get("order_count")
    submitted_count = int(execution.get("submitted_count") or 0)
    if isinstance(pipeline_order_count, int) and pipeline_order_count != submitted_count:
        issues.append(
            make_issue(
                "order-count-contract",
                "Telegram/pipeline order count can be confused with submitted orders",
                "high" if pipeline_order_count > submitted_count else "medium",
                10,
                [
                    execution.get("path"),
                    pipeline.get("path"),
                    telegram.get("path"),
                ],
                {
                    "pipeline_order_count": pipeline_order_count,
                    "submitted_count": submitted_count,
                    "execution_result_counts": execution.get("result_counts"),
                    "telegram_order_count_mentions": telegram.get("order_count_mentions"),
                },
                repeated_evidence("order-count-contract", evidence),
                evidence.get("git_context"),
            )
        )

    if submitted_count and fills.get("present"):
        fill_symbols = set(str(symbol) for symbol in fills.get("symbols") or [])
        missing = [
            order
            for order in execution.get("submitted_orders") or []
            if str(order.get("symbol_id")) not in fill_symbols
        ]
        if missing:
            issues.append(
                make_issue(
                    "submitted-order-not-in-fills",
                    "Submitted order is not visible in copied fill snapshot",
                    "medium",
                    20,
                    [execution.get("path"), fills.get("path")],
                    {
                        "submitted_count": submitted_count,
                        "fill_count": fills.get("fill_count"),
                        "missing_submitted_orders": missing[:10],
                    },
                    repeated_evidence("submitted-order-not-in-fills", evidence),
                    evidence.get("git_context"),
                )
            )

    partial_sources = []
    for name, summary in (("account-before-order", account), ("decision-brief", decision), ("pipeline-summary", pipeline)):
        if summary.get("present") and summary.get("status") not in (None, "success"):
            partial_sources.append({"artifact": name, "status": summary.get("status"), "path": summary.get("path")})
    if partial_sources:
        issues.append(
            make_issue(
                "partial-artifact-status",
                "Partial artifact status needs separation from execution truth",
                "medium",
                30,
                [item["path"] for item in partial_sources],
                {"partial_sources": partial_sources},
                repeated_evidence("partial-artifact-status", evidence),
                evidence.get("git_context"),
            )
        )

    account_summary = as_dict(account.get("account_summary")) or as_dict(pipeline.get("account_summary"))
    pnl = account_summary.get("total_pnl_amount")
    domestic_indexes = [
        item
        for item in decision.get("market_indexes") or []
        if item.get("symbol") in {"KOSPI", "KOSDAQ"} and isinstance(item.get("change_percent"), (int, float))
    ]
    crash_indexes = [item for item in domestic_indexes if float(item.get("change_percent")) <= -4.0]
    if crash_indexes and isinstance(pnl, (int, float)) and pnl < 0:
        issues.append(
            make_issue(
                "crash-regime-context",
                "Crash-day market regime should be explicit in issue analysis",
                "medium",
                40,
                [decision.get("path"), account.get("path") or pipeline.get("path")],
                {"market_indexes": crash_indexes, "total_pnl_amount": pnl},
                repeated_evidence("crash-regime-context", evidence),
                evidence.get("git_context"),
            )
        )

    return issues


def issue_candidates_from_telegram(target: dict[str, Any]) -> list[dict[str, Any]]:
    telegram = as_dict(target.get("telegram_conversation"))
    if not telegram.get("present"):
        return []
    issues = []
    if int(telegram.get("touch_message_count") or 0) >= 3:
        issues.append(
            make_issue(
                "telegram-touch-noise",
                "Telegram touch/alert messages may be noisy on the target date",
                "low",
                50,
                [telegram.get("path")],
                {
                    "touch_message_count": telegram.get("touch_message_count"),
                    "direction_counts": telegram.get("direction_counts"),
                },
                {},
                {},
            )
        )
    return issues


def make_issue(
    issue_id: str,
    title: str,
    severity: str,
    impact_rank: int,
    evidence_paths: list[Any],
    target_date_evidence: dict[str, Any],
    previous_date_evidence: dict[str, Any] | None,
    git_context: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "title": title,
        "severity": severity,
        "impact_rank": impact_rank,
        "evidence_paths": [str(path) for path in evidence_paths if path],
        "target_date_evidence": target_date_evidence,
        "previous_date_evidence": previous_date_evidence or {},
        "git_context": compact_git_context(git_context or {}),
    }


def repeated_evidence(issue_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    repeats = []
    for day in evidence.get("previous_dates") or []:
        primary = as_dict(day.get("primary_run"))
        if not primary:
            continue
        probe = {candidate["id"] for candidate in issue_candidates_from_run(primary, day, {"previous_dates": [], "git_context": {}})}
        if issue_id in probe:
            repeats.append({"date": day.get("date"), "primary_run_id": day.get("primary_run_id")})
    return {"repeated_on": repeats} if repeats else {}


def compact_git_context(git_context: dict[str, Any]) -> dict[str, Any]:
    log = as_dict(git_context.get("log"))
    status = as_dict(git_context.get("status"))
    diff = as_dict(git_context.get("diff_stat"))
    return {
        "repo": git_context.get("repo"),
        "available": git_context.get("available"),
        "recent_log_excerpt": "\n".join(str(log.get("stdout") or "").splitlines()[:12]),
        "status_short": status.get("stdout") or "",
        "diff_stat": diff.get("stdout") or "",
    }


def build_issue_prompt(index: int, issue: dict[str, Any]) -> str:
    evidence_paths = "\n".join(f"- {path}" for path in issue.get("evidence_paths") or [])
    evidence_summary = json.dumps(issue.get("target_date_evidence") or {}, ensure_ascii=False, sort_keys=True)
    if len(evidence_summary) > 1200:
        evidence_summary = evidence_summary[:1180] + "... truncated"
    return (
        f"{index}번째 문제점에 대해서 구체적으로 설명해봐. "
        "코드/파일 수정, git 작업, patch 적용은 하지 말고 분석만 해줘.\n\n"
        f"문제 제목: {issue.get('title')}\n"
        f"심각도: {issue.get('severity')}\n"
        "근거 artifact:\n"
        f"{evidence_paths or '- 없음'}\n\n"
        "핵심 근거 요약:\n"
        f"{evidence_summary}"
    )


def build_fork_argv(session_id: str, prompt: str, codex_bin: str = "codex") -> list[str]:
    return [
        codex_bin,
        "fork",
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "never",
        session_id,
        prompt,
    ]


def shell_quote_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in argv)


def write_outputs(output_dir: Path, evidence: dict[str, Any], issues: list[dict[str, Any]], shortfall: str | None, fork_commands: list[list[str]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "evidence.json").write_text(json_dump(evidence) + "\n", encoding="utf-8")
    (output_dir / "issues.json").write_text(json_dump({"issues": issues, "shortfall": shortfall}) + "\n", encoding="utf-8")
    (output_dir / "analysis.md").write_text(render_analysis(evidence, issues, shortfall), encoding="utf-8")
    script = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    script.extend(shell_quote_command(argv) for argv in fork_commands)
    (output_dir / "fork-commands.sh").write_text("\n".join(script) + "\n", encoding="utf-8")


def render_analysis(evidence: dict[str, Any], issues: list[dict[str, Any]], shortfall: str | None) -> str:
    target = as_dict(evidence.get("target_date"))
    primary = as_dict(target.get("primary_run"))
    lines = [
        "# Danta Bot Daily Review",
        "",
        f"- Target date: `{target.get('date')}`",
        f"- Primary run: `{target.get('primary_run_id')}`",
        f"- Same-date runs: `{len(target.get('run_ids') or [])}`",
        f"- Previous dates compared: `{len(evidence.get('previous_dates') or [])}`",
        "",
        "## Primary Evidence",
        "",
    ]
    if primary:
        execution = as_dict(primary.get("execution"))
        fills = as_dict(primary.get("fills"))
        account = as_dict(primary.get("account"))
        pipeline = as_dict(primary.get("pipeline"))
        lines.extend(
            [
                f"- execution status: `{execution.get('status')}`",
                f"- execution result counts: `{execution.get('result_counts')}`",
                f"- submitted count: `{execution.get('submitted_count')}`",
                f"- fill count: `{fills.get('fill_count')}`",
                f"- account status: `{account.get('status')}`",
                f"- pipeline order count: `{as_dict(pipeline.get('execution')).get('order_count')}`",
            ]
        )
    else:
        lines.append("- No primary run found.")
    lines.extend(["", "## Issues", ""])
    if not issues:
        lines.append("- No evidence-backed issue found.")
    for issue in issues:
        lines.extend(
            [
                f"### {issue.get('rank')}. {issue.get('title')}",
                "",
                f"- id: `{issue.get('id')}`",
                f"- severity: `{issue.get('severity')}`",
                "- evidence:",
            ]
        )
        for path in issue.get("evidence_paths") or []:
            lines.append(f"  - `{path}`")
        lines.extend(["", ""])
    if shortfall:
        lines.extend(["## Shortfall", "", f"- {shortfall}", ""])
    lines.extend(["## Git Context", ""])
    git_context = compact_git_context(as_dict(evidence.get("git_context")))
    lines.append("```text")
    lines.append(str(git_context.get("recent_log_excerpt") or ""))
    if git_context.get("status_short"):
        lines.append("\nstatus:")
        lines.append(str(git_context.get("status_short")))
    if git_context.get("diff_stat"):
        lines.append("\ndiff stat:")
        lines.append(str(git_context.get("diff_stat")))
    lines.append("```")
    return "\n".join(lines).rstrip() + "\n"


def resolve_fork_session_id(explicit_session_id: str | None) -> str:
    session_id = explicit_session_id or os.getenv("CODEX_THREAD_ID")
    if not session_id:
        raise ValueError("fork creation requires --fork-session-id or CODEX_THREAD_ID")
    return session_id


def codex_sessions_dir() -> Path:
    return Path(os.getenv("CODEX_HOME", Path.home() / ".codex")).expanduser() / "sessions"


def recorded_session_ids() -> dict[str, float]:
    sessions_dir = codex_sessions_dir()
    if not sessions_dir.exists():
        return {}
    sessions: dict[str, float] = {}
    for path in sessions_dir.rglob("rollout-*.jsonl"):
        match = UUID_RE.search(path.name)
        if match:
            sessions[match.group(0)] = path.stat().st_mtime
    return sessions


def newest_session_id(sessions: dict[str, float]) -> str | None:
    if not sessions:
        return None
    return max(sessions.items(), key=lambda item: item[1])[0]


def fork_timeout_seconds() -> int:
    raw = os.getenv("CODEX_FORK_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_FORK_TIMEOUT_SECONDS
    try:
        return max(10, int(raw))
    except ValueError:
        return DEFAULT_FORK_TIMEOUT_SECONDS


def run_tui_command(argv: list[str], timeout_seconds: int) -> tuple[int | None, str, bool]:
    master_fd, slave_fd = pty.openpty()
    process = subprocess.Popen(
        argv,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)
    chunks: list[bytes] = []
    saw_working = False
    sent_quit = False
    timed_out = False
    deadline = time.monotonic() + timeout_seconds

    try:
        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                break
            ready, _, _ = select.select([master_fd], [], [], min(0.2, remaining))
            if not ready:
                continue
            try:
                data = os.read(master_fd, 8192)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            text = b"".join(chunks[-20:]).decode("utf-8", errors="replace")
            saw_working = saw_working or "| Working |" in text
            if saw_working and not sent_quit and "| Ready |" in text:
                os.write(master_fd, b"/quit\r")
                sent_quit = True

        while select.select([master_fd], [], [], 0)[0]:
            try:
                data = os.read(master_fd, 8192)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(master_fd)

    if process.returncode is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    output = b"".join(chunks).decode("utf-8", errors="replace")
    return process.returncode, output, timed_out


def run_forks(output_dir: Path, issues: list[dict[str, Any]], session_id: str, codex_bin: str = "codex") -> None:
    results_path = output_dir / "fork-results.jsonl"
    with results_path.open("a", encoding="utf-8") as handle:
        for issue in issues:
            argv = build_fork_argv(session_id, str(issue.get("question_prompt") or ""), codex_bin=codex_bin)
            stdout_path = output_dir / f"fork-{issue.get('rank')}-stdout.txt"
            stderr_path = output_dir / f"fork-{issue.get('rank')}-stderr.txt"
            record: dict[str, Any] = {"issue_id": issue.get("id"), "argv": argv}
            try:
                before_sessions = recorded_session_ids()
                returncode, combined, timed_out = run_tui_command(argv, fork_timeout_seconds())
                after_sessions = recorded_session_ids()
                new_sessions = {key: value for key, value in after_sessions.items() if key not in before_sessions}
                detected_session_id = newest_session_id(new_sessions)
                if not detected_session_id:
                    match = UUID_RE.search(combined)
                    detected_session_id = match.group(0) if match else None
                stdout_path.write_text(combined, encoding="utf-8")
                stderr_path.write_text("", encoding="utf-8")
                record.update(
                    {
                        "exit_code": returncode,
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                        "detected_session_id": detected_session_id,
                        "timed_out": timed_out,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - best-effort fork logging
                record.update({"exit_code": None, "error": str(exc), "detected_session_id": None})
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def prepare_output_dir(output_dir: Path, force: bool) -> None:
    if output_dir.exists():
        if not output_dir.is_dir():
            raise FileExistsError(f"output path exists and is not a directory: {output_dir}")
        if not force:
            raise FileExistsError(f"output dir already exists; pass --force to overwrite: {output_dir}")
        for name in GENERATED_FILES:
            path = output_dir / name
            if path.exists():
                path.unlink()
        for pattern in GENERATED_GLOBS:
            for path in output_dir.glob(pattern):
                if path.is_file():
                    path.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    zip_path = Path(args.zip).expanduser()
    output_dir = Path(args.output_dir).expanduser() if args.output_dir else Path("/tmp/danta-bot-daily-review") / args.date.isoformat()
    prepare_output_dir(output_dir, args.force)
    with SafeZip(zip_path) as bundle:
        target = collect_day(bundle, args.date, args.run_id)
        previous = collect_previous_days(bundle, args.date, args.previous_days)
    git_context = collect_git_context(Path(args.repo).expanduser()) if args.repo else {"available": False}
    evidence = {
        "zip_path": str(zip_path),
        "target_date": target,
        "previous_dates": previous,
        "git_context": git_context,
    }
    issues, shortfall = extract_issues(evidence, args.issue_count)
    session_id = args.fork_session_id or os.getenv("CODEX_THREAD_ID") or "<SESSION_ID>"
    fork_commands = [build_fork_argv(session_id, str(issue.get("question_prompt") or "")) for issue in issues]
    write_outputs(output_dir, evidence, issues, shortfall, fork_commands)
    if not args.dry_run:
        run_forks(output_dir, issues, resolve_fork_session_id(args.fork_session_id), codex_bin=os.getenv("CODEX_BIN", "codex"))
    return {"output_dir": str(output_dir), "issue_count": len(issues), "shortfall": shortfall}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true", help="run fixture-based self-tests")
    parser.add_argument("--zip", help="copied docker zip path")
    parser.add_argument("--date", type=parse_date, help="target date in YYYY-MM-DD")
    parser.add_argument("--issue-count", type=int, default=3, help="maximum number of evidence-backed issues")
    parser.add_argument("--repo", default=os.getcwd(), help="local repo path for git context")
    parser.add_argument("--output-dir", help="output directory")
    parser.add_argument("--previous-days", type=int, default=1, help="previous calendar days to compare")
    parser.add_argument("--run-id", help="explicit primary run id")
    parser.add_argument("--fork-session-id", help="session id passed positionally to codex fork; defaults to CODEX_THREAD_ID")
    parser.add_argument("--execute-forks", action="store_true", help="deprecated compatibility flag; forks run by default unless --dry-run is set")
    parser.add_argument("--dry-run", action="store_true", help="write commands but do not run forks")
    parser.add_argument("--force", action="store_true", help="overwrite output directory")
    return parser


def require_args(args: argparse.Namespace) -> None:
    if args.self_test:
        return
    missing = [name for name in ("zip", "date") if getattr(args, name.replace("-", "_"), None) is None]
    if missing:
        raise SystemExit(f"missing required arguments: {', '.join('--' + name for name in missing)}")
    if args.issue_count < 0:
        raise SystemExit("--issue-count must be >= 0")
    if args.previous_days < 0:
        raise SystemExit("--previous-days must be >= 0")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    require_args(args)
    if args.self_test:
        run_self_tests()
        return 0
    result = analyze(args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def run_self_tests() -> None:
    tests = [
        test_safe_zip_rejects_traversal,
        test_latest_and_explicit_run_selection,
        test_previous_day_and_telegram_summary,
        test_git_context_fallback,
        test_issue_count_shortfall_and_ranking,
        test_fork_argv_and_outputs,
        test_execute_fork_logging,
        test_force_keeps_unknown_files,
    ]
    for test in tests:
        test()
    print(f"self-test passed: {len(tests)} tests")


def make_fixture_zip(path: Path) -> None:
    def put_json(zipf: zipfile.ZipFile, name: str, data: Any) -> None:
        zipf.writestr(name, json.dumps(data, ensure_ascii=False))

    with zipfile.ZipFile(path, "w") as zipf:
        for run_id, order_count, submitted, status in [
            ("20260701T151500+0900-prev", 1, 0, "success"),
            ("20260702T090000+0900-old", 1, 0, "success"),
            ("20260702T151500+0900-new", 3, 1, "success"),
        ]:
            orders = []
            for index in range(order_count):
                result = "submitted" if index < submitted else "skipped"
                orders.append(
                    {
                        "symbol_id": f"00000{index}",
                        "symbol_name": f"Name {index}",
                        "direction": "sell" if result == "submitted" else "none",
                        "result": result,
                        "reason": "fixture",
                        "validated_order_quantity": 1 if result == "submitted" else 0,
                    }
                )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/execution.json",
                {"status": "success", "run_id": run_id, "orders": orders},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/pipeline-summary.json",
                {"status": status, "run_id": run_id, "execution": {"status": status, "order_count": order_count}},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/today-fills.json",
                {"status": "success", "fills": []},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/account-before-order.json",
                {"status": "partial", "account_summary": {"total_pnl_amount": -1000}},
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/decision-brief.json",
                {
                    "status": "success",
                    "market_index_snapshot": {
                        "indexes": [
                            {"symbol": "KOSPI", "name": "KOSPI", "change_percent": -5.0, "status": "success"}
                        ]
                    },
                },
            )
            put_json(
                zipf,
                f"codex-exec/reports/runs/{run_id}/pipeline-command-log.json",
                {"status": "success", "commands": [{"returncode": 0}]},
            )
            zipf.writestr(f"codex-exec/reports/runs/{run_id}/main-events.jsonl", '{"type":"turn.completed"}\n')
            zipf.writestr(f"codex-exec/reports/runs/{run_id}/telegram-summary.txt", f"주문 수: {order_count}")
        zipf.writestr("codex-exec/reports/2026-07-02_포트폴리오.md", "portfolio")
        zipf.writestr(
            "telegram-gateway/memory/telegram-conversations/2026-07-02.jsonl",
            "\n".join(
                [
                    json.dumps({"direction": "inbound", "type": "telegram_message", "text": "/version"}, ensure_ascii=False),
                    json.dumps({"direction": "outbound", "type": "telegram_send", "text": "거래 주문 수: 3"}, ensure_ascii=False),
                ]
            )
            + "\n",
        )
        zipf.writestr(
            "telegram-gateway/memory/telegram-conversations/2026-07-01.jsonl",
            json.dumps({"direction": "outbound", "type": "telegram_send", "text": "주문 수: 1"}, ensure_ascii=False) + "\n",
        )


def test_safe_zip_rejects_traversal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.zip"
        with zipfile.ZipFile(path, "w") as zipf:
            zipf.writestr("../bad.txt", "bad")
        try:
            with SafeZip(path):
                raise AssertionError("unsafe zip was accepted")
        except ValueError as exc:
            assert "unsafe zip member" in str(exc)


def test_latest_and_explicit_run_selection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "fixture.zip"
        make_fixture_zip(zip_path)
        with SafeZip(zip_path) as bundle:
            day = collect_day(bundle, dt.date(2026, 7, 2), None)
            assert day["primary_run_id"] == "20260702T151500+0900-new"
            explicit = collect_day(bundle, dt.date(2026, 7, 2), "20260702T090000+0900-old")
            assert explicit["primary_run_id"] == "20260702T090000+0900-old"


def test_previous_day_and_telegram_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "fixture.zip"
        make_fixture_zip(zip_path)
        with SafeZip(zip_path) as bundle:
            previous = collect_previous_days(bundle, dt.date(2026, 7, 2), 1)
            assert len(previous) == 1
            assert previous[0]["telegram_conversation"]["present"] is True
            assert previous[0]["primary_run"]["logs"]["pipeline_command_log"]["present"] is True
            assert previous[0]["primary_run"]["logs"]["main_events"]["present"] is True


def test_git_context_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        context = collect_git_context(Path(tmp) / "missing")
        assert context["available"] is False


def test_issue_count_shortfall_and_ranking() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "fixture.zip"
        output_dir = Path(tmp) / "out"
        make_fixture_zip(zip_path)
        args = argparse.Namespace(
            zip=str(zip_path),
            date=dt.date(2026, 7, 2),
            issue_count=2,
            repo=str(Path(tmp) / "missing"),
            output_dir=str(output_dir),
            previous_days=1,
            run_id=None,
            fork_session_id=None,
            execute_forks=False,
            dry_run=True,
            force=True,
        )
        result = analyze(args)
        issues = json.loads((output_dir / "issues.json").read_text(encoding="utf-8"))["issues"]
        assert result["issue_count"] == 2
        assert issues[0]["id"] == "order-count-contract"


def test_fork_argv_and_outputs() -> None:
    argv = build_fork_argv("session-1", "1번째 문제점에 대해서 구체적으로 설명해봐.")
    assert argv[:6] == ["codex", "fork", "--sandbox", "read-only", "--ask-for-approval", "never"]
    assert argv[6] == "session-1"
    quoted = shell_quote_command(argv)
    assert "codex fork" in quoted


def test_execute_fork_logging() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake = tmp_path / "codex-fake"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('forked session 12345678-1234-1234-1234-123456789abc')\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        issues = [
            {
                "id": "x",
                "rank": 1,
                "question_prompt": "1번째 문제점에 대해서 구체적으로 설명해봐.",
            }
        ]
        run_forks(tmp_path, issues, "session-1", codex_bin=str(fake))
        rows = [json.loads(line) for line in (tmp_path / "fork-results.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["exit_code"] == 0
        assert rows[0]["detected_session_id"] == "12345678-1234-1234-1234-123456789abc"


def test_force_keeps_unknown_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp) / "out"
        output_dir.mkdir()
        keep = output_dir / "keep.txt"
        generated = output_dir / "analysis.md"
        keep.write_text("keep", encoding="utf-8")
        generated.write_text("old", encoding="utf-8")
        prepare_output_dir(output_dir, True)
        assert keep.exists()
        assert not generated.exists()


if __name__ == "__main__":
    raise SystemExit(main())
