#!/usr/bin/env python3
"""Run daily-trading as one compact deterministic pipeline command.

The pipeline keeps orchestration and large helper stdout out of the Main agent
prompt path. It writes canonical run artifacts, captures verbose command output
to a local command log, and prints only a compact summary pointer.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
FIRST_VERDICT_ROLES = (
    "analyst-quality-value",
    "analyst-momentum-cycle",
    "analyst-risk-allocation",
)
COMMAND_OUTPUT_LIMIT = 2000


def now_kst() -> datetime:
    return datetime.now(KST)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    tmp.replace(path)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError):
        return None


def load_yaml_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except Exception:
        return None


def resolve_workspace_path(workspace_dir: Path, path_text: str | Path) -> Path:
    path = Path(path_text).expanduser()
    if path.is_absolute():
        return path
    return workspace_dir / path


def repo_root_from(path: Path) -> Path:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def script_dir() -> Path:
    return Path(__file__).resolve().parent


def default_output_dir(run_id: str) -> str:
    return f"reports/runs/{run_id}"


def normalize_symbol_ids(raw: Any) -> list[str]:
    if isinstance(raw, dict):
        raw = raw.get("universe") or raw.get("symbols") or raw.get("symbol_ids") or []
    if raw is None:
        items: list[Any] = []
    elif isinstance(raw, str):
        items = raw.replace("\n", ",").split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        items = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            value = item.get("symbol_id") or item.get("symbol") or item.get("code")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_symbol_key(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if digits and digits == text:
        return digits.zfill(6)
    return text


def zero_usage() -> dict[str, int]:
    return {field: 0 for field in TOKEN_USAGE_FIELDS}


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return ""


def format_signed_number(value: Any) -> str:
    number = as_int(value)
    sign = "+" if number > 0 else ""
    return f"{sign}{number:,}"


def md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\n", " ").strip()


def bool_status(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def report_date_from(started_at: str) -> str:
    text = str(started_at or "").strip()
    if not text:
        return now_kst().strftime("%Y-%m-%d")
    try:
        return datetime.fromisoformat(text).astimezone(KST).strftime("%Y-%m-%d")
    except ValueError:
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else now_kst().strftime("%Y-%m-%d")


def token_usage_from(raw: Any) -> dict[str, int]:
    usage = zero_usage()
    if not isinstance(raw, dict):
        return usage
    for field in TOKEN_USAGE_FIELDS:
        usage[field] = as_int(raw.get(field))
    if usage["total_tokens"] <= 0:
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def compact_text(value: str, limit: int = COMMAND_OUTPUT_LIMIT) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def symbol_key(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("symbol_id") or item.get("symbol") or item.get("code") or "").strip()


def cache_symbol_has_content(value: Any) -> bool:
    if isinstance(value, dict):
        if not value:
            return False
        if value.get("errors") and len(value) <= 1:
            return False
        candidates = [
            item
            for key, item in value.items()
            if key not in {"symbol_name", "errors", "sentiment", "article_date", "date"}
        ]
        return any(cache_symbol_has_content(item) for item in candidates)
    if isinstance(value, list):
        return any(cache_symbol_has_content(item) for item in value)
    text = str(value or "").strip()
    if not text:
        return False
    if "수집된 뉴스가 없습니다" in text:
        return False
    return True


def cache_symbol_keys(path: Path) -> set[str]:
    payload = load_json_if_exists(path) if path.suffix.lower() == ".json" else load_yaml_if_exists(path)
    if not isinstance(payload, dict):
        return set()
    symbols = payload.get("symbols")
    if isinstance(symbols, dict):
        return {normalize_symbol_key(key) for key, value in symbols.items() if normalize_symbol_key(key) and cache_symbol_has_content(value)}
    if isinstance(symbols, list):
        return set(normalize_symbol_ids(symbols))
    return set()


def cache_symbol_all_keys(path: Path) -> set[str]:
    payload = load_json_if_exists(path) if path.suffix.lower() == ".json" else load_yaml_if_exists(path)
    if not isinstance(payload, dict):
        return set()
    symbols = payload.get("symbols")
    if isinstance(symbols, dict):
        return {normalize_symbol_key(key) for key in symbols if normalize_symbol_key(key)}
    if isinstance(symbols, list):
        return set(normalize_symbol_ids(symbols))
    return set()


def cache_coverage(path: Path, symbols: list[str]) -> tuple[bool, list[str]]:
    wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
    available = cache_symbol_keys(path)
    missing = sorted(wanted - available)
    return bool(wanted) and not missing, missing


def cache_evidence_counts(path: Path, symbols: list[str]) -> dict[str, Any]:
    wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
    available = cache_symbol_keys(path)
    present = cache_symbol_all_keys(path)
    usable = wanted & available
    present_wanted = wanted & present
    return {
        "wanted_symbol_count": len(wanted),
        "cache_symbol_count": len(present),
        "present_symbol_count": len(present_wanted),
        "usable_symbol_count": len(usable),
        "missing_usable_symbol_count": len(wanted - available),
        "missing_usable_symbols_sample": sorted(wanted - available)[:20],
    }


def count_symbol_errors(payload: Any) -> int:
    symbols = payload.get("symbols") if isinstance(payload, dict) else []
    if not isinstance(symbols, list):
        return 0
    count = 0
    for item in symbols:
        if not isinstance(item, dict):
            continue
        errors = item.get("errors")
        if isinstance(errors, list) and errors:
            count += 1
    return count


def safe_stage(name: str, status: str, *, detail: str = "", required: bool = True, path: Path | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stage": name,
        "status": status,
        "required": required,
        "detail": detail,
    }
    if path is not None:
        payload["path"] = str(path)
    return payload


def requested_execution_completed(stages: list[dict[str, Any]], execution: dict[str, Any]) -> bool:
    if execution.get("request_type") not in {"demo-submit", "real-submit"}:
        return False
    if execution.get("status") != "success":
        return False
    return any(item.get("stage") == "order-execution" and item.get("status") == "success" for item in stages)


def summarized_status(stages: list[dict[str, Any]], execution: dict[str, Any]) -> str:
    if any(item.get("required") and item.get("status") == "failed" for item in stages):
        return "failed"
    if (
        execution.get("request_type") in {"demo-submit", "real-submit"}
        and execution.get("requires_main_agent_order_execution")
        and not requested_execution_completed(stages, execution)
    ):
        return "partial"
    partial_required = [
        item
        for item in stages
        if item.get("required") and (item.get("status") == "partial" or item.get("status") == "skipped")
    ]
    if requested_execution_completed(stages, execution):
        partial_required = [item for item in partial_required if item.get("stage") != "execution-plan"]
    if partial_required:
        return "partial"
    return "success"


class Pipeline:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workspace_dir = Path(args.workspace_dir).expanduser().resolve()
        self.repo_root = repo_root_from(self.workspace_dir)
        self.run_id = args.run_id or now_kst().strftime("daily-trading-%Y%m%d-%H%M%S-kst")
        self.started_at = args.started_at or now_kst().isoformat(timespec="seconds")
        output_text = args.output_dir or default_output_dir(self.run_id)
        self.output_dir_text = output_text
        self.output_dir = resolve_workspace_path(self.workspace_dir, output_text)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.command_log_path = self.output_dir / "pipeline-command-log.json"
        self.summary_path = self.output_dir / "pipeline-summary.json"
        self.run_path = self.output_dir / "run.json"
        self.logs: list[dict[str, Any]] = []
        self.stages: list[dict[str, Any]] = []

    def add_stage(self, name: str, status: str, *, detail: str = "", required: bool = True, path: Path | None = None) -> None:
        self.stages.append(safe_stage(name, status, detail=detail, required=required, path=path))
        self.write_run_json(status=self.pipeline_status())

    def pipeline_status(self) -> str:
        required_failed = [item for item in self.stages if item.get("required") and item.get("status") == "failed"]
        if required_failed:
            return "failed"
        partial = [
            item
            for item in self.stages
            if item.get("status") == "partial" or (item.get("required") and item.get("status") == "skipped")
        ]
        return "partial" if partial else "success"

    def write_run_json(self, *, status: str | None = None) -> None:
        write_json(
            self.run_path,
            {
                "schema_version": "1",
                "run_id": self.run_id,
                "started_at": self.started_at,
                "updated_at": now_iso(),
                "status": status or self.pipeline_status(),
                "pipeline_summary": str(self.summary_path),
                "stages": self.stages,
            },
        )

    def command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.args.env:
            env["CODEX_MCP_TRADING_ENV"] = "paper" if self.args.env in {"paper", "demo"} else "acct"
        return env

    def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            cmd,
            cwd=self.workspace_dir,
            env=env or self.command_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        entry = {
            "stage": stage,
            "command": cmd,
            "returncode": result.returncode,
            "stdout_tail": compact_text(result.stdout),
            "stderr_tail": compact_text(result.stderr),
            "required": required,
            "recorded_at": now_iso(),
        }
        self.logs.append(entry)
        write_json(self.command_log_path, {"commands": self.logs})
        return result

    def artifact_script(self) -> str:
        return str(script_dir() / "build_run_artifacts.py")

    def subagent_script(self) -> str:
        return str(script_dir() / "run_subagent.py")

    def main_evidence_script(self) -> str:
        return str(script_dir() / "collect_main_evidence.py")

    def order_execution_script(self) -> str:
        return str(script_dir() / "execute_orders.py")

    def telegram_summary_script(self) -> str:
        return str(script_dir() / "render_telegram_summary.py")

    def portfolio_script_candidates(self) -> list[Path]:
        return [
            self.repo_root / "containers/codex-exec/profiles/base/skills/check-portfolio/scripts/read_portfolio.sh",
            self.repo_root / "containers/codex-exec/shared-skills/check-portfolio/scripts/read_portfolio.sh",
            Path("/app/skills/check-portfolio/scripts/read_portfolio.sh"),
        ]

    def optional_cache_filename(self, domain: str) -> str:
        date = self.args.date or now_kst().strftime("%Y-%m-%d")
        if domain == "financial":
            return f"financial-{date}.yaml"
        return f"news-{date}.yaml"

    def expected_cache_path(self, domain: str) -> Path:
        return self.optional_cache_candidate_paths(domain)[0]

    def optional_cache_candidate_paths(self, domain: str) -> list[Path]:
        filename = self.optional_cache_filename(domain)
        paths: list[Path] = []
        if domain == "financial":
            configured = os.environ.get("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR")
            subdir = "collect-financial-information"
        else:
            configured = os.environ.get("COLLECT_NEWS_INFORMATION_MEMORY_DIR")
            subdir = "collect-news-information"
        if configured:
            paths.append(Path(configured).expanduser() / filename)
        memory_root = os.environ.get("DAILY_TRADING_MEMORY_DIR")
        if memory_root:
            paths.append(Path(memory_root).expanduser() / subdir / filename)
        paths.append(self.workspace_dir / "memory" / subdir / filename)
        return paths

    def default_cache_path(self, domain: str) -> str:
        for path in self.optional_cache_candidate_paths(domain):
            if path.exists():
                return str(path)
        return ""

    def optional_cache_script_candidates(self, domain: str) -> list[Path]:
        if domain == "financial":
            skill_name = "collect-financial-information"
            script_name = "financial_cache.py"
        else:
            skill_name = "collect-news-information"
            script_name = "news_cache.py"

        candidates = [
            Path("/app/skills") / skill_name / "scripts" / script_name,
            Path("/codex-home/skills") / skill_name / "scripts" / script_name,
        ]
        codex_home = os.environ.get("CODEX_HOME")
        if codex_home:
            candidates.insert(1, Path(codex_home).expanduser() / "skills" / skill_name / "scripts" / script_name)
        return candidates

    def optional_cache_script(self, domain: str) -> Path | None:
        for path in self.optional_cache_script_candidates(domain):
            if path.exists():
                return path
        return None

    def etf_or_etn_symbol_ids(self) -> list[str]:
        price = load_json_if_exists(self.output_dir / "price-chart.json")
        if not isinstance(price, dict):
            return []
        result: list[str] = []
        for item in price.get("symbols", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("product_type") or "").lower() not in {"etf", "etn"}:
                continue
            symbol_id = str(item.get("symbol_id") or "").strip()
            if symbol_id:
                result.append(symbol_id)
        return result

    def cache_has_etf_nav_evidence(self, path: Path, etf_symbols: list[str]) -> bool:
        payload = load_yaml_if_exists(path)
        if not isinstance(payload, dict):
            return False
        symbols = payload.get("symbols")
        if not isinstance(symbols, dict):
            return False

        def contains_key(value: Any, wanted: str) -> bool:
            if isinstance(value, dict):
                if wanted in value:
                    return True
                return any(contains_key(child, wanted) for child in value.values())
            if isinstance(value, list):
                return any(contains_key(child, wanted) for child in value)
            return False

        for symbol_id in etf_symbols:
            symbol_payload = symbols.get(symbol_id)
            if not contains_key(symbol_payload, "ETF/ETN 현재가") or not contains_key(symbol_payload, "NAV 비교추이(종목)"):
                return False
        return True

    def covered_cache_path(self, domain: str, path_text: str, symbols: list[str], *, detail: str) -> str:
        path = resolve_workspace_path(self.workspace_dir, path_text)
        covered, missing = cache_coverage(path, symbols)
        if covered:
            etf_symbols = self.etf_or_etn_symbol_ids() if domain == "financial" else []
            if etf_symbols and not self.cache_has_etf_nav_evidence(path, etf_symbols):
                self.logs.append(
                    {
                        "stage": f"{domain}-cache-coverage",
                        "path": str(path),
                        "missing_etf_nav_symbol_count": len(etf_symbols),
                        "missing_etf_nav_symbols_sample": etf_symbols[:20],
                        "recorded_at": now_iso(),
                    }
                )
                write_json(self.command_log_path, {"commands": self.logs})
                return ""
            self.add_stage(f"{domain}-cache", "success", detail=detail, required=False, path=path)
            return str(path)
        self.logs.append(
            {
                "stage": f"{domain}-cache-coverage",
                "path": str(path),
                "missing_symbol_count": len(missing),
                "missing_symbols_sample": missing[:20],
                "recorded_at": now_iso(),
            }
        )
        write_json(self.command_log_path, {"commands": self.logs})
        return ""

    def parse_cache_collect_path(self, stdout: str) -> str:
        text = stdout.strip()
        if not text:
            return ""
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                path_text = str(payload.get("path") or payload.get("cache_path") or "").strip()
                if path_text:
                    return path_text
        return text.splitlines()[-1].strip()

    def parse_existing_cache_path(self, stdout: str) -> str:
        path_text = self.parse_cache_collect_path(stdout)
        if not path_text:
            return ""
        path = resolve_workspace_path(self.workspace_dir, path_text)
        return str(path) if path.exists() else ""

    def first_existing_cache_path(self, paths: list[Path], symbols: list[str]) -> Path | None:
        wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.exists() and (cache_symbol_keys(resolved) & wanted):
                return resolved
        return None

    def first_existing_cache_file_path(self, paths: list[Path]) -> Path | None:
        seen: set[Path] = set()
        for path in paths:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if resolved.exists():
                return resolved
        return None

    def zero_usable_news_cache_path(self, paths: list[Path], symbols: list[str]) -> Path | None:
        path = self.first_existing_cache_file_path(paths)
        if path is None:
            return None
        counts = cache_evidence_counts(path, symbols)
        if counts["wanted_symbol_count"] > 0 and counts["usable_symbol_count"] == 0:
            return path
        return None

    def resolve_portfolio(self) -> tuple[dict[str, Any], Path]:
        output_path = self.output_dir / "check-portfolio.json"
        if self.args.portfolio_json:
            source = resolve_workspace_path(self.workspace_dir, self.args.portfolio_json)
            portfolio = load_json(source)
            write_json(output_path, portfolio)
            self.add_stage("check-portfolio", "success", detail="loaded provided JSON", path=output_path)
            return portfolio, output_path

        for script in self.portfolio_script_candidates():
            if not script.exists():
                continue
            result = self.run_cmd("check-portfolio", [str(script)])
            if result.returncode != 0:
                self.add_stage("check-portfolio", "failed", detail="portfolio command failed", path=self.command_log_path)
                raise RuntimeError("check-portfolio command failed")
            try:
                portfolio = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                self.add_stage("check-portfolio", "failed", detail=f"invalid portfolio JSON: {exc}", path=self.command_log_path)
                raise
            write_json(output_path, portfolio)
            self.add_stage("check-portfolio", "success", detail=f"loaded via {script}", path=output_path)
            return portfolio, output_path

        self.add_stage("check-portfolio", "failed", detail="no check-portfolio script found")
        raise RuntimeError("no check-portfolio script found; pass --portfolio-json")

    def collect_main_evidence(self, symbols: list[str]) -> None:
        price_path = self.output_dir / "price-chart.json"
        account_path = self.output_dir / "account-before-order.json"
        if self.args.reuse_existing_artifacts and price_path.exists() and account_path.exists():
            self.add_stage("main-evidence", "success", detail="reused existing price/account artifacts", path=self.output_dir)
            return

        cmd = [
            sys.executable,
            self.main_evidence_script(),
            "collect",
            "--run-id",
            self.run_id,
            "--started-at",
            self.started_at,
            "--symbols",
            ",".join(symbols),
            "--output-dir",
            str(self.output_dir),
            "--env",
            self.args.env,
            "--request-type",
            self.args.request_type,
        ]
        if self.args.skip_account:
            cmd.append("--skip-account")
        result = self.run_cmd("main-evidence", cmd)
        price = load_json_if_exists(price_path)
        if result.returncode == 0 and isinstance(price, dict) and price.get("status") != "failed":
            self.add_stage("main-evidence", "success", detail="collected price/account artifacts", path=self.output_dir)
            return
        self.add_stage("main-evidence", "failed", detail="required price/account collection failed", path=self.command_log_path)
        raise RuntimeError("main evidence collection failed")

    def collect_optional_cache(self, domain: str, symbols: list[str]) -> str:
        configured = self.args.financial_cache_path if domain == "financial" else self.args.news_cache_path
        candidate_paths: list[Path] = []
        if configured:
            configured_path = resolve_workspace_path(self.workspace_dir, configured)
            candidate_paths.append(configured_path)
            covered = self.covered_cache_path(domain, configured, symbols, detail="using provided full-universe cache path")
            if covered:
                return covered

        for path in self.optional_cache_candidate_paths(domain):
            if path not in candidate_paths:
                candidate_paths.append(path)
        default_path = self.default_cache_path(domain)
        if default_path:
            covered = self.covered_cache_path(domain, default_path, symbols, detail="using existing same-date full-universe memory cache")
            if covered:
                return covered

        cache_script = self.optional_cache_script(domain)
        if cache_script is None:
            partial = self.first_existing_cache_path(candidate_paths, symbols)
            if partial:
                self.add_stage(f"{domain}-cache", "partial", detail="optional cache script not found; using existing incomplete cache", required=False, path=partial)
                return str(partial)
            zero_news = self.zero_usable_news_cache_path(candidate_paths, symbols) if domain == "news" else None
            if zero_news:
                self.add_stage(
                    "news-cache",
                    "partial",
                    detail="optional news cache script not found; cache exists but zero usable articles",
                    required=False,
                    path=zero_news,
                )
                return str(zero_news)
            self.add_stage(f"{domain}-cache", "skipped", detail="optional cache script not found", required=False)
            return ""
        date = self.args.date or now_kst().strftime("%Y-%m-%d")
        get_cmd = [sys.executable, str(cache_script), "get", "--date", date]
        get_result = self.run_cmd(f"{domain}-cache-get", get_cmd, required=False)
        get_path = self.parse_existing_cache_path(get_result.stdout)
        if get_path:
            get_cache_path = resolve_workspace_path(self.workspace_dir, get_path)
            if get_cache_path not in candidate_paths:
                candidate_paths.insert(0, get_cache_path)
            covered = self.covered_cache_path(domain, get_path, symbols, detail="using get-returned same-date full-universe cache")
            if covered:
                return covered
        cmd = [
            sys.executable,
            str(cache_script),
            "collect",
            "--date",
            date,
            "--symbols",
            ",".join(symbols),
        ]
        if domain == "financial" and self.has_etf_or_etn_price_rows():
            cmd.append("--include-etf")
        result = self.run_cmd(f"{domain}-cache-collect", cmd, required=False)
        if result.returncode == 0:
            path_text = self.parse_cache_collect_path(result.stdout)
            if path_text:
                collected_path = resolve_workspace_path(self.workspace_dir, path_text)
                candidate_paths.insert(0, collected_path)
            else:
                collected_path = None
            second_get_result = self.run_cmd(f"{domain}-cache-get", get_cmd, required=False)
            second_get_path = self.parse_existing_cache_path(second_get_result.stdout)
            if second_get_path:
                second_path = resolve_workspace_path(self.workspace_dir, second_get_path)
                candidate_paths.insert(0, second_path)
            covered = self.covered_cache_path(domain, second_get_path, symbols, detail="optional cache collected once and get-returned cache covers full universe") if second_get_path else ""
            if not covered and collected_path:
                covered = self.covered_cache_path(domain, str(collected_path), symbols, detail="optional cache collected once and covers full universe")
            if covered:
                return covered
            partial = self.first_existing_cache_path(candidate_paths, symbols)
            if partial:
                self.add_stage(f"{domain}-cache", "partial", detail="optional cache collected once but still missing universe symbols; using partial cache", required=False, path=partial)
                return str(partial)
            zero_news = self.zero_usable_news_cache_path(candidate_paths, symbols) if domain == "news" else None
            if zero_news:
                self.add_stage(
                    "news-cache",
                    "partial",
                    detail="optional news cache collected once; cache exists but zero usable articles",
                    required=False,
                    path=zero_news,
                )
                return str(zero_news)
            self.add_stage(f"{domain}-cache", "partial", detail="optional cache collected once but no cache file was produced", required=False)
            return ""
        partial = self.first_existing_cache_path(candidate_paths, symbols)
        if partial:
            self.add_stage(f"{domain}-cache", "partial", detail="optional cache collection failed once; using existing incomplete cache", required=False, path=partial)
            return str(partial)
        zero_news = self.zero_usable_news_cache_path(candidate_paths, symbols) if domain == "news" else None
        if zero_news:
            self.add_stage(
                "news-cache",
                "partial",
                detail="optional news cache collection failed once; cache exists but zero usable articles",
                required=False,
                path=zero_news,
            )
            return str(zero_news)
        self.add_stage(f"{domain}-cache", "partial", detail="optional cache collection failed once", required=False, path=self.command_log_path)
        return ""

    def has_etf_or_etn_price_rows(self) -> bool:
        return bool(self.etf_or_etn_symbol_ids())

    def run_artifact_command(self, stage: str, args: list[str], *, required: bool = True) -> dict[str, Any] | None:
        result = self.run_cmd(stage, [sys.executable, self.artifact_script(), *args], required=required)
        if result.returncode != 0:
            self.add_stage(stage, "failed" if required else "partial", detail="artifact helper failed", required=required, path=self.command_log_path)
            if required:
                raise RuntimeError(f"{stage} failed")
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def run_first_verdicts(self) -> None:
        specs_path = self.output_dir / "first-verdict-specs.json"
        result = self.run_cmd(
            "first-verdict",
            [sys.executable, self.subagent_script(), "run-group", "--spec", str(specs_path), "--max-workers", str(self.args.max_workers)],
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {}
        if result.returncode == 0 and payload.get("status") == "success":
            self.add_stage("first-verdict", "success", detail="all first-verdict wrappers succeeded", path=self.output_dir / "subagents")
            return
        self.add_stage("first-verdict", "failed", detail="required first-verdict wrapper failed", path=self.command_log_path)
        raise RuntimeError("first-verdict failed")

    def run_second_verdict(self) -> None:
        spec_path = self.output_dir / "second-verdict-spec.json"
        spec = load_json(spec_path)
        if not normalize_symbol_ids(spec.get("symbol_ids")):
            write_json(
                self.output_dir / "verdict-second.json",
                {
                    "schema_version": "1",
                    "run_id": self.run_id,
                    "started_at": self.started_at,
                    "generated_at": now_iso(),
                    "stage": "verdict-second",
                    "status": "success",
                    "skipped": True,
                    "skip_reason": "no selected symbols",
                    "errors": [],
                    "symbols": [],
                },
            )
            self.add_stage("second-verdict", "skipped", detail="no selected symbols", required=False, path=self.output_dir / "verdict-second.json")
            return
        last_detail = "required second-verdict wrapper failed"
        for attempt in range(1, 4):
            result = self.run_cmd("second-verdict", [sys.executable, self.subagent_script(), "run-one", "--spec", str(spec_path)])
            try:
                wrapper = json.loads(result.stdout)
            except json.JSONDecodeError:
                wrapper = {}
            if result.returncode == 0 and wrapper.get("status") == "success":
                self.write_verdict_second(wrapper)
                detail = "second-verdict wrapper merged" if attempt == 1 else f"second-verdict wrapper merged after retry {attempt - 1}"
                self.add_stage("second-verdict", "success", detail=detail, path=self.output_dir / "verdict-second.json")
                return
            last_detail = f"required second-verdict wrapper failed on attempt {attempt}"
        self.add_stage("second-verdict", "failed", detail=last_detail, path=self.command_log_path)
        raise RuntimeError("second-verdict failed")

    def run_order_execution(self) -> dict[str, Any]:
        result = self.run_cmd(
            "order-execution",
            [
                sys.executable,
                self.order_execution_script(),
                "run",
                "--output-dir",
                str(self.output_dir),
                "--env",
                self.args.env,
                "--submit",
            ],
        )
        if result.returncode != 0:
            self.add_stage("order-execution", "failed", detail=compact_text(result.stderr or result.stdout), path=self.output_dir / "execution.json")
            raise RuntimeError("order-execution failed")
        execution = load_json(self.output_dir / "execution.json")
        status = str(execution.get("status") or "")
        stage_status = "success" if status == "success" else "partial" if status == "partial" else "failed"
        self.add_stage("order-execution", stage_status, detail=f"status={status}", path=self.output_dir / "execution.json")
        if stage_status == "failed":
            raise RuntimeError("order-execution failed")
        return execution

    def write_verdict_second(self, wrapper: dict[str, Any]) -> None:
        parsed = wrapper.get("parsed_json") if isinstance(wrapper.get("parsed_json"), dict) else {}
        symbols: list[dict[str, Any]] = []
        for item in parsed.get("symbols", []):
            if not isinstance(item, dict):
                continue
            symbol_id = symbol_key(item)
            if not symbol_id:
                continue
            symbols.append(
                {
                    "symbol_id": symbol_id,
                    "symbol_name": item.get("symbol_name") or symbol_id,
                    "target_holding_quantity": max(0, as_int(item.get("target_holding_quantity"))),
                    "relative_attractiveness_rank": as_int(item.get("relative_attractiveness_rank")),
                    "reason_code": safe_name(str(item.get("reason_code") or "hold_neutral")).lower(),
                    "one_line_reason": str(item.get("one_line_reason") or "")[:300],
                }
            )
        artifact = {
            "schema_version": "1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "generated_at": now_iso(),
            "stage": "verdict-second",
            "status": "success" if symbols else "partial",
            "skipped": False,
            "skip_reason": "",
            "errors": wrapper.get("errors") if isinstance(wrapper.get("errors"), list) else [],
            "symbols": symbols,
        }
        write_json(self.output_dir / "verdict-second.json", artifact)
        self.write_second_sidecar(str(wrapper.get("agent_role") or "judge-midterm"), str(wrapper.get("task_name") or "second-judge-midterm"), symbols)

    def write_second_sidecar(self, role: str, task_name: str, symbols: list[dict[str, Any]]) -> None:
        path = self.output_dir / "verdicts" / f"second-verdict--{safe_name(role)}--{safe_name(task_name)}.md"
        lines = [
            "| 종목 | 목표수량 | 상대매력도 | 판단코드 | 의견(판단) |",
            "|---|---:|---:|---|---|",
        ]
        for item in symbols:
            symbol_name = f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip()
            lines.append(
                f"| {symbol_name} | {as_int(item.get('target_holding_quantity'))} | {as_int(item.get('relative_attractiveness_rank'))} | {item.get('reason_code', '')} | {item.get('one_line_reason', '')} |"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def report_path(self) -> Path:
        return self.workspace_dir / "reports" / f"{report_date_from(self.started_at)}_포트폴리오.md"

    def load_summary_stages(self) -> list[dict[str, Any]]:
        if self.stages:
            return self.stages
        run = load_json_if_exists(self.run_path) or {}
        stages = run.get("stages") if isinstance(run, dict) else []
        return stages if isinstance(stages, list) else []

    def load_portfolio_for_summary(self) -> dict[str, Any]:
        if self.args.portfolio_json:
            path = resolve_workspace_path(self.workspace_dir, self.args.portfolio_json)
            payload = load_json_if_exists(path)
            if isinstance(payload, dict):
                return payload
        payload = load_json_if_exists(self.output_dir / "check-portfolio.json")
        return payload if isinstance(payload, dict) else {}

    def build_account_display_summary(self, account_summary: dict[str, Any]) -> dict[str, Any]:
        return {
            "cash_amount": account_summary.get("cash_amount"),
            "securities_valuation_amount": account_summary.get("securities_valuation_amount"),
            "total_evaluation_amount": account_summary.get("total_evaluation_amount"),
            "total_pnl_amount": account_summary.get("total_pnl_amount"),
            "today_trade_amounts": {
                "buy_amount": account_summary.get("today_buy_amount"),
                "sell_amount": account_summary.get("today_sell_amount"),
                "display_policy": "Do not mix these cumulative same-day amounts into the main account state; show only if explicitly useful as 당일 거래 누계.",
            },
        }

    def build_evidence_summary(self, decision_brief: dict[str, Any], stages: list[dict[str, Any]], symbols: list[str]) -> dict[str, Any]:
        stage_by_name = {item.get("stage"): item for item in stages if isinstance(item, dict)}
        decision_symbols = decision_brief.get("symbols") if isinstance(decision_brief.get("symbols"), list) else []
        financial_supplied = 0
        news_with_articles = 0
        price_only = 0
        for item in decision_symbols:
            if not isinstance(item, dict):
                continue
            financial_summary = item.get("financial_summary") if isinstance(item.get("financial_summary"), dict) else {}
            if financial_summary.get("cache_status") == "supplied":
                financial_supplied += 1
            news_summary = item.get("news_summary") if isinstance(item.get("news_summary"), list) else []
            if news_summary:
                news_with_articles += 1
            if item.get("evidence_mode") == "price-only":
                price_only += 1

        summary: dict[str, Any] = {
            "symbol_count": len(symbols),
            "price_only_symbol_count": price_only,
            "financial": {
                "status": "supplied" if financial_supplied else "not_supplied",
                "symbol_count_with_summary": financial_supplied,
                "display_text": f"재무: {financial_supplied}개 종목 반영" if financial_supplied else "재무: 반영된 요약 없음",
            },
            "news": {
                "status": "supplied" if news_with_articles else "not_supplied",
                "symbol_count_with_articles": news_with_articles,
                "display_text": f"뉴스: {news_with_articles}개 종목 기사 반영" if news_with_articles else "뉴스: 반영된 기사 없음",
            },
        }
        for domain in ("financial", "news"):
            stage = stage_by_name.get(f"{domain}-cache")
            if not isinstance(stage, dict):
                continue
            path_text = str(stage.get("path") or "").strip()
            domain_summary = summary[domain]
            domain_summary["cache_stage_status"] = stage.get("status")
            domain_summary["cache_stage_detail"] = stage.get("detail")
            if path_text:
                domain_summary["cache_path"] = path_text
                path = resolve_workspace_path(self.workspace_dir, path_text)
                if path.exists():
                    domain_summary["cache_counts"] = cache_evidence_counts(path, symbols)
            if domain == "news":
                counts = domain_summary.get("cache_counts") if isinstance(domain_summary.get("cache_counts"), dict) else {}
                if counts and as_int(counts.get("usable_symbol_count")) == 0:
                    domain_summary["status"] = "cache_exists_zero_usable_articles"
                    domain_summary["display_text"] = "뉴스: 캐시 파일은 있으나 사용 가능한 기사 0건"
                elif not path_text:
                    domain_summary["status"] = "cache_missing"
                    domain_summary["display_text"] = "뉴스: 캐시 파일 없음"
                elif news_with_articles and news_with_articles < len(symbols):
                    domain_summary["status"] = "partial"
                    domain_summary["display_text"] = f"뉴스: {news_with_articles}개 종목 기사 반영, 일부 종목 기사 없음"
            elif domain == "financial":
                if not path_text:
                    domain_summary["status"] = "cache_missing"
                    domain_summary["display_text"] = "재무: 캐시 파일 없음"
                elif financial_supplied and financial_supplied < len(symbols):
                    domain_summary["status"] = "partial"
                    domain_summary["display_text"] = f"재무: {financial_supplied}개 종목 반영, 일부 종목 요약 없음"
        return summary

    def adopt_existing_run_identity(self) -> None:
        run = load_json_if_exists(self.run_path) or {}
        if not isinstance(run, dict):
            return
        self.run_id = str(run.get("run_id") or self.run_id)
        self.started_at = str(run.get("started_at") or self.started_at)

    def build_verdict_summary(self, account: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
        verdict_second = load_json_if_exists(self.output_dir / "verdict-second.json") or {}
        account_by_symbol = {symbol_key(item): item for item in account.get("symbols", []) if isinstance(item, dict)}
        execution_by_symbol = {symbol_key(item): item for item in execution.get("orders", []) if isinstance(item, dict)}
        rows: list[dict[str, Any]] = []
        for item in verdict_second.get("symbols", []) if isinstance(verdict_second, dict) else []:
            if not isinstance(item, dict):
                continue
            symbol_id = symbol_key(item)
            if not symbol_id:
                continue
            account_item = account_by_symbol.get(symbol_id, {})
            execution_item = execution_by_symbol.get(symbol_id, {})
            current_qty = as_int(account_item.get("current_live_holding_quantity"))
            target_qty = as_int(item.get("target_holding_quantity"))
            delta = target_qty - current_qty
            rows.append(
                {
                    "symbol_id": symbol_id,
                    "symbol_name": item.get("symbol_name") or account_item.get("symbol_name") or symbol_id,
                    "current_live_holding_quantity": current_qty,
                    "target_holding_quantity": target_qty,
                    "delta_quantity": delta,
                    "relative_attractiveness_rank": as_int(item.get("relative_attractiveness_rank")),
                    "reason_code": item.get("reason_code") or "",
                    "one_line_reason": item.get("one_line_reason") or "",
                    "order_result": execution_item.get("result") or "",
                    "order_direction": execution_item.get("direction") or "none",
                    "order_quantity": as_int(execution_item.get("validated_order_quantity")),
                    "order_or_reservation_id": execution_item.get("order_or_reservation_id") or "",
                }
            )
        submitted = [item for item in rows if item.get("order_result") == "submitted"]
        return {
            "status": verdict_second.get("status"),
            "symbol_count": len(rows),
            "submitted_order_count": len(submitted),
            "symbols": rows,
        }

    def write_portfolio_report(self, summary: dict[str, Any]) -> Path:
        path = self.report_path()
        account = summary.get("account_summary") if isinstance(summary.get("account_summary"), dict) else {}
        execution = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
        verdict = summary.get("verdict_summary") if isinstance(summary.get("verdict_summary"), dict) else {}
        decision = summary.get("decision_brief") if isinstance(summary.get("decision_brief"), dict) else {}
        token_total = (((summary.get("token_usage") or {}).get("total") or {}).get("total_tokens")) if isinstance(summary.get("token_usage"), dict) else 0
        decision_brief = load_json_if_exists(self.output_dir / "decision-brief.json") or {}
        verdict_first = load_json_if_exists(self.output_dir / "verdict-first.json") or {}
        execution_full = load_json_if_exists(self.output_dir / "execution.json") or {}
        account_full = load_json_if_exists(self.output_dir / "account-before-order.json") or {}
        price_chart = load_json_if_exists(self.output_dir / "price-chart.json") or {}
        evidence_summary = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
        stages = summary.get("stages") if isinstance(summary.get("stages"), list) else []
        stage_by_name = {item.get("stage"): item for item in stages if isinstance(item, dict)}
        active_order_lookup_performed = account_full.get("active_order_lookup_performed")
        order_available_lookup_performed = account_full.get("order_available_lookup_performed")
        active_order_count_text = (
            f"{len(account_full.get('active_orders', [])) if isinstance(account_full.get('active_orders'), list) else 0}건"
            if active_order_lookup_performed is True
            else "미조회"
        )
        order_reservation_check = (
            account_full.get("active_order_checks", {}).get("order_resv_ccnl", "")
            if isinstance(account_full.get("active_order_checks"), dict)
            else ""
        )
        if active_order_lookup_performed is not True and not order_reservation_check:
            order_reservation_check = "미조회"

        lines = [
            f"# 포트폴리오 평결문 - {report_date_from(self.started_at)}",
            "",
            "## 실행 정보",
            f"- run_id: {summary.get('run_id', '')}",
            f"- 작업 시작: {summary.get('started_at', '')}",
            f"- 환경: {account_full.get('execution_environment') or self.args.env}",
            f"- 최종 상태: {summary.get('status', '')}",
            f"- 실행 디렉터리: {summary.get('run_dir', '')}",
            "",
            "## 1. 수집 상태",
            "| 도메인 | 상태 | 전체 종목 수 | 오류 종목 수 | 핵심 오류 |",
            "|---|---|---:|---:|---|",
        ]
        for domain, stage_name in (("시장", "main-evidence"), ("재무", "financial-cache"), ("뉴스", "news-cache")):
            stage = stage_by_name.get(stage_name, {})
            detail = stage.get("detail", "")
            error_count = 0
            if domain == "시장":
                error_count = count_symbol_errors(price_chart)
            elif domain in {"재무", "뉴스"}:
                domain_summary = evidence_summary.get("financial" if domain == "재무" else "news")
                counts = domain_summary.get("cache_counts") if isinstance(domain_summary, dict) and isinstance(domain_summary.get("cache_counts"), dict) else {}
                error_count = as_int(counts.get("missing_usable_symbol_count"))
            if domain == "재무":
                detail = ((evidence_summary.get("financial") or {}).get("display_text") if isinstance(evidence_summary.get("financial"), dict) else "") or detail
            elif domain == "뉴스":
                detail = ((evidence_summary.get("news") or {}).get("display_text") if isinstance(evidence_summary.get("news"), dict) else "") or detail
            lines.append(
                f"| {domain} | {stage.get('status', '')} | {(summary.get('portfolio_counts') or {}).get('universe', 0)} | {error_count} | {md_cell(detail)} |"
            )

        lines.extend(
            [
                "",
                "## 2. 평결 제외 종목",
                "| 종목식별자 | 종목명 | 제외 사유 | 누락 필수 정보 |",
                "|---|---|---|---|",
            ]
        )
        excluded_count = 0
        for item in decision_brief.get("symbols", []) if isinstance(decision_brief, dict) else []:
            if not isinstance(item, dict) or item.get("eligible_for_verdict", True):
                continue
            excluded_count += 1
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | "
                f"{md_cell(', '.join(item.get('exclusion_reasons', [])) if isinstance(item.get('exclusion_reasons'), list) else item.get('exclusion_reasons'))} | "
                f"{md_cell(', '.join(item.get('required_missing', [])) if isinstance(item.get('required_missing'), list) else item.get('required_missing'))} |"
            )
        if excluded_count == 0:
            lines.append("| - | - | 없음 | - |")

        price_only_count = 0
        for item in decision_brief.get("symbols", []) if isinstance(decision_brief, dict) else []:
            if isinstance(item, dict) and item.get("evidence_mode") == "price-only":
                price_only_count += 1
        lines.extend(
            [
                "",
                "## 3. `decision-brief.json` 요약",
                f"- `decision-brief.json` 생성 여부: {'yes' if decision else 'no'}",
                f"- 포함된 eligible 종목 수: {sum(1 for item in decision_brief.get('symbols', []) if isinstance(item, dict) and item.get('eligible_for_verdict', False)) if isinstance(decision_brief, dict) else 0}",
                f"- price-only eligible 종목 수: {price_only_count}",
                "- 제외된 raw payload / 기사 원문 / 민감정보: yes",
                f"- 핵심 누락 또는 오류: {decision.get('error_count', 0)}건",
                "",
                "## 4. `first-verdict` 독립 평결",
                "| 종목식별자 | 종목명 | 원점수 평균(0-10) | 확신도 보정 최종점수(0-10) | 유효 응답 수 | 핵심 근거 | 핵심 리스크 |",
                "|---|---|---:|---:|---:|---|---|",
            ]
        )
        for item in verdict_first.get("symbols", []) if isinstance(verdict_first, dict) else []:
            if not isinstance(item, dict):
                continue
            agent_scores = item.get("agent_scores") if isinstance(item.get("agent_scores"), list) else []
            reasons = [str(score.get("one_line_reason", "")) for score in agent_scores if isinstance(score, dict) and score.get("one_line_reason")]
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {item.get('mean_score', '')} | "
                f"{item.get('mean_confidence_adjusted_score', '')} | {len(agent_scores)} | {md_cell('; '.join(reasons[:2]))} | - |"
            )

        lines.extend(
            [
                "",
                "## 5. `second-verdict` 포트폴리오 평결",
                "- 중기 시장 판단: `judge-midterm` 목표수량 결과 사용",
                "- 잔여 현금 처리: 목표현금을 별도 판단값으로 만들지 않고 목표수량 충족 후 남는 금액으로만 기록",
                f"- Main agent 검증 결과: {execution.get('status', '')}",
                "",
                "| 종목식별자 | 종목명 | 현재 보유수량 | 목표 보유수량 | 상대매력도 | 판단 코드 | 한 줄 판단 |",
                "|---|---|---:|---:|---:|---|---|",
            ]
        )
        for item in verdict.get("symbols", []) if isinstance(verdict.get("symbols"), list) else []:
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {as_int(item.get('current_live_holding_quantity'))} | "
                f"{as_int(item.get('target_holding_quantity'))} | {as_int(item.get('relative_attractiveness_rank'))} | "
                f"{md_cell(item.get('reason_code'))} | {md_cell(item.get('one_line_reason'))} |"
            )

        submitted_orders = [item for item in execution.get("orders", []) if isinstance(item, dict) and item.get("result") == "submitted"]
        lines.extend(
            [
                "",
                "## 6. 최신 계좌 검증",
                f"- 총자산: {format_number(account.get('total_evaluation_amount'))}원",
                f"- 현금 또는 주문가능금액: {format_number(account.get('cash_amount'))}원",
                f"- 주식평가: {format_number(account.get('securities_valuation_amount'))}원",
                f"- 평가손익: {format_signed_number(account.get('total_pnl_amount'))}원",
                f"- 주문 전 기존 미체결/예약 주문 조회: {bool_status(active_order_lookup_performed)}",
                f"- 주문가능 조회: {bool_status(order_available_lookup_performed)}",
                f"- 주문 전 기존 미체결/예약 주문: {active_order_count_text}",
                f"- 예약 주문 확인: {order_reservation_check}",
                "- 당일 체결: 계좌 요약에 반영된 스냅샷 기준",
                "",
                "## 7. 주문 전 기존 미체결/예약 주문 조정",
                "| 종목식별자 | 종목명 | 기존 주문번호 | 구분 | 방향 | 잔여수량 | 가격 | 주문 API | 경로 | 조치 | 사유 | 결과 | 확인 상태 | 대체 주문번호 |",
                "|---|---|---|---|---|---:|---:|---|---|---|---|---|---|---|",
            ]
        )
        adjustments = execution_full.get("order_adjustments") if isinstance(execution_full.get("order_adjustments"), list) else []
        if adjustments:
            for item in adjustments:
                if not isinstance(item, dict):
                    continue
                lines.append(
                    f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {md_cell(item.get('existing_order_id'))} | "
                    f"{md_cell(item.get('existing_order_kind'))} | {md_cell(item.get('direction'))} | {as_int(item.get('remaining_quantity'))} | "
                    f"{as_int(item.get('order_price'))} | {md_cell(item.get('order_api'))} | {md_cell(item.get('order_path'))} | "
                    f"{md_cell(item.get('action'))} | {md_cell(item.get('reason'))} | {md_cell(item.get('result'))} | "
                    f"{md_cell(item.get('confirmed_status'))} | {md_cell(item.get('replacement_order_id'))} |"
                )
        else:
            if active_order_lookup_performed is True:
                lines.append("| - | - | - | - | - | 0 | 0 | - | - | none | 기존 조정 대상 없음 | skipped | - | - |")
            else:
                lines.append("| - | - | - | - | - | 0 | 0 | - | - | refresh_required | 주문 전 기존 미체결/예약 주문 미조회 | blocked_until_refreshed | 미조회 | - |")

        lines.extend(
            [
                "",
                "## 8. 최종 주문 목록",
                "| 종목식별자 | 종목명 | 방향 | 현재 실시간 보유수량 | 미체결·예약 매수 | 미체결·예약 매도 | 예상 보유수량 | 목표 보유수량 | 추가 필요수량 | 결과 |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for item in execution_full.get("orders", []) if isinstance(execution_full, dict) else []:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"| {md_cell(item.get('symbol_id'))} | {md_cell(item.get('symbol_name'))} | {md_cell(item.get('direction'))} | "
                f"{as_int(item.get('current_live_holding_quantity'))} | {as_int(item.get('pending_and_reserved_buy_quantity'))} | "
                f"{as_int(item.get('pending_and_reserved_sell_quantity'))} | {as_int(item.get('expected_holding_quantity'))} | "
                f"{as_int(item.get('target_holding_quantity'))} | {as_int(item.get('additional_required_quantity'))} | {md_cell(item.get('result'))} |"
            )

        lines.extend(
            [
                "",
                "## 9. 실행 결과",
                f"- 요청 유형: {execution.get('request_type', '')}",
                f"- 실제 제출 여부: {'yes' if submitted_orders else 'no'}",
                f"- 제출된 주문번호 또는 예약번호: {', '.join(str(item.get('order_or_reservation_id') or '') for item in submitted_orders if item.get('order_or_reservation_id')) or '-'}",
                "- 취소/정정 요청번호: -",
                "- 취소/정정 확인 상태: -",
                f"- 실패 또는 보류 사유: {md_cell('; '.join(error.get('message', '') for error in execution.get('errors', []) if isinstance(error, dict))) if isinstance(execution.get('errors'), list) else '-'}",
                "| 종목 | 방향 | 수량 | 결과 | 사유 | 예약/주문번호 |",
                "|---|---|---:|---|---|---|",
            ]
        )
        for item in submitted_orders:
            symbol_name = md_cell(f"{item.get('symbol_id', '')} {item.get('symbol_name', '')}".strip())
            lines.append(
                f"| {symbol_name} | {md_cell(item.get('direction'))} | {as_int(item.get('quantity'))} | {md_cell(item.get('result'))} | "
                f"{md_cell(item.get('reason'))} | {md_cell(item.get('order_or_reservation_id') or '-')} |"
            )
        lines.extend(
            [
                "",
                "## 10. 아티팩트",
                f"- 실행 디렉터리: {summary.get('run_dir', '')}",
                f"- 보존된 partial / failed 아티팩트: {sum(1 for item in stages if isinstance(item, dict) and item.get('status') in {'partial', 'failed'})}",
                f"- pipeline-summary.json: {summary.get('summary_path', '')}",
                f"- decision-brief.json: {(summary.get('artifacts') or {}).get('decision_brief', '')}",
                f"- verdict-second.json: {(summary.get('artifacts') or {}).get('verdict_second', '')}",
                f"- execution.json: {(summary.get('artifacts') or {}).get('execution', '')}",
                f"- 총 사용 토큰: {format_number(token_total)}",
                "",
                "## 11. 메모",
                "- 당일 체결수량은 현재 보유수량에 이미 반영된 값으로 보고 다시 차감하지 않음",
                "- `second-verdict`는 단일 `judge-midterm` 목표수량을 사용하며 deterministic helper와 `execute_orders.py`가 총자산/주문가능금액/집중도/active 주문/same-day/account-order gate를 검증함",
                "- 투자 권유가 아니라 의사결정 보조 분석입니다.",
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def write_telegram_summary(self) -> Path:
        path = self.output_dir / "telegram-summary.txt"
        result = self.run_cmd(
            "telegram-summary",
            [
                sys.executable,
                self.telegram_summary_script(),
                "--summary",
                str(self.summary_path),
                "--output",
                str(path),
            ],
            required=False,
        )
        self.stages = [item for item in self.stages if item.get("stage") != "telegram-summary"]
        if result.returncode != 0:
            self.add_stage("telegram-summary", "partial", required=False, detail=compact_text(result.stderr or result.stdout), path=path)
        else:
            self.add_stage("telegram-summary", "success", required=False, detail="rendered telegram-summary.txt", path=path)
        return path

    def build_summary(self, portfolio: dict[str, Any]) -> dict[str, Any]:
        token_summary = load_json_if_exists(self.output_dir / "token-summary.json") or {}
        execution = load_json_if_exists(self.output_dir / "execution.json") or {}
        account = load_json_if_exists(self.output_dir / "account-before-order.json") or {}
        decision_brief = load_json_if_exists(self.output_dir / "decision-brief.json") or {}
        orders = []
        for item in execution.get("orders", []) if isinstance(execution, dict) else []:
            if not isinstance(item, dict):
                continue
            orders.append(
                {
                    "symbol_id": item.get("symbol_id"),
                    "symbol_name": item.get("symbol_name"),
                    "direction": item.get("direction"),
                    "quantity": item.get("validated_order_quantity"),
                    "result": item.get("result"),
                    "reason": item.get("reason"),
                    "order_or_reservation_id": item.get("order_or_reservation_id"),
                }
            )
        stages = self.load_summary_stages()
        self.stages = stages
        verdict_summary = self.build_verdict_summary(account, execution)
        symbols = normalize_symbol_ids(portfolio.get("universe"))
        account_summary = account.get("account_summary") if isinstance(account.get("account_summary"), dict) else {}
        account_display_summary = self.build_account_display_summary(account_summary)
        evidence_summary = self.build_evidence_summary(decision_brief, stages, symbols)
        report_path = self.report_path()
        telegram_summary_path = self.output_dir / "telegram-summary.txt"
        summary = {
            "schema_version": "1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "status": summarized_status(stages, execution),
            "run_dir": str(self.output_dir),
            "summary_path": str(self.summary_path),
            "command_log_path": str(self.command_log_path),
            "report_path": str(report_path),
            "telegram_summary_path": str(telegram_summary_path),
            "stages": stages,
            "portfolio_counts": {
                "recommanded": len(normalize_symbol_ids(portfolio.get("recommanded"))),
                "recommended": len(normalize_symbol_ids(portfolio.get("recommended"))),
                "specified": len(normalize_symbol_ids(portfolio.get("specified"))),
                "holding": len(normalize_symbol_ids(portfolio.get("holding"))),
                "universe": len(normalize_symbol_ids(portfolio.get("universe"))),
            },
            "decision_brief": {
                "status": decision_brief.get("status"),
                "symbol_count": len(decision_brief.get("symbols", [])) if isinstance(decision_brief.get("symbols"), list) else 0,
                "error_count": len(decision_brief.get("errors", [])) if isinstance(decision_brief.get("errors"), list) else 0,
            },
            "verdict_summary": verdict_summary,
            "account_summary": account_summary,
            "account_display_summary": account_display_summary,
            "evidence_summary": evidence_summary,
            "execution": {
                "status": execution.get("status"),
                "request_type": execution.get("request_type"),
                "order_count": len(orders),
                "orders": orders,
                "errors": execution.get("errors", [])[:5] if isinstance(execution.get("errors"), list) else [],
                "requires_main_agent_order_execution": bool(execution.get("requires_main_agent_order_execution")),
                "required_main_agent_actions": execution.get("required_main_agent_actions", [])
                if isinstance(execution.get("required_main_agent_actions"), list)
                else [],
            },
            "token_usage": {
                "main": (token_summary.get("main") or {}).get("token_usage", zero_usage()),
                "subagents": (token_summary.get("subagents") or {}).get("token_usage", zero_usage()),
                "total": (token_summary.get("total") or {}).get("token_usage", zero_usage()),
            },
            "telegram_response_policy": {
                "source": "Use telegram-summary.txt as the fixed Telegram response. Regenerate it from pipeline-summary.json with render_telegram_summary.py.",
                "account_state_fields": [
                    "cash_amount",
                    "securities_valuation_amount",
                    "total_evaluation_amount",
                    "total_pnl_amount",
                ],
                "today_trade_amount_policy": "Show today_buy_amount/today_sell_amount only under a separate 당일 거래 누계 label when relevant; never present them as newly caused by this command unless execution.json confirms submitted orders.",
                "gate_label": "주문 전 기존 미체결/예약 주문",
                "evidence_policy": "Report evidence_summary.financial.display_text and evidence_summary.news.display_text, distinguishing missing cache from cache_exists_zero_usable_articles.",
                "verdict_policy": "Mention judge-midterm/second-verdict outcome and submitted or target-changed symbols, including target quantity and one_line_reason when available.",
            },
            "artifacts": {
                "check_portfolio": str(self.output_dir / "check-portfolio.json"),
                "price_chart": str(self.output_dir / "price-chart.json"),
                "account_before_order": str(self.output_dir / "account-before-order.json"),
                "decision_brief": str(self.output_dir / "decision-brief.json"),
                "verdict_first": str(self.output_dir / "verdict-first.json"),
                "verdict_second": str(self.output_dir / "verdict-second.json"),
                "execution": str(self.output_dir / "execution.json"),
                "token_summary": str(self.output_dir / "token-summary.json"),
                "portfolio_report": str(report_path),
                "telegram_summary": str(telegram_summary_path),
            },
            "main_agent_read_policy": (
                "Read pipeline-summary.json first. For explicit demo-submit or real-submit runs, pass --submit-orders so execute_orders.py refreshes "
                "read-only account/order gates, reconciles active pending/reserved orders, and submits, adjusts, or blocks immediate/reservation orders before summary generation. "
                "For explicit limit requests, treat execution-plan order_price values as the default limit price candidates unless a current API gate rejects them. "
                "Open command_log_path or other intermediate artifacts only when a stage failed and the summary is insufficient."
            ),
        }
        self.write_portfolio_report(summary)
        write_json(self.summary_path, summary)
        self.write_telegram_summary()
        summary["stages"] = self.load_summary_stages()
        write_json(self.summary_path, summary)
        self.write_run_json(status=summary["status"])
        return summary

    def missing_summarize_artifacts(self) -> list[str]:
        required = [
            self.run_path,
            self.output_dir / "check-portfolio.json",
            self.output_dir / "decision-brief.json",
            self.output_dir / "verdict-first.json",
            self.output_dir / "verdict-second.json",
            self.output_dir / "account-before-order.json",
            self.output_dir / "execution.json",
        ]
        missing = [str(path) for path in required if not path.exists()]
        for path in required:
            if not path.exists():
                continue
            payload = load_json_if_exists(path)
            if not isinstance(payload, dict):
                missing.append(f"{path}: invalid JSON object")
                continue
            if path == self.run_path:
                stages = payload.get("stages")
                if not isinstance(stages, list):
                    missing.append(f"{self.run_path}: missing stages")
                elif not stages:
                    missing.append(f"{self.run_path}: empty stages")
        return missing

    def run(self) -> dict[str, Any]:
        self.write_run_json(status="running")
        portfolio, portfolio_path = self.resolve_portfolio()
        symbols = normalize_symbol_ids(portfolio.get("universe"))
        if not symbols:
            self.add_stage("portfolio-universe", "failed", detail="check-portfolio universe is empty", path=portfolio_path)
            raise RuntimeError("check-portfolio universe is empty")
        self.add_stage("portfolio-universe", "success", detail=f"{len(symbols)} symbols", path=portfolio_path)

        self.collect_main_evidence(symbols)
        financial_cache = self.collect_optional_cache("financial", symbols)
        news_cache = self.collect_optional_cache("news", symbols)

        decision_args = [
            "decision-brief",
            "--output-dir",
            str(self.output_dir),
            "--portfolio-json",
            str(portfolio_path),
        ]
        if financial_cache:
            decision_args.extend(["--financial-cache-path", financial_cache])
        if news_cache:
            decision_args.extend(["--news-cache-path", news_cache])
        decision = self.run_artifact_command("decision-brief", decision_args)
        decision_status = str((decision or {}).get("status") or "")
        self.add_stage("decision-brief", "success" if decision_status in {"success", "partial"} else "failed", detail=f"status={decision_status}", path=self.output_dir / "decision-brief.json")
        if decision_status not in {"success", "partial"}:
            raise RuntimeError("decision-brief failed")

        self.run_artifact_command(
            "first-specs",
            [
                "first-specs",
                "--output-dir",
                str(self.output_dir),
                "--workspace-dir",
                str(self.workspace_dir),
                "--skill-dir",
                str(script_dir().parent),
            ],
        )
        self.add_stage("first-specs", "success", detail="built first-verdict specs", path=self.output_dir / "first-verdict-specs.json")
        self.run_first_verdicts()

        first = self.run_artifact_command("merge-first", ["merge-first", "--output-dir", str(self.output_dir)])
        first_status = str((first or {}).get("status") or "")
        self.add_stage("merge-first", "success" if first_status == "success" else "failed", detail=f"status={first_status}", path=self.output_dir / "verdict-first.json")
        if first_status != "success":
            raise RuntimeError("merge-first failed")

        self.run_artifact_command(
            "second-spec",
            [
                "second-spec",
                "--output-dir",
                str(self.output_dir),
                "--portfolio-json",
                str(portfolio_path),
                "--workspace-dir",
                str(self.workspace_dir),
                "--skill-dir",
                str(script_dir().parent),
            ],
        )
        self.add_stage("second-spec", "success", detail="built second-verdict spec", path=self.output_dir / "second-verdict-spec.json")
        self.run_second_verdict()

        execution = self.run_artifact_command(
            "execution-plan",
            [
                "execution-plan",
                "--output-dir",
                str(self.output_dir),
                "--request-type",
                self.args.request_type,
                "--order-path",
                getattr(self.args, "order_path", "reservation"),
            ],
        )
        execution_status = str((execution or {}).get("status") or "success")
        self.add_stage("execution-plan", "partial" if execution_status == "partial" else "success", detail=f"status={execution_status}", path=self.output_dir / "execution.json")

        if (
            getattr(self.args, "submit_orders", False)
            and self.args.request_type in {"demo-submit", "real-submit"}
            and bool((execution or {}).get("requires_main_agent_order_execution"))
        ):
            execution = self.run_order_execution()

        token_args = ["token-summary", "--run-dir", str(self.output_dir)]
        if self.args.main_events:
            token_args.extend(["--main-events", str(resolve_workspace_path(self.workspace_dir, self.args.main_events))])
        token_summary = self.run_artifact_command("token-summary", token_args, required=False)
        if token_summary is not None:
            detail = "main/sub-agent token summary built" if self.args.main_events else "sub-agent token summary built"
            self.add_stage("token-summary", "success", detail=detail, required=False, path=self.output_dir / "token-summary.json")
        return self.build_summary(portfolio)


def fake_codex_script(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path

output_path = None
for index, arg in enumerate(sys.argv):
    if arg == "-o" and index + 1 < len(sys.argv):
        output_path = Path(sys.argv[index + 1])
        break
if output_path is None:
    print("missing -o", file=sys.stderr)
    sys.exit(2)

prompt = sys.argv[-1] if sys.argv else ""
stage = "second-verdict" if "stage: second-verdict" in prompt else "first-verdict"
match = re.search(r"symbol_ids:\\s*([^\\n]+)", prompt)
symbols = [item.strip() for item in (match.group(1).split(",") if match else ["005930"]) if item.strip()]
rows = []
for index, symbol in enumerate(symbols, start=1):
    if stage == "second-verdict":
        rows.append({
            "symbol_id": symbol,
            "symbol_name": symbol,
            "target_holding_quantity": 1 if symbol == "005930" else 0,
            "relative_attractiveness_rank": index,
            "reason_code": "hold_neutral",
            "one_line_reason": "self-test"
        })
    else:
        rows.append({
            "symbol_id": symbol,
            "symbol_name": symbol,
            "score": 8 if symbol == "005930" else 5,
            "confidence": 8,
            "reason_code": "buy_candidate" if symbol == "005930" else "hold_neutral",
            "one_line_reason": "self-test",
            "missing_data": []
        })
payload = {"stage": stage, "agent_id": "fake", "persona": "fake", "human_markdown_path": "", "symbols": rows, "errors": []}
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
if "--json" in sys.argv:
    print(json.dumps({
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 50,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                    "total_tokens": 120
                }
            }
        }
    }))
sys.exit(0)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_self_test_fixtures(workspace: Path, run_dir: Path) -> Path:
    portfolio_path = workspace / "portfolio.json"
    write_json(
        portfolio_path,
        {
            "recommanded": [],
            "recommended": [],
            "specified": ["005930", "000660"],
            "holding": ["005930"],
            "universe": ["005930", "000660"],
        },
    )
    write_json(
        run_dir / "price-chart.json",
        {
            "schema_version": "1",
            "run_id": "pipeline-self-test",
            "started_at": "2026-06-18T09:00:00+09:00",
            "status": "success",
            "symbols": [
                {
                    "symbol_id": "005930",
                    "symbol_name": "삼성전자",
                    "product_type": "stock",
                    "eligible_for_verdict": True,
                    "price": {"current_or_last": 70000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                    "local_signals": [],
                    "required_missing": [],
                    "errors": [],
                },
                {
                    "symbol_id": "000660",
                    "symbol_name": "SK하이닉스",
                    "product_type": "stock",
                    "eligible_for_verdict": True,
                    "price": {"current_or_last": 200000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                    "local_signals": [],
                    "required_missing": [],
                    "errors": [],
                },
            ],
        },
    )
    write_json(
        run_dir / "account-before-order.json",
        {
            "schema_version": "1",
            "run_id": "pipeline-self-test",
            "started_at": "2026-06-18T09:00:00+09:00",
            "status": "success",
            "active_order_lookup_performed": False,
            "order_available_lookup_performed": False,
            "account_summary": {"cash_amount": 1000000, "total_evaluation_amount": 1500000},
            "active_orders": [],
            "symbols": [
                {"symbol_id": "005930", "symbol_name": "삼성전자", "current_live_holding_quantity": 0, "current_price": 70000},
                {"symbol_id": "000660", "symbol_name": "SK하이닉스", "current_live_holding_quantity": 0, "current_price": 200000},
            ],
        },
    )
    return portfolio_path


def run_self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        workspace = Path(tmp_name)
        run_dir = workspace / "reports" / "runs" / "pipeline-self-test"
        portfolio_path = write_self_test_fixtures(workspace, run_dir)
        incomplete_cache = workspace / "incomplete-cache.json"
        write_json(incomplete_cache, {"symbols": {"005930": {"items": ["probe"]}}})
        covered, missing = cache_coverage(incomplete_cache, ["005930", "000660"])
        if covered or missing != ["000660"]:
            failures.append(f"cache coverage check failed: covered={covered}, missing={missing}")
        empty_payload_cache = workspace / "empty-payload-cache.yaml"
        empty_payload_cache.write_text('date: "2026-06-18"\nsymbols:\n  "005930": {}\n  "000660": []\n', encoding="utf-8")
        covered, missing = cache_coverage(empty_payload_cache, ["005930", "000660"])
        if covered or missing != ["000660", "005930"]:
            failures.append(f"empty payload cache should be incomplete: covered={covered}, missing={missing}")
        empty_news_cache = workspace / "empty-news-cache.yaml"
        empty_news_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: ""\n        sentiment: neutral\n        content: ""\n',
            encoding="utf-8",
        )
        covered, missing = cache_coverage(empty_news_cache, ["005930"])
        if covered or missing != ["005930"]:
            failures.append(f"empty news article should be incomplete: covered={covered}, missing={missing}")
        no_news_cache = workspace / "no-news-cache.yaml"
        no_news_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "005930":\n    articles:\n      - article_date: ""\n        sentiment: neutral\n        content: "2026-06-18 기준 수집된 뉴스가 없습니다."\n',
            encoding="utf-8",
        )
        covered, missing = cache_coverage(no_news_cache, ["005930"])
        if covered or missing != ["005930"]:
            failures.append(f"no-news placeholder should be incomplete: covered={covered}, missing={missing}")
        no_news_counts = cache_evidence_counts(no_news_cache, ["005930"])
        if no_news_counts.get("present_symbol_count") != 1 or no_news_counts.get("usable_symbol_count") != 0:
            failures.append(f"no-news cache counts did not distinguish present from usable: {no_news_counts}")
        etf_probe_dir = workspace / "reports" / "runs" / "etf-cache-probe"
        write_json(
            etf_probe_dir / "price-chart.json",
            {
                "symbols": [
                    {"symbol_id": "069500", "symbol_name": "KODEX 200", "product_type": "etf"},
                ]
            },
        )
        stale_etf_cache = workspace / "stale-etf-financial.yaml"
        stale_etf_cache.write_text('date: "2026-06-18"\nsymbols:\n  "069500":\n    items:\n      - "price only"\n', encoding="utf-8")
        fresh_etf_cache = workspace / "fresh-etf-financial.yaml"
        fresh_etf_cache.write_text(
            'date: "2026-06-18"\nsymbols:\n  "069500":\n    KODEX 200:\n      ETF/ETN 현재가:\n        응답:\n          - nav: "10000"\n      NAV 비교추이(종목):\n        NAV 비교 요약:\n          - nav: "10000"\n',
            encoding="utf-8",
        )
        etf_probe = Pipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(etf_probe_dir),
                run_id="etf-cache-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        if etf_probe.covered_cache_path("financial", str(stale_etf_cache), ["069500"], detail="stale etf cache"):
            failures.append("ETF financial cache without NAV evidence should not be accepted as covered")
        if not etf_probe.covered_cache_path("financial", str(fresh_etf_cache), ["069500"], detail="fresh etf cache"):
            failures.append("ETF financial cache with NAV evidence should be accepted as covered")
        stage_status_probe = Pipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(workspace / "reports" / "runs" / "status-probe"),
                run_id="status-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        stage_status_probe.add_stage("optional-noop", "skipped", required=False)
        if stage_status_probe.pipeline_status() != "success":
            failures.append(f"optional skipped stage changed pipeline status: {stage_status_probe.pipeline_status()}")
        old_financial_memory = os.environ.get("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR")
        old_news_memory = os.environ.get("COLLECT_NEWS_INFORMATION_MEMORY_DIR")
        try:
            env_financial_dir = workspace / "env-financial-cache"
            env_news_dir = workspace / "env-news-cache"
            env_financial_dir.mkdir(parents=True, exist_ok=True)
            env_news_dir.mkdir(parents=True, exist_ok=True)
            (env_financial_dir / "financial-2026-06-18.yaml").write_text('date: "2026-06-18"\nsymbols: {}\n', encoding="utf-8")
            (env_news_dir / "news-2026-06-18.yaml").write_text('date: "2026-06-18"\nsymbols: {}\n', encoding="utf-8")
            os.environ["COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR"] = str(env_financial_dir)
            os.environ["COLLECT_NEWS_INFORMATION_MEMORY_DIR"] = str(env_news_dir)
            if Path(stage_status_probe.default_cache_path("financial")).parent != env_financial_dir:
                failures.append("financial env memory dir was not preferred")
            if Path(stage_status_probe.default_cache_path("news")).parent != env_news_dir:
                failures.append("news env memory dir was not preferred")
        finally:
            if old_financial_memory is None:
                os.environ.pop("COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR", None)
            else:
                os.environ["COLLECT_FINANCIAL_INFORMATION_MEMORY_DIR"] = old_financial_memory
            if old_news_memory is None:
                os.environ.pop("COLLECT_NEWS_INFORMATION_MEMORY_DIR", None)
            else:
                os.environ["COLLECT_NEWS_INFORMATION_MEMORY_DIR"] = old_news_memory

        old_codex_home_env = os.environ.get("CODEX_HOME")
        try:
            codex_home = workspace / "codex-home"
            installed_financial_script = codex_home / "skills" / "collect-financial-information" / "scripts" / "financial_cache.py"
            installed_financial_script.parent.mkdir(parents=True, exist_ok=True)
            installed_financial_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            os.environ["CODEX_HOME"] = str(codex_home)
            resolved_installed_script = stage_status_probe.optional_cache_script("financial")
            if resolved_installed_script != installed_financial_script:
                failures.append(f"installed financial cache script was not resolved via CODEX_HOME: {resolved_installed_script}")
        finally:
            if old_codex_home_env is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_codex_home_env

        class OptionalCacheProbePipeline(Pipeline):
            def __init__(self, args: argparse.Namespace) -> None:
                super().__init__(args)
                self.cache_attempts = 0
                self.get_attempts = 0

            def optional_cache_script(self, domain: str) -> Path:
                return workspace / f"{domain}_cache_probe.py"

            def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                if stage.endswith("-cache-get"):
                    self.get_attempts += 1
                    domain = stage.removesuffix("-cache-get")
                    subdir = "collect-financial-information" if domain == "financial" else "collect-news-information"
                    prefix = "financial" if domain == "financial" else "news"
                    path = self.workspace_dir / "memory" / subdir / f"{prefix}-2026-06-18.yaml"
                    stdout = str(path) if path.exists() else "missing cache"
                    self.logs.append(
                        {
                            "stage": stage,
                            "command": cmd,
                            "returncode": 0 if path.exists() else 1,
                            "stdout_tail": stdout,
                            "stderr_tail": "",
                            "required": required,
                            "recorded_at": now_iso(),
                        }
                    )
                    write_json(self.command_log_path, {"commands": self.logs})
                    return subprocess.CompletedProcess(cmd, 0 if path.exists() else 1, stdout=stdout, stderr="")
                if stage.endswith("-cache-collect"):
                    self.cache_attempts += 1
                    domain = stage.removesuffix("-cache-collect")
                    subdir = "collect-financial-information" if domain == "financial" else "collect-news-information"
                    prefix = "financial" if domain == "financial" else "news"
                    path = self.workspace_dir / "memory" / subdir / f"{prefix}-2026-06-18.yaml"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        'date: "2026-06-18"\nsource: kis_open_api\nsymbols:\n  "005930":\n    items:\n      - "probe"\n',
                        encoding="utf-8",
                    )
                    self.logs.append(
                        {
                            "stage": stage,
                            "command": cmd,
                            "returncode": 0,
                            "stdout_tail": str(path),
                            "stderr_tail": "",
                            "required": required,
                            "recorded_at": now_iso(),
                        }
                    )
                    write_json(self.command_log_path, {"commands": self.logs})
                    return subprocess.CompletedProcess(cmd, 0, stdout=str(path), stderr="")
                return super().run_cmd(stage, cmd, required=required, env=env)

        optional_cache_dir = workspace / "reports" / "runs" / "optional-cache-probe"
        for probe_script in (workspace / "financial_cache_probe.py", workspace / "news_cache_probe.py"):
            probe_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        optional_probe = OptionalCacheProbePipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(optional_cache_dir),
                run_id="optional-cache-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        financial_partial = optional_probe.collect_optional_cache("financial", ["005930", "000660"])
        news_partial = optional_probe.collect_optional_cache("news", ["005930", "000660"])
        if optional_probe.cache_attempts != 2:
            failures.append(f"optional cache probe should collect once per domain: attempts={optional_probe.cache_attempts}")
        if optional_probe.get_attempts != 4:
            failures.append(f"optional cache probe should get before and after collect per domain: attempts={optional_probe.get_attempts}")
        if not financial_partial or not news_partial:
            failures.append("optional cache probe did not return partial cache paths")
        if [item.get("status") for item in optional_probe.stages] != ["partial", "partial"]:
            failures.append(f"optional cache probe stages unexpected: {optional_probe.stages}")
        unrelated_cache = workspace / "unrelated-cache.yaml"
        unrelated_cache.write_text('date: "2026-06-18"\nsymbols:\n  "123456":\n    items:\n      - "probe"\n', encoding="utf-8")
        if optional_probe.first_existing_cache_path([unrelated_cache], ["005930"]):
            failures.append("unrelated cache symbols should not be returned as partial data")

        class EmptyCacheFallbackProbePipeline(Pipeline):
            def optional_cache_script(self, domain: str) -> Path:
                return workspace / f"{domain}_empty_cache_probe.py"

            def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                if stage.endswith("-cache-get") or stage.endswith("-cache-collect"):
                    domain = stage.split("-cache-", 1)[0]
                    subdir = "collect-financial-information" if domain == "financial" else "collect-news-information"
                    prefix = "financial" if domain == "financial" else "news"
                    path = self.workspace_dir / "memory" / subdir / f"{prefix}-2026-06-18.yaml"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('date: "2026-06-18"\nsource: kis_open_api\nsymbols: {}\n', encoding="utf-8")
                    self.logs.append(
                        {
                            "stage": stage,
                            "command": cmd,
                            "returncode": 1,
                            "stdout_tail": str(path),
                            "stderr_tail": "",
                            "required": required,
                            "recorded_at": now_iso(),
                        }
                    )
                    write_json(self.command_log_path, {"commands": self.logs})
                    return subprocess.CompletedProcess(cmd, 1, stdout=str(path), stderr="")
                return super().run_cmd(stage, cmd, required=required, env=env)

        for probe_script in (workspace / "financial_empty_cache_probe.py", workspace / "news_empty_cache_probe.py"):
            probe_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        for stale_cache in (
            workspace / "memory" / "collect-financial-information" / "financial-2026-06-18.yaml",
            workspace / "memory" / "collect-news-information" / "news-2026-06-18.yaml",
        ):
            if stale_cache.exists():
                stale_cache.unlink()
        empty_cache_probe = EmptyCacheFallbackProbePipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(workspace / "reports" / "runs" / "empty-cache-probe"),
                run_id="empty-cache-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        if empty_cache_probe.collect_optional_cache("financial", ["005930"]):
            failures.append("empty financial cache should not be returned as partial data")
        empty_news_path = empty_cache_probe.collect_optional_cache("news", ["005930"])
        if not empty_news_path:
            failures.append("empty news cache should be returned so zero usable articles can be reported")
        news_stage = empty_cache_probe.stages[-1] if empty_cache_probe.stages else {}
        if news_stage.get("stage") != "news-cache" or "zero usable articles" not in str(news_stage.get("detail")):
            failures.append(f"empty news cache stage did not describe zero usable articles: {news_stage}")

        retry_dir = workspace / "reports" / "runs" / "retry-probe"
        retry_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            retry_dir / "second-verdict-spec.json",
            {
                "run_id": "retry-probe",
                "started_at": "2026-06-18T09:00:00+09:00",
                "stage": "second-verdict",
                "symbol_ids": ["005930"],
            },
        )

        class RetryProbePipeline(Pipeline):
            def __init__(self, args: argparse.Namespace) -> None:
                super().__init__(args)
                self.probe_attempts = 0

            def run_cmd(self, stage: str, cmd: list[str], *, required: bool = True) -> subprocess.CompletedProcess[str]:
                self.probe_attempts += 1
                if self.probe_attempts < 3:
                    return subprocess.CompletedProcess(cmd, 1, stdout='{"status":"failed"}', stderr="")
                wrapper = {
                    "status": "success",
                    "stage": "second-verdict",
                    "agent_role": "judge-midterm",
                    "task_name": "judge-midterm",
                    "parsed_json": {
                        "stage": "second-verdict",
                        "symbols": [
                            {
                                "symbol_id": "005930",
                                "symbol_name": "삼성전자",
                                "target_holding_quantity": 1,
                                "relative_attractiveness_rank": 1,
                                "reason_code": "hold_neutral",
                                "one_line_reason": "retry self-test",
                            }
                        ],
                    },
                    "errors": [],
                }
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(wrapper), stderr="")

        retry_probe = RetryProbePipeline(
            argparse.Namespace(
                command="run",
                workspace_dir=str(workspace),
                output_dir=str(retry_dir),
                run_id="retry-probe",
                started_at="2026-06-18T09:00:00+09:00",
                env="acct",
                request_type="analysis",
                portfolio_json=str(portfolio_path),
                financial_cache_path="",
                news_cache_path="",
                main_events="",
                date="2026-06-18",
                reuse_existing_artifacts=True,
                skip_account=False,
                max_workers=3,
            )
        )
        retry_probe.run_second_verdict()
        if retry_probe.probe_attempts != 3 or not (retry_dir / "verdict-second.json").exists():
            failures.append(f"second-verdict retry probe failed: attempts={retry_probe.probe_attempts}")
        fake_codex = workspace / "fake-codex"
        fake_codex_script(fake_codex)
        old_codex_bin = os.environ.get("CODEX_BIN")
        old_reuse = os.environ.get("CODEX_SUBAGENT_REUSE_SUCCESS")
        os.environ["CODEX_BIN"] = str(fake_codex)
        os.environ["CODEX_SUBAGENT_REUSE_SUCCESS"] = "0"
        try:
            main_events = workspace / "main-events.jsonl"
            main_events.write_text(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
                + "\n"
                + json.dumps({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1, "output_tokens": 1}}})
                + "\n",
                encoding="utf-8",
            )
            pipeline = Pipeline(
                argparse.Namespace(
                    command="run",
                    workspace_dir=str(workspace),
                    output_dir=str(run_dir),
                    run_id="pipeline-self-test",
                    started_at="2026-06-18T09:00:00+09:00",
                    env="acct",
                    request_type="real-submit",
                    portfolio_json=str(portfolio_path),
                    financial_cache_path="",
                    news_cache_path="",
                    main_events=str(main_events),
                    date="2026-06-18",
                    reuse_existing_artifacts=True,
                    skip_account=False,
                    max_workers=3,
                )
            )
            summary = pipeline.run()
            if summary["status"] != "partial":
                failures.append(f"real-submit summary should remain partial before submit-order execution: {summary['status']}")
            if summary["token_usage"]["subagents"]["total_tokens"] != 480:
                failures.append(f"unexpected subagent token total: {summary['token_usage']}")
            if summary["token_usage"]["main"]["total_tokens"] != 17 or summary["token_usage"]["total"]["total_tokens"] != 497:
                failures.append(f"unexpected pipeline token summary with main events: {summary['token_usage']}")
            verdict_summary = summary.get("verdict_summary") if isinstance(summary.get("verdict_summary"), dict) else {}
            if verdict_summary.get("symbol_count") != 1 or not verdict_summary.get("symbols"):
                failures.append(f"pipeline summary omitted compact verdict summary: {verdict_summary}")
            account_display = summary.get("account_display_summary") if isinstance(summary.get("account_display_summary"), dict) else {}
            if "today_buy_amount" in account_display or "today_sell_amount" in account_display:
                failures.append(f"display account summary should not expose same-day totals as main fields: {account_display}")
            if not isinstance(account_display.get("today_trade_amounts"), dict):
                failures.append(f"display account summary omitted separate same-day trade bucket: {account_display}")
            evidence_summary = summary.get("evidence_summary") if isinstance(summary.get("evidence_summary"), dict) else {}
            if not isinstance(evidence_summary.get("news"), dict) or "display_text" not in evidence_summary.get("news", {}):
                failures.append(f"pipeline summary omitted displayable news evidence status: {evidence_summary}")
            telegram_policy = summary.get("telegram_response_policy") if isinstance(summary.get("telegram_response_policy"), dict) else {}
            if telegram_policy.get("gate_label") != "주문 전 기존 미체결/예약 주문":
                failures.append(f"telegram response policy omitted explicit gate label: {telegram_policy}")
            if "telegram-summary.txt" not in str(telegram_policy.get("source", "")):
                failures.append(f"telegram response policy did not require fixed renderer output: {telegram_policy}")
            telegram_summary_path = Path(str(summary.get("telegram_summary_path") or ""))
            if not telegram_summary_path.exists():
                failures.append(f"telegram summary was not written: {telegram_summary_path}")
            else:
                telegram_text = telegram_summary_path.read_text(encoding="utf-8")
                for required_text in ("daily-trading 결과:", "계좌", "주문", "평결", "총 사용 토큰:"):
                    if required_text not in telegram_text:
                        failures.append(f"telegram summary omitted {required_text}: {telegram_summary_path}")
            report_path = Path(str(summary.get("report_path") or ""))
            if not report_path.exists():
                failures.append(f"portfolio report was not written: {report_path}")
            else:
                report_text = report_path.read_text(encoding="utf-8")
                if "## 4. `first-verdict` 독립 평결" not in report_text or "## 5. `second-verdict` 포트폴리오 평결" not in report_text:
                    failures.append(f"portfolio report omitted verdict sections: {report_path}")
                if "주문 전 기존 미체결/예약 주문 조회: no" not in report_text or "주문 전 기존 미체결/예약 주문: 미조회" not in report_text:
                    failures.append("portfolio report did not preserve active-order gate lookup state")
                if "주문 전 기존 미체결/예약 주문 미조회" not in report_text:
                    failures.append("portfolio report did not mark unrefreshed active-order adjustment gate")
            execution_summary = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
            if execution_summary.get("requires_main_agent_order_execution") is not True:
                failures.append("real-submit pipeline summary did not request submit-order execution")
            expected_actions = ["refresh_active_order_lookup", "refresh_order_available_lookup", "continue_order_execution"]
            if execution_summary.get("required_main_agent_actions") != expected_actions:
                failures.append(f"unexpected submit-order action list: {execution_summary.get('required_main_agent_actions')}")
            read_policy = summary.get("main_agent_read_policy", "")
            if "execution-plan order_price values as the default limit price candidates" not in read_policy:
                failures.append(f"pipeline summary read policy omitted default order_price guidance: {read_policy}")
            fake_execute_orders = workspace / "fake-execute-orders.py"
            fake_execute_orders.write_text(
                """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[sys.argv.index("--output-dir") + 1])
