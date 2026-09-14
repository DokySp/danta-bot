"""Operator CLI: same application calls used by schedules and ingress."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .application import Application, MarketBundle, code_identity
from .config import HumanRequired, ROOT, canonical, load_config, trusted_approval, utcnow, validate_activation
from .reporting import render_readme, write_report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="danta")
    result.add_argument("--config-dir", type=Path, default=ROOT / "config")
    result.add_argument("--snapshot", type=Path, default=ROOT / "tests/fixtures/offline-e2e.json")
    result.add_argument("--approval-file", type=Path)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    config = commands.add_parser("config")
    config.add_argument("action", choices=["validate", "diff"])
    config.add_argument("--against", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--kind", choices=["full_review", "event_review"], default="full_review")
    run.add_argument("--event-id")
    run.add_argument("--request-key")
    commands.add_parser("reconcile")
    commands.add_parser("status")
    commands.add_parser("serve")
    report = commands.add_parser("report")
    report.add_argument("--date")
    report.add_argument("--design", action="store_true")
    for name in ("replay", "evaluate"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
    commands.add_parser("pause")
    candidates = commands.add_parser("candidates")
    candidates.add_argument("action", choices=["add", "remove", "exclude", "include"])
    candidates.add_argument("ticker")
    resume = commands.add_parser("resume")
    resume.add_argument("--approval", required=True)
    approvals = commands.add_parser("approvals")
    approvals.add_argument("action", choices=["show"])
    activate = commands.add_parser("activate")
    activate.add_argument("--approval", required=True)
    activate.add_argument("--expected-config", required=True)
    return result


def make_application(config, args):
    approval = None
    if args.approval_file:
        envelope = json.loads(args.approval_file.read_text())
        approval = trusted_approval(args.approval_file, config, envelope["id"])
    if config.mode == "offline":
        bundle = MarketBundle(json.loads(args.snapshot.read_text()), config.research, mode=config.mode)
        return Application(config, bundle)
    config.require_external("account_read", approval)
    from .runtime import build_external_runtime
    bundle, broker, decide, refresh = build_external_runtime(config, approval)
    return Application(config, bundle, broker=broker, decide=decide, refresh=refresh, approval=approval)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    app = None
    try:
        config = load_config(args.config_dir)
        if args.command == "doctor":
            result = {"configuration": "VALID", "mode": config.mode, "code_id": code_identity(), "config_hash": config.config_hash,
                "strategy_hash": config.strategy_hash, "default_network": "BLOCKED" if config.mode == "offline" else "APPROVAL_REQUIRED",
                "external_integration": "EXTERNAL_INTEGRATION_UNVERIFIED", "strategy": "STRATEGY_UNPROVEN", "live": "LIVE_NOT_AUTHORIZED"}
        elif args.command == "config":
            result = {"status": "VALID", "config_hash": config.config_hash, "strategy_hash": config.strategy_hash}
            if args.action == "diff":
                if args.against is None:
                    raise ValueError("config diff requires --against <config snapshot JSON>")
                old = json.loads(args.against.read_text())
                old = old.get("data", old)
                changes = []
                def walk(before, after, path=""):
                    if isinstance(before, dict) and isinstance(after, dict):
                        for key in sorted(set(before) | set(after)):
                            walk(before.get(key), after.get(key), f"{path}.{key}".lstrip("."))
                    elif before != after:
                        changes.append({"path": path, "before": before, "after": after})
                walk(old, config.data)
                result["changes"] = changes
        elif args.command == "report" and args.design:
            result = render_readme(ROOT / "README.md", ROOT / "report.html")
        elif args.command in {"replay", "evaluate"}:
            from .evaluation import evaluate_manifest, replay_manifest
            if config.mode != "offline":
                raise HumanRequired("Evaluation CLI uses isolated offline manifests; runtime data approval is separate")
            result = (replay_manifest if args.command == "replay" else evaluate_manifest)(args.manifest)
        elif args.command == "serve":
            from .service import serve
            serve(config, args, application_factory=make_application)
            result = {"status": "STOPPED"}
        elif args.command == "approvals":
            if args.approval_file is None:
                result = {"status": "LIVE_NOT_AUTHORIZED", "trusted_approval": None}
            else:
                envelope = json.loads(args.approval_file.read_text())
                result = trusted_approval(args.approval_file, config, envelope["id"])
        else:
            app = make_application(config, args)
            approval = app.approval
            if args.command == "run":
                if config.app["monitoring"]["enabled"]:
                    app.start_monitor(config.app["monitoring"]["quote_poll_fallback_seconds"])
                result = app.review(kind=args.kind, event_id=args.event_id, request_key=args.request_key)
            elif args.command == "reconcile":
                result = app.reconcile()
            elif args.command == "status":
                result = app.status()
            elif args.command == "pause":
                result = app.pause()
            elif args.command == "candidates":
                command = {"add": "add_portfolio_ticker", "remove": "remove_portfolio_ticker",
                           "exclude": "add_portfolio_except_ticker", "include": "remove_portfolio_except_ticker"}[args.action]
                result = app.update_candidate_list(command, args.ticker)
            elif args.command == "report":
                date = args.date or app.bundle.now.date().isoformat()
                from datetime import date as Date
                Date.fromisoformat(date)
                rows = app.store.db.execute("SELECT payload FROM journal WHERE kind='RUN_OUTCOME' AND substr(json_extract(payload,'$.created_at'),1,10)=?", (date,)).fetchall()
                data = {"schema_version": 1, "date": date, "created_at": utcnow().isoformat(), "config_hash": config.config_hash,
                    "strategy_hash": config.strategy_hash, "code_id": app.code_id, "status": app.status(), "runs": [json.loads(row[0]) for row in rows]}
                directory = config.state_dir / "reports" / date
                directory.mkdir(parents=True, exist_ok=True)
                result = write_report(data, directory / "daily.json", directory / "daily.html", "일일 판단·성과")
            elif args.command in {"resume", "activate"}:
                if approval is None or approval["id"] != args.approval:
                    raise HumanRequired("Matching trusted operator approval required")
                if args.command == "activate":
                    validate_activation(config, approval, args.expected_config, app.code_id)
                    app.reconcile()
                    with app.store.transaction():
                        app.store.set("activation", {"config_hash": config.config_hash, "code_id": app.code_id,
                                                     "approval_id": approval["id"]})
                        app.store.event("operator", "ACTIVATED", {"approval_id": approval["id"]})
                else:
                    app.resume()
                result = {"status": args.command.upper(), "approval_id": approval["id"]}
            else:
                raise ValueError("Unknown command")
        print(canonical(result))
        return 0
    except HumanRequired as error:
        print(canonical({"status": error.state, "reason": str(error)}))
        return 2
    except (ValueError, OSError, KeyError) as error:
        print(canonical({"status": "FAILED", "error_type": type(error).__name__, "reason": str(error)}))
        return 1
    finally:
        if app:
            app.close()
