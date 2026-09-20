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


class DeployTradingEngineTest(unittest.TestCase):
    def test_tags_context_alias_and_fail_closed_gate(self):
        scripts = {
            "deploy-trading-engine.sh": ("trading-engine", "trading-engine"),
            "deploy-trading-engine-experimental.sh": ("trading-engine-experimental", "trading-engine"),
            "deploy-telegram-gateway.sh": ("telegram-gateway", "telegram-gateway"),
        }
        scenarios = [
            (["example", "v1"], 0, "", 0),
            (["example"], 0, "", 0),
            (["example", "latest"], 0, "", 0),
            (["example", "v1"], 1, "", 1),
            (["example", "v1"], 0, "build", 1),
            (["example", "v1"], 0, "push-release", 1),
            (["example", "v1"], 0, "tag", 1),
            (["example", "v1"], 0, "push-latest", 1),
            (["example"], 0, "push-latest", 1),
            ([], 0, "", 64),
        ]
        for script, args, test_exit, docker_failure, exit_code in (
                (script, *scenario) for script in scripts for scenario in scenarios):
            image_name, context = scripts[script]
            tag = args[1] if len(args) == 2 else "latest"
            with self.subTest(script=script, args=args, test_exit=test_exit, docker_failure=docker_failure):
                with tempfile.TemporaryDirectory(prefix="danta-deploy-test-") as temporary:
                    root = Path(temporary)
                    (root / "scripts").mkdir()
                    commands = root / "commands"
                    commands.mkdir()
                    for name in scripts:
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
if name == "docker":
    action = sys.argv[1]
    if action == "push":
        action += "-latest" if sys.argv[2].endswith(":latest") else "-release"
    if action == os.environ["STUB_DOCKER_FAILURE"]:
        sys.exit(1)
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
                            "STUB_DOCKER_FAILURE": docker_failure,
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
                    self.assertEqual(build[-1], str(root / "containers" / context))
                    self.assertIn(f"example/{image_name}:{tag}", build)
                    self.assertIn(f"{image_name}:{tag}", build)
                    version = args[1] if len(args) == 2 else "git-fixture-version"
                    self.assertIn(f"APP_VERSION={version}", build)
                    self.assertFalse(any(arg.startswith(("CODEX_VERSION=", "CODEX_EXEC_PROFILE=", "IMAGE_TITLE=")) for arg in build))
                    remote_image = f"example/{image_name}:{tag}"
                    latest_image = f"example/{image_name}:latest"
                    expected = [build, ["docker", "push", remote_image]]
                    if tag != "latest":
                        expected.extend([["docker", "tag", remote_image, latest_image],
                                         ["docker", "push", latest_image]])
                    if docker_failure:
                        count = {"build": 1, "push-release": 2, "tag": 3,
                                 "push-latest": 2 if tag == "latest" else 4}[docker_failure]
                        expected = expected[:count]
                    self.assertEqual(docker_calls, expected)


if __name__ == "__main__":
    unittest.main()
