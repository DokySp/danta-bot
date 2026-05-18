#!/usr/bin/env python3
import json
import logging
import os
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml


MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_bool_value(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def csv_value(raw: str | None) -> set[str]:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def escape_markdown_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MARKDOWN_V2_SPECIALS)}])", r"\\\1", text)


def split_telegram_text(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + limit])
        start += limit
    return chunks


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").strip()
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class Config:
    version: str
    http_timeout: int
    gateway_host: str
    gateway_port: int
    gateway_routes_file: Path

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            version=os.getenv("APP_VERSION", "1.0.0"),
            http_timeout=env_int("HTTP_TIMEOUT", 10),
            gateway_host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            gateway_port=env_int("GATEWAY_PORT", 8080),
            gateway_routes_file=Path(os.getenv("GATEWAY_ROUTES_FILE", "/app/config/routes.yaml")),
        )


@dataclass(frozen=True)
class RouteConfig:
    route_id: str
    env_file: Path
    url: str
    telegram_bot_token: str
    allowed_chat_ids: set[str]
    default_chat_id: str | None
    parse_mode: str | None
    poll_timeout: int
    poll_interval: float
    http_timeout: int
    ack_text: str | None
    echo_mode: bool


@dataclass(frozen=True)
class RoutingTable:
    routes: dict[str, RouteConfig]

    def route_ids(self) -> list[str]:
        return sorted(self.routes)


class RoutingConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.watched_mtimes: dict[Path, int] = {}
        self.routing: RoutingTable | None = None
        self.reload(initial=True)

    def get(self) -> RoutingTable:
        self.reload(initial=False)
        if self.routing is None:
            raise RuntimeError("routing config is not loaded")
        return self.routing

    def reload(self, initial: bool) -> None:
        if not initial and self.routing is not None:
            try:
                current_mtimes = self._current_watched_mtimes()
            except OSError:
                logging.exception("routing config file is unavailable; keeping last-known-good config")
                return
            if current_mtimes == self.watched_mtimes:
                return

        try:
            loaded = load_routing_table(self.path)
            watched_mtimes = self._mtimes_for_loaded_table(loaded)
        except Exception:
            if initial or self.routing is None:
                raise
            logging.exception("routing config reload failed; keeping last-known-good config")
            return

        with self.lock:
            self.routing = loaded
            self.watched_mtimes = watched_mtimes
        logging.info(
            "loaded routing config path=%s routes=%s",
            self.path,
            ",".join(loaded.route_ids()),
        )

    def _current_watched_mtimes(self) -> dict[Path, int]:
        return {path: path.stat().st_mtime_ns for path in self.watched_mtimes}

    def _mtimes_for_loaded_table(self, routing: RoutingTable) -> dict[Path, int]:
        paths = {self.path, *(route.env_file for route in routing.routes.values())}
        return {path: path.stat().st_mtime_ns for path in paths}


def load_routing_table(path: Path) -> RoutingTable:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("routes file must contain a YAML object")

    raw_routes = data.get("routes", {})
    if not isinstance(raw_routes, dict):
        raise ValueError("routes file routes must be a YAML object")
    if not raw_routes:
        raise ValueError("routes file must define at least one route")

    routes: dict[str, RouteConfig] = {}
    for raw_route_id, raw_config in raw_routes.items():
        route_id = str(raw_route_id).strip()
        if not route_id:
            raise ValueError("route id must not be empty")
        if not isinstance(raw_config, dict):
            raise ValueError(f"route {route_id} must be a YAML object")
        if raw_config.get("enabled", True) is False:
            continue

        env_file_text = str(raw_config.get("env_file", "")).strip()
        if not env_file_text:
            raise ValueError(f"route {route_id} env_file is required")
        env_file = Path(env_file_text)
        if not env_file.is_absolute():
            env_file = path.parent / env_file

        url = str(raw_config.get("url", "")).strip()
        if not url:
            raise ValueError(f"route {route_id} url is required")

        values = parse_env_file(env_file)
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError(f"TELEGRAM_BOT_TOKEN is required in {env_file}")

        routes[route_id] = RouteConfig(
            route_id=route_id,
            env_file=env_file,
            url=url,
            telegram_bot_token=token,
            allowed_chat_ids=csv_value(values.get("TELEGRAM_ALLOWED_CHAT_IDS")),
            default_chat_id=values.get("TELEGRAM_DEFAULT_CHAT_ID", "").strip() or None,
            parse_mode=values.get("TELEGRAM_PARSE_MODE", "MarkdownV2").strip() or None,
            poll_timeout=int(values.get("TELEGRAM_POLL_TIMEOUT", "25") or "25"),
            poll_interval=float(values.get("TELEGRAM_POLL_INTERVAL", "1") or "1"),
            http_timeout=int(values.get("HTTP_TIMEOUT", "10") or "10"),
            ack_text=values.get("TELEGRAM_ACK_TEXT", "").strip() or None,
            echo_mode=env_bool_value(values.get("TELEGRAM_ECHO_MODE"), False),
        )

    if not routes:
        raise ValueError("routes file must define at least one enabled route")
    return RoutingTable(routes=routes)


