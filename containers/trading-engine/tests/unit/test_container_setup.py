"""Container setup preserves data and keeps approval validation on the CLI path."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from danta.cli import main
from danta.config import HumanRequired
from danta.container_init import initialize
from danta.container_init import main as container_main


class ContainerSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("config", "var", "auth", "locks"):
            (self.root / name).mkdir()
        for name in ("app.yaml", "strategy.yaml", "schedules.yaml", "secrets.yaml", "runtime.json"):
            (self.root / "config" / name).write_text('{"id": "SYNTHETIC_ONLY"}')

    def test_permissions_are_repeatable_without_rewriting_configuration_or_data(self):
        state = self.root / "var/state.sqlite"
        state.write_bytes(b"EXISTING_DATA")
        state.chmod(0o640)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with patch("danta.container_init.os.fchown") as chown:
            initialize(self.root, self.root / "locks")
            initialize(self.root, self.root / "locks")
        self.assertIn(10001, [call.args[1] for call in chown.call_args_list])
        self.assertIn(0, [call.args[1] for call in chown.call_args_list])
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        for name, mode in (("locks", 0o700), ("var", 0o700), ("auth", 0o700),
                           ("config", 0o755), ("config/secrets.yaml", 0o400), ("config/runtime.json", 0o444)):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, mode)
        self.assertEqual(state.stat().st_mode & 0o777, 0o640)

    def test_secret_symlink_and_hardlink_cannot_change_another_file(self):
        target = self.root / "unrelated"
        target.write_text("UNCHANGED")
        target.chmod(0o600)
        secret = self.root / "config/secrets.yaml"
        for link in (lambda: secret.symlink_to(target), lambda: os.link(target, secret)):
            secret.unlink()
            link()
            with patch("danta.container_init.os.fchown"), self.assertRaises((OSError, ValueError)):
                initialize(self.root, self.root / "locks")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.read_text(), "UNCHANGED")

    def test_config_approval_is_validated_and_explicit_override_still_works(self):
        default = self.root / "config/runtime.json"
        explicit = self.root / "explicit.json"
        explicit.write_text(default.read_text())
        for extra, expected in (([], default), (["--approval-file", str(explicit)], explicit)):
            with patch("danta.cli.load_config", return_value=SimpleNamespace(mode="shadow")), \
                    patch("danta.cli.trusted_approval", side_effect=HumanRequired("SYNTHETIC_INVALID")) as trusted, \
                    redirect_stdout(io.StringIO()) as output:
                status = main(["--config-dir", str(self.root / "config"), *extra, "approvals", "show"])
            self.assertEqual(status, 2)
            self.assertEqual(trusted.call_args.args[0], expected)
            self.assertEqual(json.loads(output.getvalue())["status"], "WAITING_FOR_HUMAN")

    def test_offline_service_ignores_default_approval_and_shadow_loads_it(self):
        for mode, expected in (("offline", None), ("shadow", self.root / "config/runtime.json")):
            with patch("danta.cli.load_config", return_value=SimpleNamespace(mode=mode)), \
                    patch("danta.service.serve") as serve, redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--config-dir", str(self.root / "config"), "serve"]), 0)
            self.assertEqual(serve.call_args.args[1].approval_file, expected)

    def test_entrypoint_prepares_and_drops_privileges_before_engine_or_login(self):
        for args, command in ((["serve"], ["danta", "serve"]),
                              (["codex", "login", "status"], ["codex", "login", "status"])):
            calls = []
            with patch("danta.container_init.os.getuid", return_value=0), \
                    patch("danta.container_init.initialize", side_effect=lambda **kw: calls.append(("prepare", kw))), \
                    patch("danta.container_init.os.setgroups", side_effect=lambda value: calls.append(("groups", value))), \
                    patch("danta.container_init.os.setgid", side_effect=lambda value: calls.append(("gid", value))), \
                    patch("danta.container_init.os.setuid", side_effect=lambda value: calls.append(("uid", value))), \
                    patch("danta.container_init.os.execvp", side_effect=lambda *value: calls.append(("exec", value))), \
                    patch("sys.argv", ["entrypoint", *args]):
                container_main()
            self.assertEqual(calls, [("prepare", {"config": Path("/root/danta-config")}),
                                     ("groups", []), ("gid", 10001), ("uid", 10001),
                                     ("exec", (command[0], command))])

    def test_entrypoint_does_not_run_engine_when_initialization_fails(self):
        with patch("danta.container_init.os.getuid", return_value=0), \
                patch("danta.container_init.initialize", side_effect=PermissionError), \
                patch("danta.container_init.os.execvp") as execute:
            with self.assertRaises(PermissionError):
                container_main()
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
