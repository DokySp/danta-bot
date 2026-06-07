#!/usr/bin/env python3
import argparse
import json
import os
import time
import uuid
from pathlib import Path


def gate_dir() -> Path:
    path = Path(os.getenv("KIS_CALL_GATE_DIR", "~/.cache/codex/kis-call-gate")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def configured_min_interval(requested: float) -> float:
    configured = float(os.getenv("KIS_CALL_GATE_MIN_INTERVAL", "1.2"))
    return max(1.0, configured, requested)


def acquire(args: argparse.Namespace) -> int:
    root = gate_dir()
    lock_path = root / "gate.lock"
    last_call_path = root / "last-call.json"
    deadline = time.time() + args.timeout
    token = uuid.uuid4().hex

    while time.time() < deadline:
        now = time.time()
        payload = {
            "token": token,
            "run_id": args.run_id,
            "agent": args.agent,
            "acquired_at_epoch": now,
        }
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            current = read_json(lock_path)
            acquired_at = float(current.get("acquired_at_epoch", 0) or 0)
            if acquired_at and now - acquired_at > args.stale_after:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            time.sleep(0.1)
            continue

        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, ensure_ascii=True)

        last_call = read_json(last_call_path)
        released_at = float(last_call.get("released_at_epoch", 0) or 0)
        wait_for = configured_min_interval(args.min_interval) - (time.time() - released_at)
        if wait_for > 0:
            time.sleep(wait_for)

        print(json.dumps(payload, ensure_ascii=True))
        return 0

    print(json.dumps({"error": "timeout", "timeout_seconds": args.timeout}, ensure_ascii=True))
    return 1


def release(args: argparse.Namespace) -> int:
    root = gate_dir()
    lock_path = root / "gate.lock"
    last_call_path = root / "last-call.json"
    current = read_json(lock_path)
    if current.get("token") != args.token:
        print(json.dumps({"error": "token_mismatch"}, ensure_ascii=True))
        return 1

    released_at = time.time()
    tmp_path = last_call_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({"released_at_epoch": released_at}, ensure_ascii=True))
    tmp_path.replace(last_call_path)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    print(json.dumps({"released": True, "released_at_epoch": released_at}, ensure_ascii=True))
    return 0


def status(_args: argparse.Namespace) -> int:
    root = gate_dir()
    print(
        json.dumps(
            {
                "lock": read_json(root / "gate.lock"),
                "last_call": read_json(root / "last-call.json"),
            },
            ensure_ascii=True,
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    subparsers = result.add_subparsers(dest="command", required=True)

    acquire_parser = subparsers.add_parser("acquire")
    acquire_parser.add_argument("--run-id", required=True)
    acquire_parser.add_argument("--agent", required=True)
    acquire_parser.add_argument("--timeout", type=float, default=120.0)
    acquire_parser.add_argument("--stale-after", type=float, default=180.0)
    acquire_parser.add_argument("--min-interval", type=float, default=1.2)
    acquire_parser.set_defaults(func=acquire)

    release_parser = subparsers.add_parser("release")
    release_parser.add_argument("--token", required=True)
    release_parser.set_defaults(func=release)

    status_parser = subparsers.add_parser("status")
    status_parser.set_defaults(func=status)
    return result


if __name__ == "__main__":
    args = parser().parse_args()
    raise SystemExit(args.func(args))