class TelegramClient:
    def __init__(self, route: RouteConfig) -> None:
        self.route = route
        self.base_url = f"https://api.telegram.org/bot{route.telegram_bot_token}"

    def post_form(self, method: str, payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
        data = urlencode(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout or self.route.http_timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Telegram {method} failed: HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Telegram {method} failed: {exc}") from exc

        parsed = json.loads(body)
        if not parsed.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body}")
        return parsed

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self.route.poll_timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            payload["offset"] = offset

        timeout = self.route.poll_timeout + self.route.http_timeout
        response = self.post_form("getUpdates", payload, timeout=timeout)
        result = response.get("result", [])
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        escape: bool = True,
    ) -> None:
        mode = parse_mode if parse_mode is not None else self.route.parse_mode
        outbound_text = escape_markdown_v2(text) if escape and mode == "MarkdownV2" else text

        for chunk in split_telegram_text(outbound_text):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if mode:
                payload["parse_mode"] = mode
            self.post_form("sendMessage", payload)

    def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        self.post_form("sendChatAction", {"chat_id": chat_id, "action": action})


class CodexExecClient:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout

    def post_message(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"codex-exec route failed: HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(f"codex-exec route failed: {exc}") from exc

        if not raw.strip():
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"reply_text": raw}
        return parsed if isinstance(parsed, dict) else {"reply_text": raw}


@dataclass(frozen=True)
class ResolvedRoute:
    route_id: str
    url: str
    text: str


class Router:
    def __init__(self, routing_store: RoutingConfigStore) -> None:
        self.routing_store = routing_store

    def resolve(self, route_id: str, text: str) -> ResolvedRoute:
        routing = self.routing_store.get()
        route = routing.routes.get(route_id)
        if not route:
            raise ValueError(f"unknown route: {route_id}")
        return ResolvedRoute(route_id=route.route_id, url=route.url, text=text)


