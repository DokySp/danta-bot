#!/usr/bin/env python3
"""Run one fresh isolated image against only the repository's offline defaults."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

PROBE = r'''
import contextlib, io, json, os, pathlib, socket, sys, threading
from datetime import datetime, timezone
from types import SimpleNamespace
from danta.cli import main, make_application
from danta.config import load_config
from danta.application import code_identity
from danta.reporting import render_readme
from danta.service import serve
socket.socket = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('network forbidden'))
configdir = pathlib.Path('/app/var/config')
configdir.mkdir(parents=True)
for name, body in json.load(sys.stdin).items():
    assert name in {'app.yaml', 'strategy.yaml', 'schedules.yaml'}
    (configdir / name).write_text(body)
config = load_config(configdir)
assert config.mode == 'offline'
assert not config.app['execution']['enabled']
assert not config.app['monitoring']['enabled']
assert not config.app['telegram']['enabled']
assert not config.app['telegram']['ingress_enabled']
def command(args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(['--config-dir', str(configdir)] + args)
    result = json.loads(out.getvalue())
    if code:
        raise AssertionError((args, result))
    return result
doctor = command(['doctor'])
result = command(['run', '--kind', 'full_review', '--request-key', 'offline-container-validation'])
assert result['provenance'] == 'FIXTURE_ONLY'
assert result['run_status'] == 'COMPLETE'
assert result['order_status'] == 'FIXTURE_FILLED'
again = command(['run', '--kind', 'full_review', '--request-key', 'offline-container-validation'])
assert again['run_id'] == result['run_id']
status = command(['status'])
report = command(['report'])
design = render_readme('/app/README.md', '/app/var/report.html')
validation = {
    'tested_at': datetime.now(timezone.utc).isoformat(), 'uid': os.getuid(),
    'image_code_id': code_identity(), 'doctor': doctor,
    'synthetic_run_status': result['run_status'], 'synthetic_order_status': result['order_status'],
    'same_request_reused': True, 'holdings_count': len(status['holdings']),
    'offline_cli_no_socket': True, 'readme_sha256': design['source_sha256'],
    'design_heading_count': design['heading_count'],
}
print(json.dumps({'validation': validation, 'artifacts': {
    'report.html': pathlib.Path('/app/var/report.html').read_text(),
    'daily.html': pathlib.Path(report['html']).read_text(),
    'daily.json': pathlib.Path(report['json']).read_text(),
}}, ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    from danta.safety import reject_credentials
    import yaml
    files = {name: (args.root / 'tests/fixtures/config' / name).read_text() for name in ('app.yaml', 'strategy.yaml', 'schedules.yaml')}
    for body in files.values():
        reject_credentials(yaml.safe_load(body))
    app = yaml.safe_load(files['app.yaml'])
    assert app['app']['mode'] == 'offline'
    for section in ('execution', 'monitoring', 'telegram'):
        assert app[section]['enabled'] is False
    assert app['telegram']['ingress_enabled'] is False
    evidence = Path(tempfile.mkdtemp(prefix='danta-service-image-validation-stdin-'))
    image_id = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.image], capture_output=True, text=True, check=True).stdout.strip()
    cmd = [
        'docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '128',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=64m,mode=1777',
        '--tmpfs', '/app/var:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=0700',
        '--tmpfs', '/tmp/danta-writers-10001:rw,noexec,nosuid,size=1m,uid=10001,gid=10001,mode=0700',
        '--entrypoint', 'python', image_id, '-c', PROBE,
    ]
    proc = subprocess.run(cmd, input=json.dumps(files), capture_output=True, text=True)
    (evidence / 'container.stderr.txt').write_text(proc.stderr)
    if proc.returncode:
        (evidence / 'container.stdout.txt').write_text(proc.stdout)
        print(json.dumps({'result': 'FAILED', 'exit_code': proc.returncode, 'evidence': str(evidence)}))
        return proc.returncode
    result = json.loads(proc.stdout)
    for name, body in result['artifacts'].items():
        assert name in {'report.html', 'daily.html', 'daily.json'}
        (evidence / name).write_text(body)
    result['validation'].update(image_tag=args.image, image_id=image_id, isolation='nonroot_readonly_network_none_no_capabilities_no_new_privileges')
    (evidence / 'validation.json').write_text(json.dumps(result['validation'], ensure_ascii=False, indent=2))
    print(json.dumps({'result': 'PASS', 'evidence': str(evidence), **result['validation']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
