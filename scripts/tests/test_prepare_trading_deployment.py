import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "prepare-trading-deployment.py"
spec = importlib.util.spec_from_file_location("prepare_deployment", SCRIPT)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class PrepareDeploymentTest(unittest.TestCase):
    def test_portable_bundle_uses_live_automatic_config_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "release"
            result = deployment.prepare(target, "example", "test-release")
            self.assertEqual(result["status"], "PREPARED")
            base = yaml.safe_load((target / "trading-engine/compose.yaml").read_text())
            self.assertNotIn("build", base["services"]["trading-engine"])
            prepared = deployment.load_config(target / "trading-engine/config")
            self.assertEqual(prepared.mode, "live")
            self.assertEqual(prepared.app["broker"]["capability_manifest"], "automatic")
            self.assertEqual(prepared.app["market"]["calendar_manifest"], "automatic")
            self.assertEqual(prepared.app["telegram"]["allowed_sender_ids"], [])
            self.assertEqual(prepared.app["telegram"]["route"], "trading-engine")
            self.assertTrue(prepared.app["execution"]["enabled"])
            self.assertFalse((target / "trading-engine/config/secrets.yaml").exists())
            self.assertEqual(base["services"]["trading-engine"]["image"], "example/trading-engine:test-release")
            self.assertEqual(set(base["services"]), {"trading-engine"})
            self.assertEqual(base["services"]["trading-engine"]["container_name"], "trading-engine")
            self.assertEqual(base["services"]["trading-engine"]["entrypoint"], ["python", "-m", "danta.container_init"])
            gateway = yaml.safe_load((target / "telegram-gateway/compose.yaml").read_text())
            self.assertEqual(gateway["services"]["telegram-gateway"]["image"], "example/telegram-gateway:test-release")
            for compose in (base, gateway):
                self.assertEqual(compose["networks"]["default"], {"name": "danta-bot-net"})
            for name in ("trading-engine", "telegram-gateway"):
                self.assertEqual({p.name for p in (target / name).iterdir()}, {"compose.yaml", "config"})
            self.assertFalse(list((target / "trading-engine/config").glob("runtime*")))
            self.assertFalse(list((target / "trading-engine/config").glob("*shadow*")))
            routes = yaml.safe_load((target / "telegram-gateway/config/routes.yaml").read_text())
            self.assertEqual(set(routes["routes"]), {"trading-engine"})
            self.assertEqual(routes["routes"]["trading-engine"]["env_file"], "/app/config/telegram.env")
            self.assertTrue((target / "telegram-gateway/config/telegram.env.example").is_file())
            guide = (target / "README.md").read_text()
            self.assertNotIn("docker network create", guide)
            self.assertIn("--remove-orphans", guide)
            for file in target.rglob("*"):
                if file.is_file():
                    self.assertNotIn(str(deployment.REPO) + "/", file.read_text())
            with self.assertRaises(FileExistsError):
                deployment.prepare(target, "example", "test-release")

    def test_secret_copy_is_explicit_private_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            engine, gateway = base / "engine", base / "gateway"
            for root, original in ((engine, deployment.ENGINE), (gateway, deployment.GATEWAY)):
                root.mkdir()
                for name in ("compose.yaml", "telegram_gateway.py"):
                    if (original / name).exists():
                        shutil.copyfile(original / name, root / name)
                (root / "config").mkdir()
                for file in (original / "config").iterdir():
                    if file.name in {"app.yaml", "strategy.yaml", "schedules.yaml", "secrets.yaml.example",
                                     "routes.example.yaml", "telegram.env.example"}:
                        shutil.copyfile(file, root / "config" / file.name)
            shutil.copytree(deployment.ENGINE / "deployment", engine / "deployment")
            shutil.copytree(deployment.ENGINE / "prompts", engine / "prompts")
            key = "ab" * 32
            values = {"DANTA_TELEGRAM_PEER_SECRET": key, "KIS_APP_KEY": "SYNTHETIC_ONLY",
                      "TELEGRAM_GATEWAY_URL": "http://old-host", "DANTA_CODEX_AUTH_HOME": "/old-auth"}
            source = engine / "config/secrets.yaml"
            source.write_text(yaml.safe_dump(values))
            source.chmod(0o600)
            (gateway / "config/telegram.env").write_text("TELEGRAM_BOT_TOKEN=SYNTHETIC_ONLY\nTELEGRAM_ALLOWED_CHAT_IDS=-12345\n")
            shutil.copyfile(gateway / "config/routes.example.yaml", gateway / "config/routes.yaml")
            (engine / "config/do-not-copy.json").write_text("PRIVATE_SENTINEL")
            original = source.read_bytes()
            runtime = '{"id":"SYNTHETIC_EXISTING_APPROVAL"}\n'
            (engine / "config/runtime.json").write_text(runtime)
            with patch.object(deployment, "ENGINE", engine), patch.object(deployment, "GATEWAY", gateway):
                target = base / "release"
                deployment.prepare(target, "example", "test", include_secrets=True, sender_ids=["12345"])
                for relative in ("trading-engine/config/secrets.yaml", "telegram-gateway/config/telegram.env"):
                    self.assertEqual((target / relative).stat().st_mode & 0o777, 0o600)
                copied = deployment.load_secrets(target / "trading-engine/config")
                self.assertEqual(copied["KIS_APP_KEY"], values["KIS_APP_KEY"])
                self.assertEqual(copied["TELEGRAM_GATEWAY_URL"], "http://telegram-gateway:8080")
                self.assertEqual(copied["DANTA_CODEX_AUTH_HOME"], "/app/auth")
                self.assertNotIn("DANTA_TELEGRAM_PEER_SECRET", copied)
                self.assertFalse((target / "telegram-gateway/config/codex-peer.secret").exists())
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse((target / "trading-engine/config/runtime.json").exists())
                self.assertFalse((target / "trading-engine/config/do-not-copy.json").exists())
                self.assertEqual(copied["TELEGRAM_ALLOWED_SENDER_IDS"], "12345")
                self.assertEqual(copied["TELEGRAM_ALLOWED_CHAT_IDS"], "-12345")
                app = deployment.load_config(target / "trading-engine/config").app
                self.assertEqual(app["telegram"]["allowed_sender_ids"], [])
                self.assertEqual(app["telegram"]["allowed_chat_ids"], [])
                with self.assertRaises(ValueError):
                    deployment.prepare(base / "group-without-sender", "example", "test", include_secrets=True)
                self.assertFalse((base / "group-without-sender").exists())
                (gateway / "config/telegram.env").write_text("TELEGRAM_BOT_TOKEN=SYNTHETIC_ONLY\nTELEGRAM_ALLOWED_CHAT_IDS=12345,67890\n")
                inferred = base / "private-chats"
                deployment.prepare(inferred, "example", "test", include_secrets=True)
                inferred_secrets = deployment.load_secrets(inferred / "trading-engine/config")
                self.assertEqual(inferred_secrets["TELEGRAM_ALLOWED_CHAT_IDS"], "12345,67890")
                self.assertEqual(inferred_secrets["TELEGRAM_ALLOWED_SENDER_IDS"], "12345,67890")
                routes = yaml.safe_load((gateway / "config/routes.yaml").read_text())
                routes["routes"]["v2"] = dict(routes["routes"]["trading-engine"])
                (gateway / "config/routes.yaml").write_text(yaml.safe_dump(routes))
                with self.assertRaises(ValueError):
                    deployment.prepare(base / "multi-route", "example", "test", include_secrets=True)
                self.assertFalse((base / "multi-route").exists())


if __name__ == "__main__":
    unittest.main()
