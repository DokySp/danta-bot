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


def cache_coverage(path: Path, symbols: list[str]) -> tuple[bool, list[str]]:
    wanted = {normalize_symbol_key(symbol) for symbol in symbols if normalize_symbol_key(symbol)}
    available = cache_symbol_keys(path)
    missing = sorted(wanted - available)
    return bool(wanted) and not missing, missing


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

    def covered_cache_path(self, domain: str, path_text: str, symbols: list[str], *, detail: str) -> str:
        path = resolve_workspace_path(self.workspace_dir, path_text)
        covered, missing = cache_coverage(path, symbols)
        if covered:
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
            self.add_stage(f"{domain}-cache", "partial", detail="optional cache collected once but no cache file was produced", required=False)
            return ""
        partial = self.first_existing_cache_path(candidate_paths, symbols)
        if partial:
            self.add_stage(f"{domain}-cache", "partial", detail="optional cache collection failed once; using existing incomplete cache", required=False, path=partial)
            return str(partial)
        self.add_stage(f"{domain}-cache", "partial", detail="optional cache collection failed once", required=False, path=self.command_log_path)
        return ""

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
                }
            )
        summary = {
            "schema_version": "1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "status": self.pipeline_status(),
            "run_dir": str(self.output_dir),
            "summary_path": str(self.summary_path),
            "command_log_path": str(self.command_log_path),
            "stages": self.stages,
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
            "account_summary": account.get("account_summary") if isinstance(account.get("account_summary"), dict) else {},
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
            "artifacts": {
                "check_portfolio": str(self.output_dir / "check-portfolio.json"),
                "price_chart": str(self.output_dir / "price-chart.json"),
                "account_before_order": str(self.output_dir / "account-before-order.json"),
                "decision_brief": str(self.output_dir / "decision-brief.json"),
                "verdict_first": str(self.output_dir / "verdict-first.json"),
                "verdict_second": str(self.output_dir / "verdict-second.json"),
                "execution": str(self.output_dir / "execution.json"),
                "token_summary": str(self.output_dir / "token-summary.json"),
            },
            "main_agent_read_policy": (
                "Read pipeline-summary.json first. For demo-submit or real-submit, if execution.requires_main_agent_order_execution is true, "
                "open only the minimal account/order artifacts and continue Main-agent order-execution; refresh listed read-only gates before submission. "
                "For explicit limit reservation requests, treat execution-plan order_price values as the default limit price candidates unless a current API gate rejects them. "
                "Open command_log_path or other intermediate artifacts only when a stage failed and the summary is insufficient."
            ),
        }
        write_json(self.summary_path, summary)
        self.write_run_json(status=summary["status"])
        return summary

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
            ],
        )
        execution_status = str((execution or {}).get("status") or "success")
        self.add_stage("execution-plan", "partial" if execution_status == "partial" else "success", detail=f"status={execution_status}", path=self.output_dir / "execution.json")

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
            if summary["status"] not in {"success", "partial"}:
                failures.append(f"unexpected pipeline status: {summary['status']}")
            if summary["token_usage"]["subagents"]["total_tokens"] != 480:
                failures.append(f"unexpected subagent token total: {summary['token_usage']}")
            if summary["token_usage"]["main"]["total_tokens"] != 17 or summary["token_usage"]["total"]["total_tokens"] != 497:
                failures.append(f"unexpected pipeline token summary with main events: {summary['token_usage']}")
            execution_summary = summary.get("execution") if isinstance(summary.get("execution"), dict) else {}
            if execution_summary.get("requires_main_agent_order_execution") is not True:
                failures.append("real-submit pipeline summary did not request Main-agent order execution")
            expected_actions = ["refresh_active_order_lookup", "refresh_order_available_lookup", "continue_order_execution"]
            if execution_summary.get("required_main_agent_actions") != expected_actions:
                failures.append(f"unexpected Main-agent action list: {execution_summary.get('required_main_agent_actions')}")
            read_policy = summary.get("main_agent_read_policy", "")
            if "execution-plan order_price values as the default limit price candidates" not in read_policy:
                failures.append(f"pipeline summary read policy omitted default order_price guidance: {read_policy}")
            if not (run_dir / "pipeline-summary.json").exists():
                failures.append("pipeline-summary.json was not written")
            if not (run_dir / "execution.json").exists():
                failures.append("execution.json was not written")
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
    run.add_argument("--date", default="")
    run.add_argument("--reuse-existing-artifacts", action="store_true")
    run.add_argument("--skip-account", action="store_true")
    run.add_argument("--max-workers", type=int, default=3)

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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "self-test":
        return run_self_test()
    if args.command == "run":
        return command_run(args)
    raise SystemExit("a subcommand is required")


if __name__ == "__main__":
    raise SystemExit(main())