execution_path = output_dir / "execution.json"
account_path = output_dir / "account-before-order.json"
execution = json.loads(execution_path.read_text(encoding="utf-8"))
account = json.loads(account_path.read_text(encoding="utf-8"))
account["active_order_lookup_performed"] = True
account["order_available_lookup_performed"] = True
account["active_orders"] = []
orders = execution.get("orders") if isinstance(execution.get("orders"), list) else []
for item in orders:
    if isinstance(item, dict):
        item["result"] = "submitted"
        item["reason"] = "fake_submit_order"
        item["order_or_reservation_id"] = "fake-resv-1"
        item["attempts"] = [{"api_name": "order_resv", "result": "submitted"}]
execution["status"] = "success"
execution["requires_main_agent_order_execution"] = False
execution["required_main_agent_actions"] = []
execution["order_execution_mode"] = "submit"
account_path.write_text(json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8")
execution_path.write_text(json.dumps(execution, ensure_ascii=False, indent=2), encoding="utf-8")
(output_dir / "order-execution-log.json").write_text(json.dumps({"status": "success"}, ensure_ascii=False), encoding="utf-8")
print(json.dumps(execution, ensure_ascii=False))
""",
                encoding="utf-8",
            )
            fake_execute_orders.chmod(0o755)

            class SubmitOrdersProbePipeline(Pipeline):
                def order_execution_script(self) -> str:
                    return str(fake_execute_orders)

            submit_run_dir = workspace / "reports" / "runs" / "submit-orders-probe"
            write_self_test_fixtures(workspace, submit_run_dir)
            submit_pipeline = SubmitOrdersProbePipeline(
                argparse.Namespace(
                    command="run",
                    workspace_dir=str(workspace),
                    output_dir=str(submit_run_dir),
                    run_id="submit-orders-probe",
                    started_at="2026-06-18T09:00:00+09:00",
                    env="acct",
                    request_type="real-submit",
                    portfolio_json=str(portfolio_path),
                    financial_cache_path="",
                    news_cache_path="",
                    main_events=str(main_events),
                    date="2026-06-18",
                    reuse_existing_artifacts=True,
                    skip_account=False,
                    max_workers=3,
                    submit_orders=True,
                )
            )
            submit_summary = submit_pipeline.run()
            submit_stages = [item.get("stage") for item in load_json(submit_run_dir / "run.json").get("stages", []) if isinstance(item, dict)]
            if "order-execution" not in submit_stages:
                failures.append(f"submit-orders pipeline did not run order-execution stage: {submit_stages}")
            if submit_summary.get("status") != "success":
                failures.append(f"submit-orders summary did not reflect fake submitted order: {submit_summary.get('status')}")
            submit_execution = submit_summary.get("execution") if isinstance(submit_summary.get("execution"), dict) else {}
            if submit_execution.get("requires_main_agent_order_execution") is not False:
                failures.append(f"submit-orders summary did not clear execution handoff: {submit_execution}")
            submit_telegram = Path(str(submit_summary.get("telegram_summary_path") or ""))
            if not submit_telegram.exists():
                failures.append(f"submit-orders summary did not render telegram summary: {submit_telegram}")
            if not (run_dir / "pipeline-summary.json").exists():
                failures.append("pipeline-summary.json was not written")
            if not (run_dir / "execution.json").exists():
                failures.append("execution.json was not written")
            execution_payload = load_json(run_dir / "execution.json")
            execution_payload["status"] = "success"
            execution_payload["requires_main_agent_order_execution"] = False
            execution_payload["required_main_agent_actions"] = []
            if execution_payload.get("orders"):
                execution_payload["orders"][0]["result"] = "submitted"
                execution_payload["orders"][0]["reason"] = "accepted_reservation_order"
                execution_payload["orders"][0]["order_or_reservation_id"] = "selftest-resv-1"
            write_json(run_dir / "execution.json", execution_payload)
            run_payload = load_json(run_dir / "run.json")
            run_payload.setdefault("stages", []).append(
                {
                    "stage": "order-execution",
                    "status": "success",
                    "required": True,
                    "detail": "self-test order execution completed",
                    "path": str(run_dir / "execution.json"),
                }
            )
            write_json(run_dir / "run.json", run_payload)
            summarize_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(run_dir.relative_to(workspace)),
                    "--request-type",
                    "real-submit",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if summarize_result.returncode != 0:
                failures.append(f"summarize CLI failed: stdout={summarize_result.stdout} stderr={summarize_result.stderr}")
            summarized = load_json(run_dir / "pipeline-summary.json")
            summarized_run = load_json(run_dir / "run.json")
            if len(summarized_run.get("stages", [])) != len(run_payload.get("stages", [])):
                failures.append("summarize did not preserve run.json stages")
            if summarized.get("status") != "success":
                failures.append(f"summarize did not reflect completed order execution: {summarized.get('status')}")
            if (summarized.get("verdict_summary") or {}).get("submitted_order_count") != 1:
                failures.append(f"summarize did not carry submitted order count: {summarized.get('verdict_summary')}")
            summarized_telegram = Path(str(summarized.get("telegram_summary_path") or ""))
            if not summarized_telegram.exists() or "selftest-resv-1" not in summarized_telegram.read_text(encoding="utf-8"):
                failures.append("summarize did not refresh telegram summary with submitted order evidence")
            final_report = Path(str(summarized.get("report_path") or ""))
            final_report_text = final_report.read_text(encoding="utf-8") if final_report.exists() else ""
            if "selftest-resv-1" not in final_report_text or "submitted" not in final_report_text:
                failures.append("summarized report did not include submitted order evidence")
            empty_run_dir = workspace / "reports" / "runs" / "empty-summary-probe"
            empty_run_dir.mkdir(parents=True, exist_ok=True)
            empty_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(empty_run_dir.relative_to(workspace)),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if empty_result.returncode == 0:
                failures.append(f"summarize accepted an empty run directory: {empty_result.stdout}")
            bad_json_dir = workspace / "reports" / "runs" / "bad-json-summary-probe"
            bad_json_dir.mkdir(parents=True, exist_ok=True)
            for source_name in (
                "run.json",
                "check-portfolio.json",
                "decision-brief.json",
                "verdict-first.json",
                "verdict-second.json",
                "account-before-order.json",
                "execution.json",
            ):
                target = bad_json_dir / source_name
                target.write_text((run_dir / source_name).read_text(encoding="utf-8"), encoding="utf-8")
            (bad_json_dir / "execution.json").write_text("{bad-json", encoding="utf-8")
            bad_json_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(bad_json_dir.relative_to(workspace)),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if bad_json_result.returncode == 0:
                failures.append(f"summarize accepted invalid required JSON: {bad_json_result.stdout}")
            empty_stages_dir = workspace / "reports" / "runs" / "empty-stages-summary-probe"
            empty_stages_dir.mkdir(parents=True, exist_ok=True)
            for source_name in (
                "run.json",
                "check-portfolio.json",
                "decision-brief.json",
                "verdict-first.json",
                "verdict-second.json",
                "account-before-order.json",
                "execution.json",
            ):
                target = empty_stages_dir / source_name
                target.write_text((run_dir / source_name).read_text(encoding="utf-8"), encoding="utf-8")
            empty_stages_payload = load_json(empty_stages_dir / "run.json")
            empty_stages_payload["stages"] = []
            write_json(empty_stages_dir / "run.json", empty_stages_payload)
            empty_stages_result = subprocess.run(
                [
                    sys.executable,
                    str(script_dir() / "run_daily_trading_pipeline.py"),
                    "summarize",
                    "--workspace-dir",
                    str(workspace),
                    "--output-dir",
                    str(empty_stages_dir.relative_to(workspace)),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if empty_stages_result.returncode == 0:
                failures.append(f"summarize accepted empty run stages: {empty_stages_result.stdout}")
        finally:
            if old_codex_bin is None:
                os.environ.pop("CODEX_BIN", None)
            else:
                os.environ["CODEX_BIN"] = old_codex_bin
            if old_reuse is None:
                os.environ.pop("CODEX_SUBAGENT_REUSE_SUCCESS", None)
            else:
                os.environ["CODEX_SUBAGENT_REUSE_SUCCESS"] = old_reuse

    payload = {"status": "passed" if not failures else "failed", "failures": failures}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not failures else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the daily-trading pipeline with compact Main-agent output.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run the deterministic daily-trading orchestration pipeline.")
    run.add_argument("--workspace-dir", default=".")
    run.add_argument("--output-dir", default="")
    run.add_argument("--run-id", default="")
    run.add_argument("--started-at", default="")
    run.add_argument("--env", default=os.environ.get("CODEX_MCP_TRADING_ENV", "acct"), choices=["acct", "real", "paper", "demo"])
    run.add_argument("--request-type", default="analysis", choices=["analysis", "prepare", "demo-submit", "real-submit"])
    run.add_argument("--portfolio-json", default="")
    run.add_argument("--financial-cache-path", default="")
    run.add_argument("--news-cache-path", default="")
    run.add_argument("--main-events", default="", help="Optional Codex JSONL events path for Main-agent token accounting.")
    run.add_argument("--submit-orders", action="store_true", help="For explicit demo-submit/real-submit runs, execute immediate or reservation orders through execute_orders.py.")
    run.add_argument("--order-path", choices=["reservation", "immediate"], default="reservation")
    run.add_argument("--date", default="")
    run.add_argument("--reuse-existing-artifacts", action="store_true")
    run.add_argument("--skip-account", action="store_true")
    run.add_argument("--max-workers", type=int, default=3)

    summarize = subparsers.add_parser("summarize", help="Rebuild pipeline-summary.json and the portfolio Markdown report from existing run artifacts.")
    summarize.add_argument("--workspace-dir", default=".")
    summarize.add_argument("--output-dir", required=True)
    summarize.add_argument("--run-id", default="")
    summarize.add_argument("--started-at", default="")
    summarize.add_argument("--env", default=os.environ.get("CODEX_MCP_TRADING_ENV", "acct"), choices=["acct", "real", "paper", "demo"])
    summarize.add_argument("--request-type", default="analysis", choices=["analysis", "prepare", "demo-submit", "real-submit"])
    summarize.add_argument("--order-path", choices=["reservation", "immediate"], default="reservation")
    summarize.add_argument("--portfolio-json", default="")

    subparsers.add_parser("self-test", help="Run an offline pipeline smoke test with a fake codex binary.")
    return parser


def command_run(args: argparse.Namespace) -> int:
    pipeline = Pipeline(args)
    try:
        summary = pipeline.run()
    except Exception as exc:  # noqa: BLE001 - write compact failed summary
        pipeline.add_stage("pipeline", "failed", detail=str(exc)[:300])
        summary = pipeline.build_summary(load_json_if_exists(pipeline.output_dir / "check-portfolio.json") or {})
        summary["error"] = str(exc)[:500]
        write_json(pipeline.summary_path, summary)
        print(json.dumps({"status": "failed", "run_dir": str(pipeline.output_dir), "summary_path": str(pipeline.summary_path)}, ensure_ascii=False, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "status": summary["status"],
                "run_dir": summary["run_dir"],
                "summary_path": summary["summary_path"],
                "subagent_total_tokens": summary["token_usage"]["subagents"]["total_tokens"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] in {"success", "partial"} else 1


def command_summarize(args: argparse.Namespace) -> int:
    pipeline = Pipeline(args)
    missing = pipeline.missing_summarize_artifacts()
    if missing:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "run_dir": str(pipeline.output_dir),
                    "missing_artifacts": missing,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    pipeline.adopt_existing_run_identity()
    summary = pipeline.build_summary(pipeline.load_portfolio_for_summary())
    print(
        json.dumps(
            {
                "status": summary["status"],
                "run_dir": summary["run_dir"],
                "summary_path": summary["summary_path"],
                "report_path": summary["report_path"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] in {"success", "partial"} else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "self-test":
        return run_self_test()
    if args.command == "run":
        return command_run(args)
    if args.command == "summarize":
        return command_summarize(args)
    raise SystemExit("a subcommand is required")


if __name__ == "__main__":
    raise SystemExit(main())
