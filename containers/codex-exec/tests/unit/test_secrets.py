"""Private file loading uses synthetic values only and never contacts providers."""
import os
from pathlib import Path
import tempfile
import unittest

from danta.config import HumanRequired, ROOT, load_config, load_secrets


class SecretsTests(unittest.TestCase):
    def test_private_file_and_policy_snapshot_are_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('app', 'strategy', 'schedules'):
                (root / (name + '.yaml')).write_bytes((ROOT / 'config' / (name + '.yaml')).read_bytes())
            before = load_config(root)
            path = root / 'secrets.yaml'
            path.write_text('KIS_APP_KEY: "synthetic private value"\nDART_API_KEY: ""\n')
            path.chmod(0o600)
            self.assertEqual(load_secrets(root)['KIS_APP_KEY'], 'synthetic private value')
            after = load_config(root)
            self.assertEqual(before.config_hash, after.config_hash)
            self.assertNotIn('synthetic private value', after.snapshot_json)
            path.chmod(0o400)
            self.assertEqual(load_secrets(root)['DART_API_KEY'], '')

    def test_invalid_file_never_exposes_source_values(self):
        cases = ['KIS_APP_KEY: [private-canary', 'KIS_APP_KEY: private-canary\nKIS_APP_KEY: repeated',
                 'KIS_ACCOUNT_REF: 12345678', 'invalid-name: private-canary',
                 'KIS_APP_KEY: "' + 'x' * 65536 + '"']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.yaml'
            for content in cases:
                with self.subTest(case=cases.index(content)):
                    path.write_text(content)
                    path.chmod(0o600)
                    with self.assertRaises(HumanRequired) as error:
                        load_secrets(directory)
                    self.assertNotIn('private-canary', str(error.exception))
            path.write_text('KIS_APP_KEY: "private-canary"')
            path.chmod(0o644)
            with self.assertRaises(HumanRequired):
                load_secrets(directory)
            path.unlink()
            with self.assertRaises(HumanRequired):
                load_secrets(directory)
            target = Path(directory) / 'private-target'
            target.write_text('KIS_APP_KEY: "private-canary"')
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaises(HumanRequired):
                load_secrets(directory)
            path.unlink()
            os.mkfifo(path, 0o600)
            with self.assertRaises(HumanRequired):
                load_secrets(directory)


if __name__ == '__main__':
    unittest.main()
