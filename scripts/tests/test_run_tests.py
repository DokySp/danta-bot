"""Behavior tests for the canonical repo-wide runner (scripts/run_tests.py).

This suite is discovered by run_tests.py itself as an ordinary suite
entry (see the "repo-tools:run_tests" Suite below); it does not invoke
run_tests.py as a subprocess, so there is no recursion. `subprocess.run`
is the only faked seam: it is the process boundary run_suite() shells
out through to invoke each real suite's `unittest discover`.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import run_tests


def fake_completed_process(
    cmd: list[str], stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, returncode=returncode, stdout=stdout, stderr=stderr)


class ParseTestCountTest(unittest.TestCase):
    """unittest's TextTestRunner writes its final "Ran N tests" summary to
    stderr. A test body under discovery can legitimately print its own
    unrelated "Ran N tests" text to stdout -- e.g. this exact runner's own
    suite, since MainAggregationTest below feeds run_tests.main() fabricated
    subprocess output containing "Ran 2 tests ... FAILED" and main() prints
    that verbatim on failure. Depending on the interpreter/platform, stdout
    and stderr can be flushed/interleaved in either order when merged into
    one stream, which previously made regex-searching a single merged blob
    version-dependent. Capturing the streams separately and always
    preferring stderr's last match removes that dependency entirely.
    """

    def test_prefers_outer_stderr_summary_over_nested_stdout_text(self) -> None:
        stdout = "...\nRan 2 tests in 0.01s\n\nFAILED (failures=1)\n"
        stderr = "........\n----------------------------------------------------------------------\nRan 8 tests in 0.03s\n\nOK\n"
        self.assertEqual(run_tests.parse_test_count(stdout, stderr), 8)

    def test_prefers_outer_stderr_summary_regardless_of_which_stream_is_checked_first_in_source_order(self) -> None:
        # Same scenario as above, but confirms the result does not depend on
        # incidental call-argument order: stderr always wins over stdout.
        stdout = "Ran 2 tests in 0.01s\n\nFAILED (failures=1)\n"
        stderr = "Ran 8 tests in 0.03s\n\nOK\n"
        self.assertEqual(run_tests.parse_test_count(stdout=stdout, stderr=stderr), 8)
        self.assertEqual(run_tests.parse_test_count(stdout, stderr), 8)

    def test_falls_back_to_stdout_when_stderr_has_no_match(self) -> None:
        stdout = "Ran 5 tests in 0.02s\n\nOK\n"
        stderr = ""
        self.assertEqual(run_tests.parse_test_count(stdout, stderr), 5)

    def test_takes_last_match_when_stderr_has_multiple_ran_lines(self) -> None:
        stderr = "Ran 2 tests in 0.00s\n\nOK\nRan 8 tests in 0.03s\n\nOK\n"
        self.assertEqual(run_tests.parse_test_count("", stderr), 8)

    def test_returns_zero_when_neither_stream_has_a_summary(self) -> None:
        self.assertEqual(run_tests.parse_test_count("no test summary here", "nor here"), 0)


class RunSuiteTest(unittest.TestCase):
    def suite(self, name: str = "example") -> run_tests.Suite:
        return run_tests.Suite(name=name, start_dir=f"{name}/tests", top_level_dir=name)

    def test_successful_discovery_is_marked_ok_with_parsed_count(self) -> None:
        stderr = "..........\n----------------------------------------------------------------------\nRan 10 tests in 0.05s\n\nOK\n"
        with patch("run_tests.subprocess.run", return_value=fake_completed_process([], stderr=stderr, returncode=0)):
            result = run_tests.run_suite(self.suite())

        self.assertTrue(result.ok)
        self.assertEqual(result.test_count, 10)

    def test_zero_tests_discovered_is_marked_not_ok_even_with_returncode_zero(self) -> None:
        stderr = "----------------------------------------------------------------------\nRan 0 tests in 0.00s\n\nOK\n"
        with patch("run_tests.subprocess.run", return_value=fake_completed_process([], stderr=stderr, returncode=0)):
            result = run_tests.run_suite(self.suite())

        self.assertFalse(result.ok)
        self.assertEqual(result.test_count, 0)

    def test_nonzero_returncode_is_marked_not_ok_even_with_tests_found(self) -> None:
        stderr = (
            "..F.......\n"
            "======================================================================\n"
            "FAIL: test_something\n"
            "----------------------------------------------------------------------\n"
            "Ran 7 tests in 0.02s\n\nFAILED (failures=1)\n"
        )
        with patch("run_tests.subprocess.run", return_value=fake_completed_process([], stderr=stderr, returncode=1)):
            result = run_tests.run_suite(self.suite())

        self.assertFalse(result.ok)
        self.assertEqual(result.test_count, 7)

    def test_unparseable_output_defaults_to_zero_tests(self) -> None:
        with patch("run_tests.subprocess.run", return_value=fake_completed_process([], stdout="no test summary here", returncode=0)):
            result = run_tests.run_suite(self.suite())

        self.assertFalse(result.ok)
        self.assertEqual(result.test_count, 0)

    def test_nested_stdout_ran_text_does_not_override_real_outer_stderr_count(self) -> None:
        """Reproduces the reported cross-version failure: a suite whose own
        tests print an incidental "Ran 2 tests ... FAILED" to stdout (this
        exact runner's suite does, via MainAggregationTest) alongside the
        real outer "Ran 8 tests ... OK" summary unittest writes to stderr.
        The parsed count must be the real outer count either way."""
        stdout = "...\nRan 2 tests in 0.01s\n\nFAILED (failures=1)\n"
        stderr = "........\n----------------------------------------------------------------------\nRan 8 tests in 0.03s\n\nOK\n"
        with patch(
            "run_tests.subprocess.run",
            return_value=fake_completed_process([], stdout=stdout, stderr=stderr, returncode=0),
        ):
            result = run_tests.run_suite(self.suite())

        self.assertTrue(result.ok)
        self.assertEqual(result.test_count, 8)

    def test_command_includes_start_and_top_level_dir_for_the_given_suite(self) -> None:
        stderr = "Ran 1 test in 0.00s\n\nOK\n"
        suite = self.suite("widgets")
        with patch("run_tests.subprocess.run", return_value=fake_completed_process([], stderr=stderr, returncode=0)) as run_mock:
            run_tests.run_suite(suite)

        cmd = run_mock.call_args.args[0]
        self.assertIn("-s", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "widgets/tests")
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "widgets")


class MainAggregationTest(unittest.TestCase):
    def suites(self) -> list[run_tests.Suite]:
        return [
            run_tests.Suite(name="alpha", start_dir="alpha/tests", top_level_dir="alpha"),
            run_tests.Suite(name="beta", start_dir="beta/tests", top_level_dir="beta"),
        ]

    def test_returns_zero_when_every_suite_passes(self) -> None:
        def fake_run(cmd, **kwargs):
            return fake_completed_process(cmd, stderr="Ran 3 tests in 0.01s\n\nOK\n", returncode=0)

        with patch("run_tests.subprocess.run", side_effect=fake_run):
            exit_code = run_tests.main(self.suites())

        self.assertEqual(exit_code, 0)

    def test_returns_nonzero_when_any_suite_fails(self) -> None:
        def fake_run(cmd, **kwargs):
            start_dir = cmd[cmd.index("-s") + 1]
            if start_dir == "beta/tests":
                return fake_completed_process(cmd, stderr="Ran 0 tests in 0.00s\n\nOK\n", returncode=0)
            return fake_completed_process(cmd, stderr="Ran 3 tests in 0.01s\n\nOK\n", returncode=0)

        with patch("run_tests.subprocess.run", side_effect=fake_run):
            exit_code = run_tests.main(self.suites())

        self.assertEqual(exit_code, 1)

    def test_returns_nonzero_when_a_suite_process_fails(self) -> None:
        def fake_run(cmd, **kwargs):
            start_dir = cmd[cmd.index("-s") + 1]
            if start_dir == "alpha/tests":
                return fake_completed_process(cmd, stderr="Ran 2 tests in 0.01s\n\nFAILED (failures=1)\n", returncode=1)
            return fake_completed_process(cmd, stderr="Ran 3 tests in 0.01s\n\nOK\n", returncode=0)

        with patch("run_tests.subprocess.run", side_effect=fake_run):
            exit_code = run_tests.main(self.suites())

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
