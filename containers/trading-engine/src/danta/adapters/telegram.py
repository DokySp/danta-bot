"""Single-engine gateway wire contract with sender/chat checks and durable receipts."""

import base64
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from . import AdapterError, http_transport, require_http_ok
from ..safety import CredentialError, reject_credentials

COMMANDS = frozenset("status report usage version review stop pause session new schedule_on schedule_off reasoning_effort add_portfolio_ticker remove_portfolio_ticker add_portfolio_except_ticker remove_portfolio_except_ticker show_touch_point resume".split())
READ_COMMANDS = frozenset("status report usage version session show_touch_point".split())
ROUTE = "trading-engine"
GATEWAY = "telegram-gateway"


@dataclass(frozen=True)
class TelegramRequest:
    request_id: str
    route: str
    update_id: int
    chat_id: str
    user_id: str
    text: str
    command: str


class TelegramAdapter:
    def __init__(self, connection: sqlite3.Connection, *, enabled=False, allowed_senders=(), allowed_chats=(),
                 gateway_url="http://telegram-gateway:8080", transport=None, authorize=None):
        self.db, self.enabled = connection, enabled
        self.senders, self.chats = set(map(str, allowed_senders)), set(map(str, allowed_chats))
        self.gateway_url, self.authorize = gateway_url.rstrip("/"), authorize
        self.transport = transport or http_transport(allowed_origins={self.gateway_url})
        self.db.execute("CREATE TABLE IF NOT EXISTS telegram_requests (peer TEXT, route TEXT, update_id INTEGER, body_hash TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', PRIMARY KEY(peer,route,update_id))")
        self.db.commit()

    def receive(self, body):
        if not self.enabled:
            raise AdapterError("TELEGRAM_INGRESS_DISABLED")
        if not isinstance(body, dict) or body.get("source") != "telegram" or type(body.get("update_id")) is not int or body["update_id"] < 0:
            raise AdapterError("INVALID_TELEGRAM_REQUEST")
        values = {}
        for key in ("route", "chat_id", "user_id"):
            value = body.get(key)
            if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value):
                raise AdapterError("INVALID_TELEGRAM_REQUEST")
            values[key] = str(value)
        if values["route"] != ROUTE:
            raise AdapterError("UNKNOWN_TELEGRAM_ROUTE")
        if values["chat_id"] not in self.chats or values["user_id"] not in self.senders:
            raise AdapterError("SENDER_NOT_ALLOWED")
        text = body.get("text")
        if not isinstance(text, str) or not text or len(text) > 16384:
            raise AdapterError("INVALID_TELEGRAM_TEXT")
        command = text.split()[0][1:].split("@", 1)[0] if text.startswith("/") else "chat"
        if command != "chat" and command not in COMMANDS:
            raise AdapterError("UNKNOWN_COMMAND")
        if command not in READ_COMMANDS and command != "chat":
            if self.authorize is None:
                raise AdapterError("CONTROL_AUTHORIZATION_REQUIRED")
            self.authorize(command, values["user_id"], values["chat_id"])
        payload = json.dumps({**body, **values}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(payload.encode()) > 65536:
            raise AdapterError("TELEGRAM_PAYLOAD_TOO_LARGE")
        body_hash = hashlib.sha256(payload.encode()).hexdigest()
        request_id = str(uuid.uuid4())
        with self.db:
            # Retain the existing peer column so stored receipts need no schema migration.
            existing = self.db.execute("SELECT body_hash,request_id FROM telegram_requests WHERE peer=? AND route=? AND update_id=?", (GATEWAY, values["route"], body["update_id"])).fetchone()
            if existing:
                if existing[0] != body_hash:
                    raise AdapterError("DUPLICATE_UPDATE_BODY_CONFLICT")
                request_id = existing[1]
            else:
                self.db.execute("INSERT INTO telegram_requests(peer,route,update_id,body_hash,request_id,payload) VALUES(?,?,?,?,?,?)", (GATEWAY, values["route"], body["update_id"], body_hash, request_id, payload))
        request = TelegramRequest(request_id, values["route"], body["update_id"], values["chat_id"], values["user_id"], text, command)
        return {"accepted": True, "request_id": request_id, "reply_text": "요청을 접수했습니다."}, request

    def _post(self, endpoint, payload):
        response = self.transport("POST", self.gateway_url + endpoint, {"content-type": "application/json"}, json.dumps(payload, ensure_ascii=False).encode(), 15)
        require_http_ok(response)
        if response.json().get("ok") is not True:
            raise AdapterError("NOTIFICATION_NOT_ACKNOWLEDGED")
        return {"ok": True}

    def send_message(self, route, chat_id, text, *, notify=False):
        if not isinstance(text, str):
            raise AdapterError("INVALID_NOTIFICATION")
        return self._post("/notify" if notify else "/sendMessage", {"route": str(route), "chat_id": str(chat_id), "text": text, "parse_mode": "", "escape": True})

    def send_document(self, route, chat_id, filename, content: bytes, *, secret_scan, caption=""):
        if not filename or "/" in filename or "\\" in filename or not isinstance(content, bytes):
            raise AdapterError("INVALID_DOCUMENT")
        try:
            reject_credentials(content)
            reject_credentials(caption)
        except CredentialError as error:
            raise AdapterError("DOCUMENT_" + error.code) from None
        if secret_scan(content) is not True:
            raise AdapterError("DOCUMENT_SECRET_CHECK_FAILED")
        return self._post("/sendDocument", {"route": str(route), "chat_id": str(chat_id), "filename": filename,
            "content_base64": base64.b64encode(content).decode("ascii"), "caption": caption, "parse_mode": ""})

    def health(self):
        response = self.transport("GET", self.gateway_url + "/healthz", {}, None, 10)
        require_http_ok(response)
        return response.json()
