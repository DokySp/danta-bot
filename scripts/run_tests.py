#!/usr/bin/env python3
"""Canonical repo-wide regression runner.

Runs every tracked unittest suite as an independent `unittest discover`
invocation and reports a per-suite summary. Exits non-zero if any suite
fails, errors, or discovers zero tests. Stdlib only, no network access,
no bytecode/cache files written.

Usage (from repository root):
    python3 scripts/run_tests.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

RAN_RE = re.compile(r"Ran (\d+) tests?")


@dataclass(frozen=True)
class Suite:
    name: str
    start_dir: str
    top_level_dir: str
    extra_pythonpath: str | None = None


SUITES: list[Suite] = [
    Suite(
        name="codex-exec/service",
        start_dir="containers/codex-exec/service",
        top_level_dir="containers/codex-exec",
        extra_pythonpath="containers/codex-exec",
    ),
    Suite(
        name="telegram-gateway",
        start_dir="containers/telegram-gateway/tests",
        top_level_dir="containers/telegram-gateway",
    ),
    Suite(
        name="skill:check-portfolio",
        start_dir="containers/codex-exec/profiles/base/skills/check-portfolio/tests",
        top_level_dir="containers/codex-exec/profiles/base/skills/check-portfolio",
    ),
    Suite(
        name="skill:show-touch-point",
        start_dir="containers/codex-exec/profiles/base/skills/show-touch-point/tests",
        top_level_dir="containers/codex-exec/profiles/base/skills/show-touch-point",
    ),
    Suite(
        name="shared-skill:collect-financial-information",
        start_dir="containers/codex-exec/shared-skills/collect-financial-information/tests",
        top_level_dir="containers/codex-exec/shared-skills/collect-financial-information",
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
    raise SystemExit(main())
