import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "prepare-codex-deployment.py"
spec = importlib.util.spec_from_file_location("prepare_deployment", SCRIPT)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class PrepareDeploymentTest(unittest.TestCase):
    def test_portable_bundle_has_no_build_or_authority_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "release"
            result = deployment.prepare(target, "example", "test-release",
                                        gateway_subnet="172.29.84.0/24", gateway_ip="172.29.84.9")
            self.assertEqual(result["status"], "PREPARED_NOT_AUTHORIZED")
            base = yaml.safe_load((target / "codex-exec/compose.yaml").read_text())
            self.assertNotIn("build", base["services"]["codex-exec"])
            self.assertEqual(deployment.load_config(target / "codex-exec/config").mode, "offline")
            shadow = yaml.safe_load((target / "codex-exec/config/app.shadow.yaml.example").read_text())
            self.assertEqual(shadow["app"]["mode"], "shadow")
            self.assertEqual(shadow["telegram"]["allowed_sender_ids"], [])
            self.assertFalse(shadow["execution"]["enabled"])
            self.assertFalse((target / "codex-exec/config/secrets.yaml").exists())
            self.assertIn("example/codex-exec:test-release", (target / "codex-exec/.env").read_text())
            self.assertIn("example/telegram-gateway:test-release", (target / "telegram-gateway/.env").read_text())
            approval = json.loads((target / "codex-exec/approvals/runtime.json.example").read_text())
            self.assertEqual(approval["capabilities"], [])
            manifest = json.loads((target / "codex-exec/approvals/runtime-manifest.json.example").read_text())
            self.assertFalse(manifest["verified"])
            self.assertFalse(manifest["bootstrap"]["ownership_verified"])
            peer = json.loads((target / "codex-exec/approvals/telegram-peer.json.example").read_text())
            self.assertFalse(peer["verified"])
            self.assertEqual(peer["allowed_source_ips"], ["172.29.84.9"])
            self.assertIn("DANTA_GATEWAY_SUBNET=172.29.84.0/24", (target / "telegram-gateway/.env").read_text())
            guide = (target / "README.md").read_text()
            self.assertIn("docker network create --subnet 172.29.84.0/24 danta-catalyst-net", guide)
            self.assertNotIn("172.30.85.", guide)
            for file in target.rglob("*"):
                if file.is_file():
                    self.assertNotIn(str(deployment.REPO), file.read_text())
            with self.assertRaises(FileExistsError):
                deployment.prepare(target, "example", "test-release")

    def test_secret_copy_is_explicit_private_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            engine, gateway = base / "engine", base / "gateway"
            for root, original in ((engine, deployment.ENGINE), (gateway, deployment.GATEWAY)):
                root.mkdir()
                for name in ("compose.yaml", "compose.auth.yaml", "compose.runtime.yaml", "telegram_gateway.py"):
                    if (original / name).exists():
                        shutil.copyfile(original / name, root / name)
                (root / "config").mkdir()
                for file in (original / "config").iterdir():
                    if file.name in {"app.yaml", "strategy.yaml", "schedules.yaml", "secrets.yaml.example",
                                     "routes.example.yaml", "telegram.env.example", "codex-peer.secret.example"}:
                        shutil.copyfile(file, root / "config" / file.name)
            shutil.copytree(deployment.ENGINE / "deployment", engine / "deployment")
            shutil.copytree(deployment.ENGINE / "prompts", engine / "prompts")
            key = "ab" * 32
            values = {"DANTA_TELEGRAM_PEER_SECRET": key, "KIS_APP_KEY": "SYNTHETIC_ONLY",
                      "TELEGRAM_GATEWAY_URL": "http://old-host", "DANTA_CODEX_AUTH_HOME": "/old-auth"}
            source = engine / "config/secrets.yaml"
            source.write_text(yaml.safe_dump(values))
            source.chmod(0o600)
            (gateway / "config/codex-peer.secret").write_text(key + "\n")
            (gateway / "config/codex-peer.secret").chmod(0o600)
            (gateway / "config/telegram-v1.env").write_text("TELEGRAM_BOT_TOKEN=SYNTHETIC_ONLY\nTELEGRAM_ALLOWED_CHAT_IDS=-12345\n")
            shutil.copyfile(gateway / "config/routes.example.yaml", gateway / "config/routes.yaml")
            (engine / "config/do-not-copy.json").write_text("PRIVATE_SENTINEL")
            original = source.read_bytes()
            with patch.object(deployment, "ENGINE", engine), patch.object(deployment, "GATEWAY", gateway):
                target = base / "release"
                deployment.prepare(target, "example", "test", include_secrets=True, sender_ids=["12345"])
                for relative in ("codex-exec/config/secrets.yaml", "telegram-gateway/config/telegram-v1.env", "telegram-gateway/config/codex-peer.secret"):
                    self.assertEqual((target / relative).stat().st_mode & 0o777, 0o600)
                copied = deployment.load_secrets(target / "codex-exec/config")
                self.assertEqual(copied["KIS_APP_KEY"], values["KIS_APP_KEY"])
                self.assertEqual(copied["TELEGRAM_GATEWAY_URL"], "http://telegram-gateway:8080")
                self.assertEqual(copied["DANTA_CODEX_AUTH_HOME"], "/app/auth")
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse((target / "codex-exec/config/do-not-copy.json").exists())
                shadow = yaml.safe_load((target / "codex-exec/config/app.shadow.yaml.example").read_text())
                self.assertEqual(shadow["telegram"]["allowed_sender_ids"], ["12345"])
                self.assertEqual(shadow["telegram"]["allowed_chat_ids"], ["-12345"])
                (gateway / "config/codex-peer.secret").write_text("cd" * 32)
                with self.assertRaises(ValueError):
                    deployment.prepare(base / "mismatch", "example", "test", include_secrets=True)
                self.assertFalse((base / "mismatch").exists())


if __name__ == "__main__":
    unittest.main()
