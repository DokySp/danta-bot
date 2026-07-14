#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import select
import subprocess
import sys
import time


QUOTA_WINDOW_SPECS = {
    "5h": ("primary", 300),
    "weekly": ("secondary", 10080),
}


def send(proc, message):
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def read_rate_limits(timeout_seconds):
    proc = subprocess.Popen(
        [os.getenv("CODEX_BIN", "codex"), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    try:
        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "codex-usage-probe",
                        "title": None,
                        "version": "0",
                    },
                    "capabilities": {"experimentalApi": False},
                },
            },
        )
        send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": None})
        send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": None,
            },
        )

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)
            for stream in ready:
                line = stream.readline()
                if not line:
                    continue
                if stream is proc.stderr:
                    continue

                message = json.loads(line)
                if message.get("id") != 2:
                    continue
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message["result"]

        raise TimeoutError("rate limit response not received")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def format_reset(epoch_seconds):
    if epoch_seconds is None:
        return "n/a"
    return dt.datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M:%S")


def format_remaining_percent(used_percent):
    if used_percent is None:
        return "n/a"
    if isinstance(used_percent, str):
        used_percent = used_percent.strip().removesuffix("%").strip()
    try:
        remaining = 100.0 - float(used_percent)
    except (TypeError, ValueError):
        return "n/a"
    remaining = min(100.0, max(0.0, remaining))
    if remaining.is_integer():
        return str(int(remaining))
    return f"{remaining:.1f}".rstrip("0").rstrip(".")


def parse_window_duration(value):
    if isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def select_window(limits, period):
    if not isinstance(limits, dict):
        return None
    spec = QUOTA_WINDOW_SPECS.get(period)
    if spec is None:
        return None
    fallback_key, expected_duration = spec

    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        if parse_window_duration(window.get("windowDurationMins")) == expected_duration:
            return window

    fallback = limits.get(fallback_key)
    if not isinstance(fallback, dict):
        return None
    fallback_duration = parse_window_duration(fallback.get("windowDurationMins"))
    if fallback_duration is None:
        return fallback
    return None


def print_window(label, window):
    if not window:
        print(f"{label}: n/a")
        return
    remaining = format_remaining_percent(window.get("usedPercent"))
    reset_at = format_reset(window.get("resetsAt"))
    mins = window.get("windowDurationMins")
    print(f"{label}: {remaining}% remaining, window {mins} min, reset {reset_at}")


def main():
    parser = argparse.ArgumentParser(description="Show Codex 5h and weekly remaining quota.")
    parser.add_argument("--json", action="store_true", help="print raw JSON response")
    parser.add_argument("--timeout", type=int, default=20, help="RPC timeout in seconds")
    args = parser.parse_args()

    result = read_rate_limits(args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    limits = result["rateLimits"]
    print("plan:", limits.get("planType"))
    print("limit:", limits.get("limitId") or "default")
    print_window("5h", select_window(limits, "5h"))
    print_window("weekly", select_window(limits, "weekly"))

    credits = limits.get("credits")
    if credits:
        print(
            "credits:",
            f"has={credits.get('hasCredits')}",
            f"unlimited={credits.get('unlimited')}",
            f"balance={credits.get('balance')}",
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
