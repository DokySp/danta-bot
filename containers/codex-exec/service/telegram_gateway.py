import base64
import json
import logging
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Config


class TelegramGateway:
    def __init__(self, config: Config) -> None:
        self.config = config

    def send_message(
        self,
        text: str,
        chat_id: str | None = None,
        route: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "text": text or "<i>No output</i>",
            "parse_mode": "HTML",
            "escape": False,
        }
        if chat_id:
            payload["chat_id"] = chat_id
        outbound_route = route or self.config.telegram_route
        if outbound_route:
            payload["route"] = outbound_route

        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self.config.telegram_gateway_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                response.read()
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"telegram-gateway failed: HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(f"telegram-gateway failed: {exc}") from exc

    def send_photo(
        self,
        path: Path,
        caption: str | None = None,
        chat_id: str | None = None,
        route: str | None = None,
    ) -> None:
        self._send_binary_file("/sendPhoto", path, caption, chat_id, route)

    def send_document(
        self,
        path: Path,
        caption: str | None = None,
        chat_id: str | None = None,
        route: str | None = None,
    ) -> None:
        self._send_binary_file("/sendDocument", path, caption, chat_id, route)

    def _send_binary_file(
        self,
        endpoint: str,
        path: Path,
        caption: str | None,
        chat_id: str | None,
        route: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "filename": path.name,
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            "parse_mode": "HTML",
        }
        if caption:
            payload["caption"] = caption
        if chat_id:
            payload["chat_id"] = chat_id
        outbound_route = route or self.config.telegram_route
        if outbound_route:
            payload["route"] = outbound_route
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self._gateway_url(endpoint),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                response.read()
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"telegram-gateway file send failed: HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(f"telegram-gateway file send failed: {exc}") from exc

    def send_chat_action(
        self,
        chat_id: str | None,
        route: str | None = None,
        action: str = "typing",
    ) -> None:
        outbound_route = route or self.config.telegram_route
        if not outbound_route:
            logging.debug("telegram chat action skipped because route is missing")
            return

        payload: dict[str, Any] = {
            "route": outbound_route,
            "action": action,
        }
        if chat_id:
            payload["chat_id"] = chat_id
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            self._gateway_url("/sendChatAction"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                response.read()
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"telegram-gateway chat action failed: HTTP {exc.code}: {raw}") from exc
        except URLError as exc:
            raise RuntimeError(f"telegram-gateway chat action failed: {exc}") from exc

    def _gateway_url(self, endpoint: str) -> str:
        base = self.config.telegram_gateway_url.rstrip("/")
        if base.endswith("/sendMessage"):
            return base.rsplit("/", 1)[0] + endpoint
        return base + endpoint


class TypingIndicator:
    def __init__(
        self,
        gateway: TelegramGateway,
        chat_id: str | None,
        route: str | None,
        interval_seconds: float,
    ) -> None:
        self.gateway = gateway
        self.chat_id = chat_id
        self.route = route
        self.interval_seconds = max(1.0, interval_seconds)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "TypingIndicator":
        if self.chat_id or self.route or self.gateway.config.telegram_route:
            self.thread = threading.Thread(target=self._loop, name="telegram-typing", daemon=True)
            self.thread.start()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.gateway.send_chat_action(self.chat_id, self.route, "typing")
            except Exception:
                logging.exception("failed to send telegram typing action")
            self.stop_event.wait(self.interval_seconds)
