#!/usr/bin/env python3
import html
import json
import logging
import os
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml


MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
BOT_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")


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


class TelegramHtmlSanitizer(HTMLParser):
    INLINE_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre"}
    LINE_BREAK_TAGS = {"br"}
    BLOCK_TAGS = {
        "p",
        "div",
        "section",
        "article",
        "header",
        "footer",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.LINE_BREAK_TAGS:
            self._append_line_break()
            return
        if tag == "li":
            self._append_line_break()
            self.parts.append("- ")
            return
        if tag in self.BLOCK_TAGS:
            self._append_line_break()
            return
        if tag in self.INLINE_TAGS:
            self.parts.append(f"<{tag}>")
            self.open_tags.append(tag)
            return
        if tag == "a":
            href = self._attr_value(attrs, "href")
            if href:
                self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
                self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.BLOCK_TAGS or tag == "li":
            self._append_line_break()
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            opened = self.open_tags.pop()
            self.parts.append(f"</{opened}>")
            if opened == tag:
                return

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.LINE_BREAK_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")
        return "".join(self.parts)

    def _append_line_break(self) -> None:
        if not self.parts or self.parts[-1].endswith("\n"):
            return
        self.parts.append("\n")

    @staticmethod
    def _attr_value(attrs: list[tuple[str, str | None]], attr_name: str) -> str | None:
        for key, value in attrs:
            if key.lower() == attr_name and value:
                return value
        return None


def sanitize_telegram_html(text: str) -> str:
    sanitizer = TelegramHtmlSanitizer()
    try:
        sanitizer.feed(text)
        sanitizer.close()
    except Exception:
        logging.exception("failed to sanitize Telegram HTML; falling back to escaped text")
        return html.escape(text, quote=False)
    return sanitizer.get_html()


def split_telegram_text(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + limit])
        start += limit
    return chunks


TELEGRAM_CONTEXT_TEXT_LIMIT = 2000
TELEGRAM_CONTEXT_CONTENT_FIELDS = (
    "animation",
    "audio",
    "checklist",
    "contact",
    "dice",
    "document",
    "game",
    "invoice",
    "live_photo",
    "location",
    "paid_media",
    "photo",
    "poll",
    "sticker",
    "story",
    "venue",
    "video",
    "video_note",
    "voice",
    "web_app_data",
)


def build_codex_input_text(text: str, message: dict[str, Any]) -> str:
    if is_gateway_command(text):
        return text

    context = telegram_reply_context(message)
    if not context:
        return text

    context_json = json.dumps(context, ensure_ascii=False, indent=2)
    return (
        "아래 Telegram context는 사용자의 현재 메시지와 함께 전달된 reply 관련 메타데이터입니다. "
        "reply_to_message, external_reply, quote 등이 있으면 사용자가 해당 대상에 답장한 것으로 보고 함께 처리하세요.\n"
        "<telegram_context>\n"
        f"{context_json}\n"
        "</telegram_context>\n\n"
        "<user_message>\n"
        f"{text}\n"
        "</user_message>"
    )


def is_gateway_command(text: str) -> bool:
    stripped = text.strip()
    return stripped == "/new" or stripped.startswith("/new ") or stripped == "/usage"


def telegram_reply_context(message: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}

    reply_to_message = message.get("reply_to_message")
    if isinstance(reply_to_message, dict):
        context["reply_to_message"] = summarize_telegram_message(reply_to_message)

    external_reply = message.get("external_reply")
    if isinstance(external_reply, dict):
        context["external_reply"] = summarize_external_reply(external_reply)

    quote = message.get("quote")
    if isinstance(quote, dict):
        context["quote"] = summarize_text_quote(quote)

    reply_to_story = message.get("reply_to_story")
    if isinstance(reply_to_story, dict):
        context["reply_to_story"] = compact_telegram_value(reply_to_story)

    for key in ("reply_to_checklist_task_id", "reply_to_poll_option_id"):
        if message.get(key) is not None:
            context[key] = message.get(key)

    return context


