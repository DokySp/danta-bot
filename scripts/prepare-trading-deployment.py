#!/usr/bin/env python3
"""Prepare a new, private directory for transfer to a separate Docker host."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys

import yaml

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "containers/trading-engine"
GATEWAY = REPO / "containers/telegram-gateway"
sys.path.insert(0, str(ENGINE / "src"))
from danta.application import code_identity
from danta.config import load_config, load_secrets


def prepare(output, namespace, version, *, include_secrets=False,
            gateway_subnet="172.30.85.0/24", gateway_ip="172.30.85.3", sender_ids=()):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", namespace):
        raise ValueError("Invalid Docker Hub namespace")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", version):
        raise ValueError("Invalid image tag")
    subnet, address = ipaddress.ip_network(gateway_subnet), ipaddress.ip_address(gateway_ip)
    if subnet.version != 4 or address not in subnet or address in (subnet.network_address, subnet.broadcast_address, subnet.network_address + 1):
        raise ValueError("Gateway needs an IPv4 host address in the selected subnet, excluding the network gateway")
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in sender_ids):
        raise ValueError("Sender IDs must be positive Telegram user IDs")
    # Preflight private inputs before creating output; never copy a whole config directory.
    private = None
    if include_secrets:
        private = load_secrets(ENGINE / "config")
        sys.path.insert(0, str(GATEWAY))
        from telegram_gateway import parse_env_file, read_peer_secret
        key = read_peer_secret(GATEWAY / "config/codex-peer.secret").decode()
        if key != private.get("DANTA_TELEGRAM_PEER_SECRET"):
            raise ValueError("Gateway and receiver peer keys differ")
        routes = yaml.safe_load((GATEWAY / "config/routes.yaml").read_text())
        if set(routes["routes"]) != {"v1"}:
            raise ValueError("Deployment preparation requires the reviewed v1 route")
        env_source = GATEWAY / "config" / Path(routes["routes"]["v1"]["env_file"]).name
        if env_source.is_symlink() or not env_source.is_file():
            raise ValueError("A regular gateway environment file is required")
        env_body = env_source.read_bytes()
        values = parse_env_file(env_source)
        if not values.get("TELEGRAM_BOT_TOKEN") or not values.get("TELEGRAM_ALLOWED_CHAT_IDS"):
            raise ValueError("Gateway token and allowed chat IDs are required")
        chat_ids = [value.strip() for value in values["TELEGRAM_ALLOWED_CHAT_IDS"].split(",") if value.strip()]
        private = dict(private, TELEGRAM_GATEWAY_URL="http://telegram-gateway:8080", DANTA_CODEX_AUTH_HOME="/app/auth")

    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)

    def write(name, body, mode=0o644):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as stream:
            stream.write(body.encode() if isinstance(body, str) else body)

    def copy(source, name):
        write(name, source.read_bytes())

    for name in ("compose.yaml", "compose.runtime.yaml", "compose.auth.yaml"):
        copy(ENGINE / name, "trading-engine/" + name)
    # Runtime host only pulls published images; it needs no source checkout or Dockerfile.
    base = yaml.safe_load((output / "trading-engine/compose.yaml").read_text())
    base["services"]["trading-engine"].pop("build")
    (output / "trading-engine/compose.yaml").write_text(yaml.safe_dump(base, sort_keys=False))
    copy(GATEWAY / "compose.yaml", "telegram-gateway/compose.yaml")
    if private:
        routes["routes"]["v1"].update(url="http://trading-engine:8080/telegram", env_file="/app/config/telegram-v1.env")
        write("telegram-gateway/config/routes.yaml", yaml.safe_dump(routes, sort_keys=False, allow_unicode=True), 0o600)
    else:
        copy(GATEWAY / "config/routes.example.yaml", "telegram-gateway/config/routes.yaml")
    copy(GATEWAY / "config/telegram.env.example", "telegram-gateway/config/telegram-v1.env.example")
    copy(GATEWAY / "config/codex-peer.secret.example", "telegram-gateway/config/codex-peer.secret.example")
    copy(ENGINE / "config/secrets.yaml.example", "trading-engine/config/secrets.yaml.example")
    for name in ("app", "strategy", "schedules"):
        copy(ENGINE / "config" / (name + ".yaml"), "trading-engine/config/" + name + ".yaml")

    app = load_config(ENGINE / "config").app
    app["app"].update(mode="shadow", account_alias="kis-primary", state_dir="/app/var/shadow/kis-primary", listen_host="0.0.0.0")
    app["broker"].update(environment="real", capability_manifest="/app/approvals/runtime-manifest.json")
    app["market"]["calendar_manifest"] = "/app/approvals/runtime-manifest.json"
    app["telegram"].update(enabled=True, ingress_enabled=True, route="v1", allowed_sender_ids=list(sender_ids),
        allowed_chat_ids=chat_ids if private else [], trusted_peer_profile="/app/approvals/telegram-peer.json")
    write("trading-engine/config/app.shadow.yaml.example", yaml.safe_dump(app, sort_keys=False, allow_unicode=True), 0o600)

    for kind in ("engine", "gateway"):
        body = (ENGINE / "deployment" / (kind + ".env.example")).read_text()
        body = body.replace("dokysp/", namespace + "/").replace("SET_RELEASE_TAG", version)
        body = body.replace("172.30.85.0/24", str(subnet)).replace("172.30.85.3", str(address))
        write(("trading-engine" if kind == "engine" else "telegram-gateway") + "/.env", body, 0o600)
    for path in sorted((ENGINE / "deployment/examples").glob("*.json.example")):
        value = json.loads(path.read_text())
        if "account_alias" in value:
            value.update(account_alias="kis-primary", environment="real")
        if path.name == "telegram-peer.json.example":
            value.update(identity="telegram-gateway-v1", allowed_source_ips=[str(address)])
        if path.name == "runtime.json.example":
            value.update(code_id=code_identity(), model_id=app["model"]["model_id"],
                prompt_hash=hashlib.sha256((ENGINE / "prompts/portfolio_decision.md").read_bytes()).hexdigest())
        write("trading-engine/approvals/" + path.name, json.dumps(value, ensure_ascii=False, indent=2) + "\n", 0o600)
    if private:
        write("trading-engine/config/secrets.yaml", yaml.safe_dump(private, sort_keys=False, default_style='"'), 0o600)
        write("telegram-gateway/config/telegram-v1.env", env_body, 0o600)
        write("telegram-gateway/config/codex-peer.secret", key + "\n", 0o600)
    for name in ("trading-engine/var", "trading-engine/locks", "telegram-gateway/memory"):
        (output / name).mkdir(mode=0o700)
    guide = (ENGINE / "deployment/README.md").read_text()
    write("README.md", guide.replace("172.30.85.0/24", str(subnet)).replace("172.30.85.3", str(address)))
    return {"output": str(output), "status": "PREPARED_NOT_AUTHORIZED", "includes_secrets": include_secrets,
        "default_mode": "offline", "shadow_settings": "example_only", "sender_allowlist_set": bool(sender_ids),
        "images_pushed": False, "remote_host_modified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--include-secrets", action="store_true")
    parser.add_argument("--gateway-subnet", default="172.30.85.0/24")
    parser.add_argument("--gateway-ip", default="172.30.85.3")
    parser.add_argument("--sender-id", action="append", default=[])
    args = parser.parse_args()
    try:
        result = prepare(args.output, args.namespace, args.version, include_secrets=args.include_secrets,
            gateway_subnet=args.gateway_subnet, gateway_ip=args.gateway_ip, sender_ids=args.sender_id)
    except Exception as error:
        # Parsers may embed private input lines in exceptions.
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))
