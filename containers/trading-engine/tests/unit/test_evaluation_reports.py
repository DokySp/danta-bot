"""CLI evaluation exports preserve frozen facts; all inputs here are synthetic."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from danta.cli import main
from danta.evaluation import EvaluationManifest, content_hash


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/evaluation-synthetic.json'


class EvaluationReportTests(unittest.TestCase):
    def invoke(self, manifest, state, *, command='evaluate', mode='offline'):
        config = SimpleNamespace(mode=mode, state_dir=state)
        with patch('danta.cli.load_config', return_value=config), \
                patch('danta.cli.make_application', side_effect=AssertionError('No runtime calls')), \
                redirect_stdout(io.StringIO()) as output:
            status = main([command, '--manifest', str(manifest)])
        return status, json.loads(output.getvalue())

    def test_cli_exports_same_manifest_metrics_with_safe_path_and_no_overwrite(self):
        data = json.loads(FIXTURE.read_text())
        data['experiment_id'] = '../escape/<script>alert(1)</script>'
        data['run_id'] = '/untrusted/run'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(data))
            status, result = self.invoke(manifest, root / 'state')
            self.assertEqual(status, 0)
            frozen = EvaluationManifest.model_validate_json(manifest.read_text()).model_dump(mode='json')
            self.assertEqual(result['manifest'], frozen)
            self.assertEqual(result['evaluation_id'], content_hash(frozen))
            directory = root / 'state/evaluations' / result['evaluation_id']
            self.assertEqual(Path(result['artifacts']['html']), directory / 'report.html')
            self.assertEqual(Path(result['artifacts']['json']), directory / 'result.json')
            self.assertEqual(json.loads((directory / 'result.json').read_text()), result)
            self.assertEqual(result['manifest']['operating_cost_krw'], None)
            self.assertEqual(result['confirmation_return'], None)
            self.assertEqual(result['evidence_status'], 'FIXTURE_ONLY')
            self.assertEqual(result['verdict'], 'INCONCLUSIVE')
            self.assertFalse(result['automatic_live_promotion'])
            rendered = (directory / 'report.html').read_text()
            for value in ('합성 fixture 검증 전용', '실제 투자 성과가 아닙니다',
                          '미확인 / 자료 없음', 'STRATEGY_UNPROVEN', 'INCONCLUSIVE',
                          result['discovery_net_pnl_krw'], result['manifest_hash'],
                          '&lt;script&gt;alert(1)&lt;/script&gt;'):
                self.assertIn(value, rendered)
            self.assertNotIn('<script>', rendered)
            original = {path.name: path.read_bytes() for path in directory.iterdir()}
            status, repeated = self.invoke(manifest, root / 'state')
            self.assertEqual(status, 1)
            self.assertEqual(repeated['error_type'], 'FileExistsError')
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, original)
            self.assertEqual(list((root / 'state/evaluations').iterdir()), [directory])

    def test_replay_remains_read_only_and_external_modes_require_separate_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            status, result = self.invoke(FIXTURE, state, command='replay')
            self.assertEqual(status, 0)
            self.assertEqual(result['engine_status'], 'ENGINE_EXECUTED')
            self.assertNotIn('artifacts', result)
            for mode in ('paper', 'live'):
                with self.subTest(mode=mode):
                    status, result = self.invoke(FIXTURE, state, mode=mode)
                    self.assertEqual(status, 2)
                    self.assertIn('isolated offline manifests', result['reason'])
            self.assertFalse(state.exists())


if __name__ == '__main__':
    unittest.main()