def summarize_telegram_message(message: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("message_id", "date"):
        if message.get(key) is not None:
            summary[key] = message.get(key)

    sender = summarize_telegram_user(message.get("from"))
    if sender:
        summary["from"] = sender
    sender_chat = summarize_telegram_chat(message.get("sender_chat"))
    if sender_chat:
        summary["sender_chat"] = sender_chat
    chat = summarize_telegram_chat(message.get("chat"))
    if chat:
        summary["chat"] = chat

    if isinstance(message.get("text"), str):
        summary["text"] = trim_telegram_text(message["text"])
    if isinstance(message.get("caption"), str):
        summary["caption"] = trim_telegram_text(message["caption"])

    content_types = [field for field in TELEGRAM_CONTEXT_CONTENT_FIELDS if field in message]
    if content_types:
        summary["content_types"] = content_types

    poll = message.get("poll")
    if isinstance(poll, dict):
        summary["poll"] = summarize_poll(poll)
    checklist = message.get("checklist")
    if isinstance(checklist, dict):
        summary["checklist"] = summarize_checklist(checklist)

    return summary


def summarize_external_reply(external_reply: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("message_id", "date"):
        if external_reply.get(key) is not None:
            summary[key] = external_reply.get(key)

    origin = external_reply.get("origin")
    if isinstance(origin, dict):
        summary["origin"] = compact_telegram_value(origin)
    chat = summarize_telegram_chat(external_reply.get("chat"))
    if chat:
        summary["chat"] = chat

    if isinstance(external_reply.get("text"), str):
        summary["text"] = trim_telegram_text(external_reply["text"])
    if isinstance(external_reply.get("caption"), str):
        summary["caption"] = trim_telegram_text(external_reply["caption"])

    content_types = [field for field in TELEGRAM_CONTEXT_CONTENT_FIELDS if field in external_reply]
    if content_types:
        summary["content_types"] = content_types
    return summary


def summarize_text_quote(quote: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if isinstance(quote.get("text"), str):
        summary["text"] = trim_telegram_text(quote["text"])
    for key in ("position", "is_manual"):
        if quote.get(key) is not None:
            summary[key] = quote.get(key)
    return summary


def summarize_poll(poll: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if isinstance(poll.get("question"), str):
        summary["question"] = trim_telegram_text(poll["question"])
    options = poll.get("options")
    if isinstance(options, list):
        summary["options"] = [
            {
                key: trim_telegram_text(value) if isinstance(value, str) else value
                for key, value in option.items()
                if key in {"persistent_id", "text", "voter_count"} and value is not None
            }
            for option in options
            if isinstance(option, dict)
        ]
    return summary


def summarize_checklist(checklist: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if isinstance(checklist.get("title"), str):
        summary["title"] = trim_telegram_text(checklist["title"])
    tasks = checklist.get("tasks")
    if isinstance(tasks, list):
        summary["tasks"] = [
            compact_telegram_value(task)
            for task in tasks
            if isinstance(task, dict)
        ]
    return summary


def summarize_telegram_user(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("id", "is_bot", "username", "first_name", "last_name")
        if value.get(key) is not None
    }


def summarize_telegram_chat(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("id", "type", "title", "username", "first_name", "last_name")
        if value.get(key) is not None
    }


def compact_telegram_value(value: Any) -> Any:
    if isinstance(value, str):
        return trim_telegram_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [compact_telegram_value(item) for item in value[:5]]
    if not isinstance(value, dict):
        return str(value)

    scalar_keys = {
        "id",
        "type",
        "message_id",
        "date",
        "title",
        "username",
        "first_name",
        "last_name",
        "text",
        "question",
        "name",
        "file_name",
        "mime_type",
        "duration",
        "width",
        "height",
        "is_manual",
        "position",
    }
    nested_keys = {
        "from",
        "sender_user",
        "sender_chat",
        "chat",
        "author_chat",
        "user",
        "added_by_user",
        "added_by_chat",
    }
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key in scalar_keys:
            compact[key] = compact_telegram_value(item)
        elif key in nested_keys and isinstance(item, dict):
            compact[key] = compact_telegram_value(item)
    return compact


def trim_telegram_text(text: str) -> str:
    if len(text) <= TELEGRAM_CONTEXT_TEXT_LIMIT:
        return text
    return text[:TELEGRAM_CONTEXT_TEXT_LIMIT].rstrip() + "... [truncated]"


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
class BotCommand:
    command: str
    description: str
    text: str | None = None

    def telegram_payload(self) -> dict[str, str]:
        return {"command": self.command, "description": self.description}


def parse_bot_commands(raw_commands: Any, *, source: str) -> tuple[BotCommand, ...]:
    if not isinstance(raw_commands, list):
        raise ValueError(f"{source} must be a list")

    commands: list[BotCommand] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_commands):
        item_source = f"{source}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_source} must be an object")

        command = str(item.get("command", "")).strip()
        description = str(item.get("description", "")).strip()
        text_value = item.get("text")
        text = str(text_value).strip() if text_value is not None else None
        if text == "":
            text = None

        if not command:
            raise ValueError(f"{item_source}.command is required")
        if not BOT_COMMAND_RE.fullmatch(command):
            raise ValueError(
                f"{item_source}.command must be 1-32 chars of lowercase English letters, digits, or underscores"
            )
        if command in seen:
            raise ValueError(f"{item_source}.command is duplicated: {command}")
        if not description:
            raise ValueError(f"{item_source}.description is required")
        if len(description) > 256:
            raise ValueError(f"{item_source}.description must be 256 chars or fewer")

        seen.add(command)
        commands.append(BotCommand(command=command, description=description, text=text))

    return tuple(commands)


def route_bot_commands(raw_config: dict[str, Any], route_id: str) -> tuple[BotCommand, ...]:
    if "bot_commands" in raw_config:
        return parse_bot_commands(raw_config.get("bot_commands"), source=f"route {route_id} bot_commands")
    return ()


def apply_bot_command_alias(route: "RouteConfig", text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return text

    for command in route.bot_commands:
        if not command.text:
            continue

        source = f"/{command.command}"
        if stripped == source:
            return command.text
        if stripped.startswith(f"{source} "):
            return f"{command.text}{stripped[len(source):]}"

    return text


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
    bot_commands: tuple[BotCommand, ...]


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
            bot_commands=route_bot_commands(raw_config, route_id),
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

    def set_my_commands(self) -> None:
        payload = {
            "commands": json.dumps(
                [command.telegram_payload() for command in self.route.bot_commands],
                ensure_ascii=False,
            )
        }
        self.post_form("setMyCommands", payload)

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        escape: bool = True,
    ) -> None:
        mode = parse_mode if parse_mode is not None else self.route.parse_mode
        outbound_text = escape_markdown_v2(text) if escape and mode == "MarkdownV2" else text
        if mode and mode.lower() == "html":
            outbound_text = sanitize_telegram_html(outbound_text)

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

    def register_bot_commands(self) -> None:
        routing = self.routing_store.get()
        for route in routing.routes.values():
            if not route.bot_commands:
                logging.info("skipping Telegram bot command registration route=%s", route.route_id)
                continue
            try:
                TelegramClient(route).set_my_commands()
            except Exception:
                logging.exception("failed to register Telegram bot commands route=%s", route.route_id)
                continue
            logging.info(
                "registered Telegram bot commands route=%s commands=%s",
                route.route_id,
                ",".join(command.command for command in route.bot_commands),
            )

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

        routed_text = apply_bot_command_alias(route, text)
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
        if routed_text != text:
            logging.info(
                "applied bot command alias route=%s text=%r routed_text=%r",
                route.route_id,
                text,
                routed_text,
            )

        client = TelegramClient(route)
        if route.echo_mode:
            logging.info("echoing telegram message route=%s chat_id=%s message_id=%s", route.route_id, chat_id, message_id)
            client.send_message(chat_id, text)
            return

        resolved = self.router.resolve(route.route_id, routed_text)
        codex_text = build_codex_input_text(resolved.text, message)
        payload = {
            "source": "telegram",
            "gateway_version": self.config.version,
            "route": route.route_id,
            "update_id": update.get("update_id"),
            "message_id": message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "text": codex_text,
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
    app.register_bot_commands()
    try:
        app.poll_forever()
    finally:
        logging.info("shutting down")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