class GatewayApp:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.routing_store = RoutingConfigStore(config.gateway_routes_file)
        self.codex = CodexExecClient(config.http_timeout)
        self.router = Router(self.routing_store)
        self.stop_event = threading.Event()
        self.offsets: dict[str, int | None] = {}
        self.offsets_lock = threading.Lock()

    def serve_http(self) -> ThreadingHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"telegram-gateway/{app.config.version}"

            def do_GET(self) -> None:
                if self.path != "/healthz":
                    self._write_json(404, {"ok": False, "error": "not found"})
                    return
                routing = app.routing_store.get()
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "version": app.config.version,
                        "routes": routing.route_ids(),
                    },
                )

            def do_POST(self) -> None:
                if self.path not in {"/sendMessage", "/notify", "/sendChatAction"}:
                    self._write_json(404, {"ok": False, "error": "not found"})
                    return

                try:
                    payload = self._read_json()
                    if self.path == "/sendChatAction":
                        route = app.resolve_send_route(payload)
                        chat_id = str(payload.get("chat_id") or route.default_chat_id or "")
                        if not chat_id:
                            self._write_json(400, {"ok": False, "error": "chat_id is required"})
                            return
                        action = str(payload.get("action") or "typing").strip()
                        if not action:
                            self._write_json(400, {"ok": False, "error": "action is required"})
                            return
                        TelegramClient(route).send_chat_action(chat_id, action)
                        self._write_json(200, {"ok": True})
                        return

                    text = str(payload.get("text", ""))
                    if not text:
                        self._write_json(400, {"ok": False, "error": "text is required"})
                        return

                    route = app.resolve_send_route(payload)
                    chat_id = str(payload.get("chat_id") or route.default_chat_id or "")
                    if not chat_id:
                        self._write_json(400, {"ok": False, "error": "chat_id is required"})
                        return

                    parse_mode = payload.get("parse_mode", route.parse_mode)
                    if parse_mode is not None:
                        parse_mode = str(parse_mode)
                    escape = bool(payload.get("escape", True))
                    TelegramClient(route).send_message(chat_id, text, parse_mode=parse_mode, escape=escape)
                except Exception as exc:  # noqa: BLE001 - convert all endpoint errors to JSON
                    logging.exception("send endpoint failed")
                    self._write_json(500, {"ok": False, "error": str(exc)})
                    return

                self._write_json(200, {"ok": True})

            def log_message(self, fmt: str, *args: Any) -> None:
                logging.info("http %s - %s", self.address_string(), fmt % args)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                parsed = json.loads(raw or "{}")
                if not isinstance(parsed, dict):
                    raise ValueError("request body must be a JSON object")
                return parsed

            def _write_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer((self.config.gateway_host, self.config.gateway_port), Handler)
        thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
        thread.start()
        return server

    def resolve_send_route(self, payload: dict[str, Any]) -> RouteConfig:
        routing = self.routing_store.get()
        route_id = str(payload.get("route", "")).strip()

        if route_id:
            route = routing.routes.get(route_id)
            if not route:
                raise ValueError(f"unknown route: {route_id}")
            return route

        raise ValueError("route is required")

    def poll_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                routing = self.routing_store.get()
                if not routing.routes:
                    logging.warning("no routes configured")
                    self.stop_event.wait(1)
                    continue

                with ThreadPoolExecutor(max_workers=len(routing.routes)) as executor:
                    futures = [
                        executor.submit(self.poll_route_once, route)
                        for route in routing.routes.values()
                    ]
                    for future in as_completed(futures):
                        future.result()
            except Exception:
                logging.exception("polling failed")
                time.sleep(1)
                continue

            poll_interval = min((route.poll_interval for route in routing.routes.values()), default=1.0)
            self.stop_event.wait(poll_interval)

    def poll_route_once(self, route: RouteConfig) -> None:
        if not route.allowed_chat_ids:
            logging.warning("route=%s TELEGRAM_ALLOWED_CHAT_IDS is empty; inbound messages ignored", route.route_id)

        client = TelegramClient(route)
        with self.offsets_lock:
            offset = self.offsets.get(route.route_id)
        updates = client.get_updates(offset)

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                with self.offsets_lock:
                    self.offsets[route.route_id] = update_id + 1
            self.handle_update(route, update)

    def handle_update(self, route: RouteConfig, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id = str(chat.get("id", ""))
        text = message.get("text")

        if not chat_id or not isinstance(text, str):
            return
        if chat_id not in route.allowed_chat_ids:
            logging.warning("ignored unauthorized chat_id=%s route=%s", chat_id, route.route_id)
            return

        user_id = str(sender.get("id", ""))
        username = sender.get("username")
        message_id = message.get("message_id")
        logging.info(
            "received telegram message route=%s chat_id=%s user_id=%s username=%s message_id=%s text=%r",
            route.route_id,
            chat_id,
            user_id,
            username,
            message_id,
            text,
        )

        client = TelegramClient(route)
        if route.echo_mode:
            logging.info("echoing telegram message route=%s chat_id=%s message_id=%s", route.route_id, chat_id, message_id)
            client.send_message(chat_id, text)
            return

        resolved = self.router.resolve(route.route_id, text)
        payload = {
            "source": "telegram",
            "gateway_version": self.config.version,
            "route": route.route_id,
            "update_id": update.get("update_id"),
            "message_id": message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "text": resolved.text,
            "raw_message": message,
        }

        logging.info(
            "routing route=%s chat_id=%s url=%s",
            route.route_id,
            chat_id,
            resolved.url,
        )
        response = self.codex.post_message(resolved.url, payload)

        reply_text = None
        if response:
            reply_text = response.get("reply_text") or response.get("text")
        if reply_text:
            client.send_message(chat_id, str(reply_text))
        elif route.ack_text:
            client.send_message(chat_id, route.ack_text)


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_env()
    app = GatewayApp(config)

    def stop(_signum: int, _frame: Any) -> None:
        app.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    server = app.serve_http()
    logging.info("telegram-gateway %s listening on %s:%s", config.version, config.gateway_host, config.gateway_port)
    try:
        app.poll_forever()
    finally:
        logging.info("shutting down")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
