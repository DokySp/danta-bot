#!/usr/bin/env python3
"""Prepare a new, private directory for transfer to a separate Docker host."""
import argparse
import hashlib
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


def prepare(output, namespace, version, *, include_secrets=False, sender_ids=()):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", namespace):
        raise ValueError("Invalid Docker Hub namespace")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", version):
        raise ValueError("Invalid image tag")
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in sender_ids):
        raise ValueError("Sender IDs must be positive Telegram user IDs")
    # Preflight private inputs before creating output; never copy a whole config directory.
    private = None
    if include_secrets:
        private = load_secrets(ENGINE / "config")
        sys.path.insert(0, str(GATEWAY))
        from telegram_gateway import parse_env_file
        private.pop("DANTA_TELEGRAM_PEER_SECRET", None)
        routes = yaml.safe_load((GATEWAY / "config/routes.yaml").read_text())
        if set(routes["routes"]) != {"trading-engine"}:
            raise ValueError("Deployment preparation requires one trading-engine route")
        if routes["routes"]["trading-engine"]["env_file"] != "/app/config/telegram.env":
            raise ValueError("The trading-engine route must use /app/config/telegram.env")
        env_source = GATEWAY / "config/telegram.env"
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

    for source, name in ((ENGINE, "trading-engine"), (GATEWAY, "telegram-gateway")):
        compose = yaml.safe_load((source / "compose.yaml").read_text())
        for service in compose["services"].values():
            service["image"] = f"{namespace}/{name}:{version}"
        write(name + "/compose.yaml", yaml.safe_dump(compose, sort_keys=False))
    if private:
        routes["routes"]["trading-engine"].update(url="http://trading-engine:8080/telegram")
        write("telegram-gateway/config/routes.yaml", yaml.safe_dump(routes, sort_keys=False, allow_unicode=True), 0o600)
    else:
        copy(GATEWAY / "config/routes.example.yaml", "telegram-gateway/config/routes.yaml")
    copy(GATEWAY / "config/telegram.env.example", "telegram-gateway/config/telegram.env.example")
    copy(ENGINE / "config/secrets.yaml.example", "trading-engine/config/secrets.yaml.example")
    for name in ("app", "strategy", "schedules"):
        copy(ENGINE / "config" / (name + ".yaml"), "trading-engine/config/" + name + ".yaml")

    app = load_config(ENGINE / "config").app
    app["app"].update(mode="shadow", account_alias="kis-primary", state_dir="/app/var/shadow/kis-primary", listen_host="0.0.0.0")
    app["broker"].update(environment="real", capability_manifest="/app/config/runtime-manifest.json")
    app["market"]["calendar_manifest"] = "/app/config/runtime-manifest.json"
    app["telegram"].update(enabled=True, ingress_enabled=True, route="trading-engine", allowed_sender_ids=list(sender_ids),
        allowed_chat_ids=chat_ids if private else [])
    write("trading-engine/config/app.shadow.yaml.example", yaml.safe_dump(app, sort_keys=False, allow_unicode=True), 0o600)

    for path in sorted((ENGINE / "config").glob("runtime*.json.example")):
        value = json.loads(path.read_text())
        if "account_alias" in value:
            value.update(account_alias="kis-primary", environment="real")
        if path.name == "runtime.json.example":
            value.update(code_id=code_identity(), model_id=app["model"]["model_id"],
                prompt_hash=hashlib.sha256((ENGINE / "prompts/portfolio_decision.md").read_bytes()).hexdigest())
        write("trading-engine/config/" + path.name, json.dumps(value, ensure_ascii=False, indent=2) + "\n", 0o600)
    runtime_files = []
    for name in ("runtime.json", "runtime-manifest.json"):
        path = ENGINE / "config" / name
        if path.is_symlink():
            raise ValueError("Runtime configuration must be a regular file")
        if path.is_file():
            # Preserve hashes and existing authority exactly; never fill verified fields.
            write("trading-engine/config/" + name, path.read_bytes(), 0o600)
            runtime_files.append(name)
    if private:
        write("trading-engine/config/secrets.yaml", yaml.safe_dump(private, sort_keys=False, default_style='"'), 0o600)
        write("telegram-gateway/config/telegram.env", env_body, 0o600)
    guide = (ENGINE / "deployment/README.md").read_text()
    write("README.md", guide)
    return {"output": str(output), "status": "PREPARED_NOT_AUTHORIZED", "includes_secrets": include_secrets,
        "default_mode": load_config(ENGINE / "config").mode, "runtime_files": runtime_files,
        "shadow_settings": "example_only", "sender_allowlist_set": bool(sender_ids),
        "images_pushed": False, "remote_host_modified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--namespace", default="dokysp")
    parser.add_argument("--version", default="latest")
    parser.add_argument("--include-secrets", action="store_true")
    parser.add_argument("--sender-id", action="append", default=[])
    args = parser.parse_args()
    try:
        result = prepare(args.output, args.namespace, args.version, include_secrets=args.include_secrets,
            sender_ids=args.sender_id)
    except Exception as error:
        # Parsers may embed private input lines in exceptions.
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))
