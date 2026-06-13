import json
import logging
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .config import Config
from .runner import CodexRunner
from .scheduler import Scheduler
from .state import StateStore
from .telegram_gateway import TelegramGateway
from .telegram_worker import TelegramTask, TelegramWorker


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = StateStore(config)
        self.runner = CodexRunner(config)
        self.gateway = TelegramGateway(config)
        self.telegram_worker = TelegramWorker(config, self.state, self.runner, self.gateway)
        self.scheduler = Scheduler(config, self.runner, self.gateway)

    def start(self) -> ThreadingHTTPServer:
        self.telegram_worker.start()
        self.scheduler.start()
        return self._serve_http()

    def stop(self) -> None:
        self.scheduler.stop()

    def _serve_http(self) -> ThreadingHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "codex-exec"

            def do_GET(self) -> None:
                if self.path != "/healthz":
                    self._write_json(404, {"ok": False, "error": "not found"})
                    return
                self._write_json(200, {"ok": True})

            def do_POST(self) -> None:
                if self.path != "/telegram":
                    self._write_json(404, {"ok": False, "error": "not found"})
                    return
                try:
                    payload = self._read_json()
                    text = str(payload.get("text", "")).strip()
                    if not text:
                        self._write_json(400, {"ok": False, "error": "text is required"})
                        return

                    app.telegram_worker.submit(
                        TelegramTask(
                            chat_id=str(payload.get("chat_id")) if payload.get("chat_id") else None,
                            text=text,
                            route=str(payload.get("route")) if payload.get("route") else None,
                            message_id=payload.get("message_id"),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - expose endpoint failures as JSON
                    logging.exception("failed to accept telegram payload")
                    self._write_json(500, {"ok": False, "error": str(exc)})
                    return

                self._write_json(202, {"ok": True, "queued": True})

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

        server = ThreadingHTTPServer((self.config.host, self.config.port), Handler)
        thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
        thread.start()
        return server


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_env()
    app = App(config)

    stop_event = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    server = app.start()
    logging.info(
        "codex-exec listening on %s:%s",
        config.host,
        config.port,
    )
    try:
        while not stop_event.wait(1):
            pass
    finally:
        app.stop()
        server.shutdown()
        server.server_close()
