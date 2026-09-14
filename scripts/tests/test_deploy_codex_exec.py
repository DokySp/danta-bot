"""Exercise deployment scripts in a temporary repo with no real Docker or network."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]


class DeployCodexExecTest(unittest.TestCase):
    def test_tags_context_alias_and_fail_closed_gate(self):
        cases = [
            ("deploy-codex-exec.sh", ["example", "v1"], 0, 0, 0, "codex-exec", "v1"),
            ("deploy-codex-exec.sh", ["example"], 0, 0, 0, "codex-exec", "latest"),
            ("deploy-codex-exec-experimental.sh", ["example", "v2"], 0, 0, 0, "codex-exec-experimental", "v2"),
            ("deploy-codex-exec.sh", ["example", "v1"], 1, 0, 1, "codex-exec", "v1"),
            ("deploy-codex-exec.sh", ["example", "v1"], 0, 1, 1, "codex-exec", "v1"),
            ("deploy-codex-exec.sh", [], 0, 0, 64, "codex-exec", "latest"),
        ]
        for script, args, test_exit, build_exit, exit_code, image_name, tag in cases:
            with self.subTest(script=script, args=args, test_exit=test_exit, build_exit=build_exit):
                with tempfile.TemporaryDirectory(prefix="danta-deploy-test-") as temporary:
                    root = Path(temporary)
                    (root / "scripts").mkdir()
                    commands = root / "commands"
                    commands.mkdir()
                    for name in ("deploy-codex-exec.sh", "deploy-codex-exec-experimental.sh"):
                        shutil.copy2(SCRIPTS / name, root / "scripts" / name)
                    for name in ("test-python", "git", "docker", "curl"):
                        command = commands / name
                        command.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["STUB_LOG"], "a") as stream:
    stream.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "test-python":
    sys.exit(int(os.environ["STUB_TEST_EXIT"]))
if name == "git":
    print("git-fixture-version")
if name == "docker" and sys.argv[1] == "build":
    sys.exit(int(os.environ["STUB_BUILD_EXIT"]))
if name == "curl":
    sys.exit(99)
''')
                        command.chmod(0o755)
                    log = root / "commands.jsonl"
                    result = subprocess.run(
                        ["/bin/bash", str(root / "scripts" / script), *args],
                        cwd=root,
                        env={
                            "PATH": f"{commands}{os.pathsep}{os.defpath}",
                            "PYTHON_BIN": str(commands / "test-python"),
                            "STUB_LOG": str(log),
                            "STUB_TEST_EXIT": str(test_exit),
                            "STUB_BUILD_EXIT": str(build_exit),
                        },
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, exit_code, result.stderr)
                    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
                    self.assertFalse(any(call[0] == "curl" for call in calls))
                    docker_calls = [call for call in calls if call[0] == "docker"]
                    if not args or test_exit:
                        self.assertEqual(docker_calls, [])
                        continue
                    build = docker_calls[0]
                    self.assertLess(
                        next(i for i, call in enumerate(calls) if call[0] == "test-python"),
                        calls.index(build),
                    )
                    self.assertEqual(build[-1], str(root / "containers" / "codex-exec"))
                    self.assertIn(f"example/{image_name}:{tag}", build)
                    self.assertIn(f"{image_name}:{tag}", build)
                    version = args[1] if len(args) == 2 else "git-fixture-version"
                    self.assertIn(f"APP_VERSION={version}", build)
                    self.assertFalse(any(arg.startswith(("CODEX_VERSION=", "CODEX_EXEC_PROFILE=", "IMAGE_TITLE=")) for arg in build))
                    if build_exit:
                        self.assertEqual(len(docker_calls), 1)
                    else:
                        self.assertEqual(docker_calls[1:], [["docker", "push", f"example/{image_name}:{tag}"]])


if __name__ == "__main__":
    unittest.main()
