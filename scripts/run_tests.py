#!/usr/bin/env python3
"""Canonical repo-wide regression runner.

Runs every tracked unittest suite as an independent `unittest discover`
invocation and reports a per-suite summary. Exits non-zero if any suite
fails, errors, or discovers zero tests. The runner uses only stdlib;
suites require containers/trading-engine/requirements.lock. No external network
access or bytecode/cache files are needed.

Usage (from repository root):
    python3 scripts/run_tests.py --docker
    # Or use an interpreter with requirements.lock installed:
    python3 scripts/run_tests.py
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

RAN_RE = re.compile(r"Ran (\d+) tests?")


def run_in_docker() -> int:
    """Test current sources with pinned dependencies, excluding ignored private files."""
    files = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
         "--", "scripts", "containers/trading-engine",
         "containers/telegram-gateway"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout.split("\0")
    with tempfile.TemporaryDirectory(prefix="danta-regression-") as temporary:
        context = Path(temporary)
        for name in filter(None, files):
            source = REPO_ROOT / name
            if "legacy" in source.relative_to(REPO_ROOT).parts:
                continue
            if source.is_symlink():
                raise ValueError(f"Test source must not be a symlink: {name}")
            if not source.is_file():  # Deleted tracked files are not test inputs.
                continue
            target = context / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (context / "Dockerfile").write_text("""FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY containers/trading-engine/requirements.lock /tmp/requirements.lock
RUN python -m pip install --no-cache-dir -r /tmp/requirements.lock
WORKDIR /workspace/danta-bot
COPY . .
USER 10001:10001
CMD ["python", "-B", "scripts/run_tests.py"]
""")
        built = subprocess.run(
            ["docker", "build", "--quiet", "-t", "danta-regression:local", str(context)],
            check=True, stdout=subprocess.PIPE, text=True,
        )
        return subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--read-only",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
             "--tmpfs", "/tmp:rw,exec,nosuid,size=256m,mode=1777", built.stdout.strip()],
        ).returncode


@dataclass(frozen=True)
class Suite:
    name: str
    start_dir: str
    top_level_dir: str
    extra_pythonpath: str | None = None


SUITES: list[Suite] = [
    Suite(
        name="trading-engine",
        start_dir="containers/trading-engine/tests",
        top_level_dir="containers/trading-engine",
        extra_pythonpath="containers/trading-engine/src",
    ),
    Suite(
        name="telegram-gateway",
        start_dir="containers/telegram-gateway/tests",
        top_level_dir="containers/telegram-gateway",
    ),
    Suite(
        name="repo-tools:run_tests",
        start_dir="scripts/tests",
        top_level_dir="scripts",
    ),
]


@dataclass
class SuiteResult:
    suite: Suite
    ok: bool
    test_count: int
    output: str


def parse_test_count(stdout: str, stderr: str) -> int:
    """Parse the outer `unittest discover` "Ran N tests" summary.

    unittest's TextTestRunner writes its final summary to stderr, so stderr
    is checked first. A test body under discovery can legitimately print its
    own unrelated "Ran N tests" text to stdout (e.g. a test that exercises
    this very runner's output-formatting code, or a nested self-test
    umbrella's fixture output) — and, depending on the interpreter/platform,
    stdout/stderr buffering can reorder those lines relative to each other
    when merged into one stream. Capturing the streams separately and always
    preferring stderr's last match avoids depending on that ordering.
    """
    for text in (stderr, stdout):
        matches = RAN_RE.findall(text)
        if matches:
            return int(matches[-1])
    return 0


def run_suite(suite: Suite) -> SuiteResult:
    import os

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if suite.extra_pythonpath:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            suite.extra_pythonpath
            if not existing
            else f"{suite.extra_pythonpath}{os.pathsep}{existing}"
        )

    cmd = [
        sys.executable,
        "-B",
        "-m",
        "unittest",
        "discover",
        "-s",
        suite.start_dir,
        "-t",
        suite.top_level_dir,
        "-p",
        "test_*.py",
    ]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    test_count = parse_test_count(proc.stdout, proc.stderr)
    ok = proc.returncode == 0 and test_count > 0
    combined_output = f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    return SuiteResult(suite=suite, ok=ok, test_count=test_count, output=combined_output)


def main(suites: list[Suite] | None = None) -> int:
    results = [run_suite(suite) for suite in (SUITES if suites is None else suites)]

    print("\n=== repo-wide regression summary ===")
    overall_ok = True
    total_tests = 0
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        if not result.ok:
            overall_ok = False
        total_tests += result.test_count
        reason = ""
        if result.ok is False and result.test_count == 0:
            reason = " (zero tests discovered)"
        print(f"[{status}] {result.suite.name}: {result.test_count} tests{reason}")
        if not result.ok:
            print("--- output ---")
            print(result.output.rstrip())
            print("--- end output ---")

    print(f"\nTotal: {total_tests} tests across {len(results)} suites")
    print("RESULT: PASS" if overall_ok else "RESULT: FAIL")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", help="Run with Python 3.12 and pinned dependencies in Docker")
    args = parser.parse_args()
    try:
        raise SystemExit(run_in_docker() if args.docker else main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Regression setup failed: {error}", file=sys.stderr)
        raise SystemExit(1)
