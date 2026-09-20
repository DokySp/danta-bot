#!/usr/bin/env python3
"""Exercise built images and Compose on an isolated network before publishing."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import json, signal, sys, threading, time
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, '/app')
import telegram_gateway as gateway
app = gateway.GatewayApp(gateway.Config.from_env())
server = app.serve_http()
route = app.routing_store.get().routes['trading-engine']
client = Mock()
deadline = time.monotonic() + 30
while True:
    try:
        version = app.engine.get_version(route.url)
        if version['runtime_status'] != 'STARTING':
            break
    except (OSError, RuntimeError):
        pass
    assert time.monotonic() < deadline, 'engine startup timeout'
    time.sleep(.2)
with patch.object(gateway, 'TelegramClient', return_value=client):
    for update_id, text in enumerate(('/version', '/status', '/pause'), 1):
        app.handle_update(route, {'update_id': update_id, 'message': {
            'message_id': update_id, 'chat': {'id': 12345}, 'from': {'id': 12345}, 'text': text}})
    replies = [call.args[1] for call in client.send_message.call_args_list]
    assert len(replies) == 3, replies
    assert 'trading-engine' in replies[0] and 'telegram-gateway' in replies[0], replies[0]
    assert 'READY' in replies[0], replies[0]
    assert '요청을 접수했습니다' in replies[1], replies[1]
    assert '요청을 접수했습니다' in replies[2], replies[2]
    count = client.send_message.call_count
    app.handle_update(route, {'update_id': 99, 'message': {
        'message_id': 99, 'chat': {'id': 67890}, 'from': {'id': 67890}, 'text': '/version'}})
    assert client.send_message.call_count == count
Path('/workspace/memory/smoke.json').write_text(json.dumps({'status':'PASS',
    'version_round_trip':True, 'live_runtime_ready':True, 'denied_chat_blocked':True,
    'telegram_delivery':'STUBBED', 'broker_provider':'SYNTHETIC_TRANSPORT','model_auth':'SYNTHETIC_PROBE'}))
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())
stop.wait()
server.shutdown()
server.server_close()
'''


def command(*args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr[-8000:] or 'Docker command failed')
    return result.stdout


def verify(engine_image, gateway_image):
    for image in (engine_image, gateway_image):
        command('docker', 'image', 'inspect', image)
    # This location is shared with Docker Desktop; /tmp bind mounts may not be.
    parent = ROOT / 'containers/trading-engine/var'
    parent.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='deployment-check-', dir=parent)).resolve()
    project = 'danta-check-' + uuid.uuid4().hex[:10]
    projects = {name: ['docker', 'compose', '-p', project + '-' + name, '-f', str(temporary / (name + '.json'))]
                for name in ('trading-engine', 'telegram-gateway')}
    compose, gateway_compose = projects['trading-engine'], projects['telegram-gateway']
    spec = {'services': {}, 'volumes': {}, 'networks': {'default': {'name': project, 'internal': True}}}
    try:
        for name, image in (('trading-engine', engine_image), ('telegram-gateway', gateway_image)):
            source = ROOT / 'containers' / name
            original = json.loads(command('docker', 'compose', '-f', str(source / 'compose.yaml'),
                                          'config', '--format', 'json'))
            service = original['services'][name]
            service.update(image=image, container_name=project + '-' + name)
            service.pop('ports', None)
            service['networks'] = {'default': {'aliases': [name]}}
            for mount in service.get('volumes', []):
                if mount['type'] == 'bind':
                    leaf = 'config' if mount['target'] in {'/app/config', '/root/danta-config'} else Path(mount['source']).name
                    mount['source'] = str(temporary / name / leaf)
                    Path(mount['source']).mkdir(parents=True, exist_ok=True)
            spec['services'][name] = service
            for key in original.get('volumes', {}):
                spec['volumes'][key] = {'name': project + '-' + key}
            if name == 'trading-engine':
                for filename in ('app.yaml', 'strategy.yaml', 'schedules.yaml'):
                    shutil.copyfile(source / 'config' / filename, temporary / name / 'config' / filename)
                configdir = temporary / name / 'config'
                shutil.copyfile(source / 'tests/fixtures/deployment_probe.py', configdir / 'deployment_probe.py')
                (configdir / 'sitecustomize.py').write_text('from deployment_probe import install\ninstall()\n')
                (configdir / 'secrets.yaml').write_text('KIS_ACCOUNT_REF: "00000000-00"\n'
                    'KIS_APP_KEY: "FAKE_KEY_NOT_A_CREDENTIAL"\nKIS_APP_SECRET: "FAKE_SECRET_NOT_A_CREDENTIAL"\n'
                    'DART_API_KEY: "FAKE_DART_NOT_A_CREDENTIAL"\nDANTA_CODEX_AUTH_HOME: "/app/auth"\n'
                    'TELEGRAM_GATEWAY_URL: "http://telegram-gateway:8080"\n'
                    'TELEGRAM_ALLOWED_CHAT_IDS: "12345"\nTELEGRAM_ALLOWED_SENDER_IDS: "12345"\n')
                (configdir / 'secrets.yaml').chmod(0o600)
                service.setdefault('environment', {})['PYTHONPATH'] = '/app/config'
            else:
                shutil.copyfile(source / 'config/routes.example.yaml', temporary / name / 'config/routes.yaml')
                (temporary / name / 'config/telegram.env').write_text(
                    'TELEGRAM_BOT_TOKEN=SYNTHETIC_ONLY\nTELEGRAM_ALLOWED_CHAT_IDS=12345\n')
                (temporary / name / 'config/probe.py').write_text(PROBE)
                # Run the real gateway HTTP/router/client; replace only public Telegram delivery.
                service['command'] = ['python', '/app/config/probe.py']
        for name, service in spec['services'].items():
            (temporary / (name + '.json')).write_text(json.dumps({**spec, 'services': {name: service}}))
            command(*projects[name], 'up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '45')
        deadline = time.monotonic() + 45
        result = None
        while time.monotonic() < deadline:
            probe = subprocess.run([*gateway_compose, 'exec', '-T', 'telegram-gateway', 'cat',
                                    '/workspace/memory/smoke.json'], text=True, capture_output=True)
            if probe.returncode == 0:
                result = json.loads(probe.stdout)
                break
            # Wait only for startup; commands themselves are never replayed.
            time.sleep(.5)
        if result is None:
            raise RuntimeError('Deployment HTTP smoke test did not complete')
        logs = command(*compose, 'logs', '--no-color', 'trading-engine')
        if 'HTTP_LISTENING' not in logs or 'INITIALIZATION_COMPLETE' not in logs:
            raise RuntimeError('Deployment startup/readiness logs are missing')
        state = json.loads(command('docker', 'inspect', project + '-trading-engine'))[0]['State']
        if not state['Running'] or state['Health']['Status'] != 'healthy':
            raise RuntimeError('Engine health check failed')
        command(*compose, 'exec', '-T', '--user', '10001:10001', 'trading-engine', 'python', '-c',
            'from pathlib import Path\n'
            'matches = [p for p in Path("/proc").glob("[0-9]*/cmdline") if b"/usr/local/bin/danta\\x00" in p.read_bytes()]\n'
            'assert len(matches) == 1\n'
            'fields = dict(line.split(":",1) for line in (matches[0].parent/"status").read_text().splitlines())\n'
            'assert fields["Uid"].split() == ["10001"]*4\n'
            'assert all(int(fields[key],16) == 0 for key in ("CapEff","CapPrm","CapAmb"))\n')
        # A clean SIGTERM must terminate, not require SIGKILL and leave the writer lock behind.
        command(*compose, 'stop', '-t', '15', 'trading-engine')
        state = json.loads(command('docker', 'inspect', project + '-trading-engine'))[0]['State']
        if state['ExitCode'] != 0:
            raise RuntimeError('Engine did not stop cleanly')
        result['separate_compose_projects'] = True
        return result
    except Exception:
        for name, commands in projects.items():
            if not (temporary / (name + '.json')).exists():
                continue
            logs = subprocess.run([*commands, 'logs', '--no-color'], text=True, capture_output=True)
            print(logs.stdout[-12000:])  # The entire project contains synthetic configuration only.
        raise
    finally:
        for name, commands in reversed(list(projects.items())):
            if (temporary / (name + '.json')).exists():
                subprocess.run([*commands, 'down', '--volumes', '--remove-orphans'], capture_output=True)
        # Root bootstrap owns the synthetic config; restore only this generated temporary tree.
        subprocess.run(['docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--user', '0:0',
            '--entrypoint', 'python', '-v', str(temporary) + ':/cleanup', engine_image, '-c',
            'import os\nfor root, dirs, files in os.walk("/cleanup"):\n'
            f' os.chown(root, {os.getuid()}, {os.getgid()})\n'
            f' for name in files: os.chown(os.path.join(root,name), {os.getuid()}, {os.getgid()})'],
            capture_output=True)
        shutil.rmtree(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine-image')
    parser.add_argument('--gateway-image')
    parser.add_argument('--version', default='deployment-check')
    args = parser.parse_args()
    for name, image in (('trading-engine', args.engine_image), ('telegram-gateway', args.gateway_image)):
        if image is None:
            image = name + ':deployment-check'
            command('docker', 'build', '--build-arg', 'APP_VERSION=' + args.version, '-t', image,
                    str(ROOT / 'containers' / name))
            if name == 'trading-engine':
                args.engine_image = image
            else:
                args.gateway_image = image
    print(json.dumps(verify(args.engine_image, args.gateway_image)))
