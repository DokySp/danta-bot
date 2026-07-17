#!/usr/bin/env python3
import html
import json
import logging
import base64
import mimetypes
import os
import re
import secrets
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import yaml


MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
BOT_COMMAND_RE = re.compile(r"^[a-z0-9_]{1,32}$")
KST = timezone(timedelta(hours=9), "KST")
TELEGRAM_MESSAGE_BREAK = "<!--telegram-message-break-->"


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


def env_path(name: str, default: str | None = None) -> Path | None:
    raw = os.getenv(name)
    if raw is None:
        raw = default
    if raw is None or raw.strip() == "":
        return None
    return Path(raw).expanduser()


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


class TelegramHtmlTokenizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tokens: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        self.tokens.append(("start", self.get_starttag_text() or f"<{tag}>", tag.lower()))

    def handle_endtag(self, tag: str) -> None:
        self.tokens.append(("end", f"</{tag.lower()}>", tag.lower()))

    def handle_data(self, data: str) -> None:
        if data:
            self.tokens.append(("data", data, ""))

    def handle_entityref(self, name: str) -> None:
        self.tokens.append(("entity", f"&{name};", ""))

    def handle_charref(self, name: str) -> None:
        self.tokens.append(("entity", f"&#{name};", ""))


def split_telegram_html(text: str, limit: int = 4096) -> list[str]:
    if limit <= 0:
        raise ValueError("Telegram message limit must be positive")
    if len(text) <= limit:
        return [text]

    tokenizer = TelegramHtmlTokenizer()
    tokenizer.feed(text)
    tokenizer.close()

    chunks: list[str] = []
    open_tags: list[tuple[str, str]] = []
    parts: list[str] = []
    has_content = False

    def closing_text(tags: list[tuple[str, str]] | None = None) -> str:
        active = open_tags if tags is None else tags
        return "".join(f"</{tag}>" for tag, _start_text in reversed(active))

    def current_length(tags: list[tuple[str, str]] | None = None) -> int:
        return sum(len(part) for part in parts) + len(closing_text(tags))

    def flush() -> None:
        nonlocal parts, has_content
        if not has_content:
            return
        chunks.append("".join(parts) + closing_text())
        parts = [start_text for _tag, start_text in open_tags]
        has_content = False

    for kind, value, tag in tokenizer.tokens:
        if kind == "start":
            next_tags = [*open_tags, (tag, value)]
            if current_length(next_tags) + len(value) > limit:
                flush()
            if current_length(next_tags) + len(value) > limit:
                raise ValueError("Telegram HTML tag nesting exceeds the message limit")
            parts.append(value)
            open_tags.append((tag, value))
            continue

        if kind == "end":
            if not open_tags or open_tags[-1][0] != tag:
                continue
            parts.append(value)
            open_tags.pop()
            continue

        if kind == "entity":
            if current_length() + len(value) > limit:
                flush()
            if current_length() + len(value) > limit:
                raise ValueError("Telegram HTML entity exceeds the message limit")
            parts.append(value)
            has_content = True
            continue

        remaining = value
        while remaining:
            available = limit - current_length()
            if available <= 0:
                flush()
                available = limit - current_length()
            if available <= 0:
                raise ValueError("Telegram HTML formatting leaves no room for message text")
            take = min(len(remaining), available)
            parts.append(remaining[:take])
            has_content = True
            remaining = remaining[take:]
            if remaining:
                flush()

    flush()
    return chunks


def telegram_html_chunks(text: str, limit: int = 4096) -> list[str]:
    chunks: list[str] = []
    for section in text.split(TELEGRAM_MESSAGE_BREAK):
        sanitized = sanitize_telegram_html(section)
        if sanitized:
            chunks.extend(split_telegram_html(sanitized, limit))
    return chunks


TELEGRAM_CONTEXT_TEXT_LIMIT = 2000
TELEGRAM_ATTACHMENT_DOWNLOAD_LIMIT = 20 * 1024 * 1024
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


@dataclass(frozen=True)
class IncomingTelegramAttachment:
    kind: str
    file_id: str
    file_unique_id: str | None
    file_name: str
    mime_type: str | None
    file_size: int | None
    caption: str | None
    message_id: int | None


@dataclass(frozen=True)
class CachedTelegramAttachment:
    attachment_id: str
    route_id: str
    chat_id: str
    kind: str
    file_name: str
    mime_type: str | None
    size: int
    caption: str | None
    host_path: Path
    metadata_path: Path
    created_at: float


def _telegram_file_size(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def safe_attachment_name(value: Any, fallback: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    name = raw.rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        name = fallback
    return name[:180]


def extract_telegram_attachment(message: dict[str, Any]) -> IncomingTelegramAttachment | None:
    caption = message.get("caption")
    normalized_caption = trim_telegram_text(caption) if isinstance(caption, str) else None
    message_id_value = message.get("message_id")
    message_id = message_id_value if isinstance(message_id_value, int) else None

    document = message.get("document")
    if isinstance(document, dict):
        file_id = str(document.get("file_id", "")).strip()
        if not file_id:
            return None
        fallback = f"document-{message_id or 'unknown'}.bin"
        return IncomingTelegramAttachment(
            kind="document",
            file_id=file_id,
            file_unique_id=str(document.get("file_unique_id", "")).strip() or None,
            file_name=safe_attachment_name(document.get("file_name"), fallback),
            mime_type=str(document.get("mime_type", "")).strip() or None,
            file_size=_telegram_file_size(document.get("file_size")),
            caption=normalized_caption,
            message_id=message_id,
        )

    photos = message.get("photo")
    if not isinstance(photos, list):
        return None
    candidates = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
    if not candidates:
        return None
    photo = max(
        candidates,
        key=lambda item: (
            _telegram_file_size(item.get("file_size")) or 0,
            int(item.get("width") or 0) * int(item.get("height") or 0),
        ),
    )
    return IncomingTelegramAttachment(
        kind="photo",
        file_id=str(photo["file_id"]),
        file_unique_id=str(photo.get("file_unique_id", "")).strip() or None,
        file_name=f"photo-{message_id or 'unknown'}.jpg",
        mime_type="image/jpeg",
        file_size=_telegram_file_size(photo.get("file_size")),
        caption=normalized_caption,
        message_id=message_id,
    )


def build_codex_attachment_input_text(
    text: str,
    attachments: tuple[CachedTelegramAttachment, ...],
) -> str:
    if not attachments:
        return text
    payload = [
        {
            "id": item.attachment_id,
            "type": item.kind,
            "path": str(item.host_path),
            "file_name": item.file_name,
            "mime_type": item.mime_type,
            "size": item.size,
            "caption": item.caption,
        }
        for item in attachments
    ]
    return (
        "아래 Telegram attachments는 사용자가 직전에 업로드해 캐시한 파일입니다. "
        "현재 지시와 관련된 파일을 실제 경로에서 열어 사용하세요. 파일을 자동 실행하지 마세요.\n"
        "<telegram_attachments>\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        "</telegram_attachments>\n\n"
        "<user_message>\n"
        f"{text}\n"
        "</user_message>"
    )


def build_codex_input_text(text: str, message: dict[str, Any]) -> str:
    if text.strip().startswith("/"):
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


def extract_telegram_update_message(
    update: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    message = update.get("message")
    if isinstance(message, dict):
        return message, None

    callback_query = update.get("callback_query")
    if not isinstance(callback_query, dict):
        return None, None
    callback_id = str(callback_query.get("id", "")).strip() or None
    callback_message = callback_query.get("message")
    data = callback_query.get("data")
    if not isinstance(callback_message, dict) or not isinstance(data, str):
        return None, callback_id

    synthetic_message = dict(callback_message)
    sender = callback_query.get("from")
    if isinstance(sender, dict):
        synthetic_message["from"] = sender
    synthetic_message["text"] = data
    return synthetic_message, callback_id


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


def telegram_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(KST)
    except (OSError, OverflowError, ValueError):
        return None


def conversation_log_path(log_dir: Path, event_datetime: datetime) -> Path:
    return log_dir / f"{event_datetime.astimezone(KST).date().isoformat()}.jsonl"


class TelegramAttachmentCache:
    def __init__(
        self,
        cache_dir: Path,
        host_dir: Path,
        *,
        ttl_seconds: int,
        max_file_bytes: int,
        max_total_bytes: int,
        max_pending: int,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("attachment cache TTL must be positive")
        if max_file_bytes <= 0 or max_file_bytes > TELEGRAM_ATTACHMENT_DOWNLOAD_LIMIT:
            raise ValueError("attachment max file bytes must be between 1 and 20 MiB")
        if max_total_bytes < max_file_bytes:
            raise ValueError("attachment max total bytes must be at least max file bytes")
        if max_pending <= 0:
            raise ValueError("attachment max pending must be positive")
        self.cache_dir = cache_dir.resolve()
        self.host_dir = host_dir.resolve()
        self.ttl_seconds = ttl_seconds
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_pending = max_pending
        self.lock = threading.RLock()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _component(value: str) -> str:
        component = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip(".")
        return component or "unknown"

    def _chat_dir(self, route_id: str, chat_id: str) -> Path:
        route_dir = self.cache_dir / self._component(route_id)
        route_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_route_dir = route_dir.resolve()
        if not resolved_route_dir.is_relative_to(self.cache_dir):
            raise ValueError("unsafe attachment cache route path")
        path = resolved_route_dir / self._component(chat_id)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(self.cache_dir):
            raise ValueError("unsafe attachment cache chat path")
        return resolved_path

    @staticmethod
    def _storage_suffix(file_name: str, kind: str) -> str:
        if kind == "photo":
            return ".jpg"
        suffix = Path(file_name).suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
            return suffix
        return ".bin"

    def store(
        self,
        route_id: str,
        chat_id: str,
        attachment: IncomingTelegramAttachment,
        content: bytes,
        *,
        now: float | None = None,
    ) -> CachedTelegramAttachment:
        timestamp = time.time() if now is None else now
        actual_size = len(content)
        if attachment.file_size is not None and attachment.file_size > self.max_file_bytes:
            raise ValueError("파일이 허용 크기 20MiB를 초과합니다.")
        if actual_size > self.max_file_bytes:
            raise ValueError("파일이 허용 크기 20MiB를 초과합니다.")

        with self.lock:
            self.cleanup_expired(now=timestamp)
            pending = self._list_pending_locked(route_id, chat_id, now=timestamp)
            if len(pending) >= self.max_pending:
                raise ValueError(f"대기 파일은 최대 {self.max_pending}개까지 저장할 수 있습니다.")
            if self._total_size_locked() + actual_size > self.max_total_bytes:
                raise ValueError("첨부파일 캐시 전체 용량이 가득 찼습니다. 잠시 후 다시 시도해 주세요.")

            attachment_id = f"{attachment.message_id or 'm'}-{secrets.token_hex(6)}"
            chat_dir = self._chat_dir(route_id, chat_id)
            data_path = chat_dir / f"{attachment_id}{self._storage_suffix(attachment.file_name, attachment.kind)}"
            metadata_path = chat_dir / f"{attachment_id}.json"
            relative_path = data_path.relative_to(self.cache_dir)
            metadata = {
                "version": 1,
                "attachment_id": attachment_id,
                "route_id": route_id,
                "chat_id": chat_id,
                "kind": attachment.kind,
                "file_unique_id": attachment.file_unique_id,
                "file_name": attachment.file_name,
                "mime_type": attachment.mime_type,
                "size": actual_size,
                "caption": attachment.caption,
                "message_id": attachment.message_id,
                "relative_path": str(relative_path),
                "created_at": timestamp,
                "status": "pending",
            }
            data_temp = data_path.with_name(f".{data_path.name}.{secrets.token_hex(4)}.part")
            metadata_temp = metadata_path.with_name(
                f".{metadata_path.name}.{secrets.token_hex(4)}.part"
            )
            try:
                with data_temp.open("xb") as handle:
                    os.chmod(data_temp, 0o600)
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                data_temp.replace(data_path)
                with metadata_temp.open("x", encoding="utf-8") as handle:
                    os.chmod(metadata_temp, 0o600)
                    json.dump(metadata, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                metadata_temp.replace(metadata_path)
            except Exception:
                data_temp.unlink(missing_ok=True)
                metadata_temp.unlink(missing_ok=True)
                data_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                raise
            return self._from_metadata(metadata, metadata_path)

    def list_pending(
        self,
        route_id: str,
        chat_id: str,
        *,
        now: float | None = None,
    ) -> tuple[CachedTelegramAttachment, ...]:
        timestamp = time.time() if now is None else now
        with self.lock:
            self.cleanup_expired(now=timestamp)
            return self._list_pending_locked(route_id, chat_id, now=timestamp)

    def _list_pending_locked(
        self,
        route_id: str,
        chat_id: str,
        *,
        now: float,
    ) -> tuple[CachedTelegramAttachment, ...]:
        del now
        route_dir = (self.cache_dir / self._component(route_id)).resolve()
        if not route_dir.is_relative_to(self.cache_dir):
            logging.warning("ignored unsafe attachment cache route path=%s", route_dir)
            return ()
        chat_dir = (route_dir / self._component(chat_id)).resolve()
        if not chat_dir.is_relative_to(self.cache_dir):
            logging.warning("ignored unsafe attachment cache chat path=%s", chat_dir)
            return ()
        if not chat_dir.is_dir():
            return ()
        items: list[CachedTelegramAttachment] = []
        for metadata_path in chat_dir.glob("*.json"):
            if not self._safe_metadata_path(metadata_path):
                logging.warning("ignored unsafe attachment metadata path=%s", metadata_path)
                continue
            metadata = self._read_metadata(metadata_path)
            if metadata is None or metadata.get("status") != "pending":
                continue
            try:
                item = self._from_metadata(metadata, metadata_path)
            except (KeyError, TypeError, ValueError):
                logging.warning("ignored invalid attachment metadata path=%s", metadata_path)
                continue
            if not self._container_data_path(metadata).is_file():
                metadata_path.unlink(missing_ok=True)
                continue
            items.append(item)
        return tuple(sorted(items, key=lambda item: (item.created_at, item.attachment_id)))

    def mark_consumed(
        self,
        attachments: tuple[CachedTelegramAttachment, ...],
        *,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self.lock:
            for attachment in attachments:
                if not self._safe_metadata_path(attachment.metadata_path):
                    logging.warning(
                        "refused to consume unsafe attachment metadata path=%s",
                        attachment.metadata_path,
                    )
                    continue
                metadata = self._read_metadata(attachment.metadata_path)
                if metadata is None or metadata.get("status") != "pending":
                    continue
                metadata["status"] = "consumed"
                metadata["consumed_at"] = timestamp
                temp_path = attachment.metadata_path.with_name(
                    f".{attachment.metadata_path.name}.{secrets.token_hex(4)}.part"
                )
                try:
                    with temp_path.open("x", encoding="utf-8") as handle:
                        os.chmod(temp_path, 0o600)
                        json.dump(metadata, handle, ensure_ascii=False, separators=(",", ":"))
                        handle.flush()
                        os.fsync(handle.fileno())
                    temp_path.replace(attachment.metadata_path)
                finally:
                    temp_path.unlink(missing_ok=True)

    def cleanup_expired(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self.lock:
            for metadata_path in self.cache_dir.rglob("*.json"):
                if not self._safe_metadata_path(metadata_path):
                    logging.warning("ignored unsafe attachment metadata path=%s", metadata_path)
                    continue
                metadata = self._read_metadata(metadata_path)
                if metadata is None:
                    continue
                try:
                    created_at = float(metadata["created_at"])
                except (KeyError, TypeError, ValueError):
                    continue
                if timestamp - created_at < self.ttl_seconds:
                    continue
                try:
                    self._container_data_path(metadata).unlink(missing_ok=True)
                except ValueError:
                    logging.warning("ignored unsafe expired attachment path=%s", metadata_path)
                metadata_path.unlink(missing_ok=True)

    def _total_size_locked(self) -> int:
        total = 0
        for metadata_path in self.cache_dir.rglob("*.json"):
            if not self._safe_metadata_path(metadata_path):
                logging.warning("ignored unsafe attachment metadata path=%s", metadata_path)
                continue
            metadata = self._read_metadata(metadata_path)
            if metadata is None:
                continue
            size = metadata.get("size")
            if isinstance(size, int) and not isinstance(size, bool) and size > 0:
                total += size
        return total

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logging.warning("failed to read attachment metadata path=%s", path)
            return None
        return value if isinstance(value, dict) else None

    def _safe_metadata_path(self, path: Path) -> bool:
        if path.is_symlink():
            return False
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False
        return resolved.is_relative_to(self.cache_dir) and resolved.is_file()

    def _container_data_path(self, metadata: dict[str, Any]) -> Path:
        relative = Path(str(metadata["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe attachment cache path")
        path = (self.cache_dir / relative).resolve()
        if not path.is_relative_to(self.cache_dir):
            raise ValueError("unsafe attachment cache path")
        return path

    def _from_metadata(
        self,
        metadata: dict[str, Any],
        metadata_path: Path,
    ) -> CachedTelegramAttachment:
        relative = Path(str(metadata["relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("unsafe attachment cache path")
        host_path = (self.host_dir / relative).resolve()
        if not host_path.is_relative_to(self.host_dir):
            raise ValueError("unsafe attachment host path")
        return CachedTelegramAttachment(
            attachment_id=str(metadata["attachment_id"]),
            route_id=str(metadata["route_id"]),
            chat_id=str(metadata["chat_id"]),
            kind=str(metadata["kind"]),
            file_name=str(metadata["file_name"]),
            mime_type=str(metadata["mime_type"]) if metadata.get("mime_type") else None,
            size=int(metadata["size"]),
            caption=str(metadata["caption"]) if metadata.get("caption") else None,
            host_path=host_path,
            metadata_path=metadata_path,
            created_at=float(metadata["created_at"]),
        )


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
    instruction: str | None = None

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
        instruction_value = item.get("instruction")
        instruction = str(instruction_value).strip() if instruction_value is not None else None
        if instruction == "":
            instruction = None

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
        commands.append(BotCommand(command=command, description=description, instruction=instruction))

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
        if not command.instruction:
            continue

        source = f"/{command.command}"
        if stripped == source:
            return command.instruction
        if stripped.startswith(f"{source} "):
            return f"{command.instruction}{stripped[len(source):]}"

    return text


@dataclass(frozen=True)
class Config:
    version: str
    http_timeout: int
    gateway_host: str
    gateway_port: int
    gateway_routes_file: Path
    conversation_log_dir: Path | None
    attachment_cache_dir: Path
    attachment_host_dir: Path
    attachment_ttl_seconds: int
    attachment_max_file_bytes: int
    attachment_max_total_bytes: int
    attachment_max_pending: int

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            version=os.getenv("APP_VERSION", "1.0.0"),
            http_timeout=env_int("HTTP_TIMEOUT", 10),
            gateway_host=os.getenv("GATEWAY_HOST", "0.0.0.0"),
            gateway_port=env_int("GATEWAY_PORT", 8080),
            gateway_routes_file=Path(os.getenv("GATEWAY_ROUTES_FILE", "/app/config/routes.yaml")),
            conversation_log_dir=env_path(
                "GATEWAY_CONVERSATION_LOG_DIR",
                "/workspace/memory/telegram-conversations",
            ),
            attachment_cache_dir=env_path(
                "GATEWAY_ATTACHMENT_CACHE_DIR",
                "/workspace/memory/telegram-inbox",
            )
            or Path("/workspace/memory/telegram-inbox"),
            attachment_host_dir=env_path(
                "GATEWAY_ATTACHMENT_HOST_DIR",
                "/workspace/memory/telegram-inbox",
            )
            or Path("/workspace/memory/telegram-inbox"),
            attachment_ttl_seconds=env_int("GATEWAY_ATTACHMENT_TTL_SECONDS", 86400),
            attachment_max_file_bytes=env_int(
                "GATEWAY_ATTACHMENT_MAX_FILE_BYTES",
                TELEGRAM_ATTACHMENT_DOWNLOAD_LIMIT,
            ),
            attachment_max_total_bytes=env_int(
                "GATEWAY_ATTACHMENT_MAX_TOTAL_BYTES",
                200 * 1024 * 1024,
            ),
            attachment_max_pending=env_int("GATEWAY_ATTACHMENT_MAX_PENDING", 10),
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
        self.generation = 0
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
            self.generation += 1
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

    def current_generation(self) -> int:
        return self.generation


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

    def post_multipart(
        self,
        method: str,
        payload: dict[str, Any],
        files: dict[str, tuple[str, bytes, str]],
        timeout: int | None = None,
    ) -> dict[str, Any]:
        boundary = f"telegram-gateway-{time.time_ns()}"
        parts: list[bytes] = []
        for key, value in payload.items():
            parts.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"),
                    str(value).encode("utf-8"),
                    b"\r\n",
                ]
            )
        for key, (filename, content, content_type) in files.items():
            safe_filename = filename.replace('"', "")
            parts.extend(
                [
                    f"--{boundary}\r\n".encode("utf-8"),
                    (
                        f'Content-Disposition: form-data; name="{key}"; '
                        f'filename="{safe_filename}"\r\n'
                    ).encode("utf-8"),
                    f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
                    content,
                    b"\r\n",
                ]
            )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        request = Request(
            f"{self.base_url}/{method}",
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
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
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            payload["offset"] = offset

        timeout = self.route.poll_timeout + self.route.http_timeout
        response = self.post_form("getUpdates", payload, timeout=timeout)
        result = response.get("result", [])
        return result if isinstance(result, list) else []

    def download_file(self, file_id: str, max_bytes: int) -> bytes:
        if not file_id:
            raise ValueError("Telegram file_id is required")
        file_response = self.post_form("getFile", {"file_id": file_id})
        result = file_response.get("result")
        file_path = str(result.get("file_path", "")).strip() if isinstance(result, dict) else ""
        if not file_path:
            raise RuntimeError("Telegram getFile response has no file_path")

        request = Request(
            f"https://api.telegram.org/file/bot{self.route.telegram_bot_token}/{quote(file_path, safe='/')}",
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.route.http_timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise ValueError("파일이 허용 크기 20MiB를 초과합니다.")
                content = response.read(max_bytes + 1)
        except HTTPError as exc:
            raise RuntimeError(f"Telegram file download failed: HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError("Telegram file download failed") from exc
        if len(content) > max_bytes:
            raise ValueError("파일이 허용 크기 20MiB를 초과합니다.")
        return content

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
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        mode = parse_mode if parse_mode is not None else self.route.parse_mode
        outbound_text = escape_markdown_v2(text) if escape and mode == "MarkdownV2" else text
        if mode and mode.lower() == "html":
            chunks = telegram_html_chunks(outbound_text)
        else:
            chunks = split_telegram_text(outbound_text)

        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if mode:
                payload["parse_mode"] = mode
            if reply_markup is not None and index == 0:
                payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            self.post_form("sendMessage", payload)

    def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        self.post_form("sendChatAction", {"chat_id": chat_id, "action": action})

    def answer_callback_query(self, callback_query_id: str) -> None:
        self.post_form("answerCallbackQuery", {"callback_query_id": callback_query_id})

    def send_binary_file(
        self,
        method: str,
        field_name: str,
        chat_id: str,
        filename: str,
        content: bytes,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = sanitize_telegram_html(caption) if parse_mode == "HTML" else caption
        if parse_mode:
            payload["parse_mode"] = parse_mode
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.post_multipart(method, payload, {field_name: (filename, content, content_type)})


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
        self.bot_commands_generation = 0
        self.bot_command_routes: dict[str, RouteConfig] = {}
        self.conversation_log_lock = threading.Lock()
        self.attachment_cache = TelegramAttachmentCache(
            config.attachment_cache_dir,
            config.attachment_host_dir,
            ttl_seconds=config.attachment_ttl_seconds,
            max_file_bytes=config.attachment_max_file_bytes,
            max_total_bytes=config.attachment_max_total_bytes,
            max_pending=config.attachment_max_pending,
        )
        self.attachment_cleanup_interval_seconds = max(
            60,
            min(300, config.attachment_ttl_seconds // 4),
        )
        self.next_attachment_cleanup_at = 0.0

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
                if self.path not in {"/sendMessage", "/notify", "/sendChatAction", "/sendPhoto", "/sendDocument"}:
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

                    if self.path in {"/sendPhoto", "/sendDocument"}:
                        route = app.resolve_send_route(payload)
                        chat_id = str(payload.get("chat_id") or route.default_chat_id or "")
                        if not chat_id:
                            self._write_json(400, {"ok": False, "error": "chat_id is required"})
                            return
                        filename = str(payload.get("filename") or "").strip()
                        if not filename:
                            self._write_json(400, {"ok": False, "error": "filename is required"})
                            return
                        encoded = str(payload.get("content_base64") or "")
                        if not encoded:
                            self._write_json(400, {"ok": False, "error": "content_base64 is required"})
                            return
                        content = base64.b64decode(encoded)
                        caption = str(payload.get("caption") or "").strip() or None
                        parse_mode = payload.get("parse_mode")
                        if parse_mode is not None:
                            parse_mode = str(parse_mode)
                        method = "sendPhoto" if self.path == "/sendPhoto" else "sendDocument"
                        field_name = "photo" if self.path == "/sendPhoto" else "document"
                        TelegramClient(route).send_binary_file(
                            method,
                            field_name,
                            chat_id,
                            filename,
                            content,
                            caption=caption,
                            parse_mode=parse_mode,
                        )
                        app.append_outbound_conversation_event(
                            route,
                            chat_id,
                            method,
                            caption,
                            source_path=self.path,
                            extra={"filename": filename},
                        )
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
                    reply_markup = payload.get("reply_markup")
                    if reply_markup is not None and not isinstance(reply_markup, dict):
                        self._write_json(400, {"ok": False, "error": "reply_markup must be an object"})
                        return
                    TelegramClient(route).send_message(
                        chat_id,
                        text,
                        parse_mode=parse_mode,
                        escape=escape,
                        reply_markup=reply_markup,
                    )
                    app.append_outbound_conversation_event(route, chat_id, "sendMessage", text, source_path=self.path)
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
        current_routes = routing.routes
        route_ids = set(current_routes) | set(self.bot_command_routes)

        registered_routes: dict[str, RouteConfig] = {}
        for route_id in sorted(route_ids):
            route = current_routes.get(route_id)
            previous_route = self.bot_command_routes.get(route_id)
            if route is None:
                if previous_route is None or not previous_route.bot_commands:
                    continue
                route = replace(previous_route, bot_commands=())

            should_update = bool(route.bot_commands)
            if previous_route is not None:
                should_update = should_update or bool(previous_route.bot_commands)
                should_update = should_update or route.telegram_bot_token != previous_route.telegram_bot_token
            if not should_update:
                logging.info("skipping Telegram bot command registration route=%s", route.route_id)
                registered_routes[route_id] = route
                continue

            command_names = ",".join(command.command for command in route.bot_commands)
            log_commands = command_names or "<none>"
            try:
                if (
                    previous_route is not None
                    and previous_route.bot_commands
                    and route.telegram_bot_token != previous_route.telegram_bot_token
                ):
                    TelegramClient(replace(previous_route, bot_commands=())).set_my_commands()
                    logging.info("cleared Telegram bot commands route=%s previous_token=true", route.route_id)
                TelegramClient(route).set_my_commands()
            except Exception:
                logging.exception("failed to register Telegram bot commands route=%s", route.route_id)
                if previous_route is not None:
                    registered_routes[route_id] = previous_route
                continue
            logging.info(
                "registered Telegram bot commands route=%s commands=%s",
                route.route_id,
                log_commands,
            )
            if route_id in current_routes:
                registered_routes[route_id] = route

        self.bot_command_routes = registered_routes
        self.bot_commands_generation = self.routing_store.current_generation()

    def register_bot_commands_if_reloaded(self) -> None:
        if self.bot_commands_generation == self.routing_store.current_generation():
            return
        self.register_bot_commands()

    def poll_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.cleanup_attachment_cache_if_due()
                routing = self.routing_store.get()
                self.register_bot_commands_if_reloaded()
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

    def cleanup_attachment_cache_if_due(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        if timestamp < self.next_attachment_cleanup_at:
            return
        self.next_attachment_cleanup_at = timestamp + self.attachment_cleanup_interval_seconds
        try:
            self.attachment_cache.cleanup_expired(now=timestamp)
        except Exception:
            logging.exception("failed to clean Telegram attachment cache")

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
        message, callback_query_id = extract_telegram_update_message(update)
        if message is None:
            return

        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id = str(chat.get("id", ""))
        text = message.get("text")

        if not chat_id:
            return
        if chat_id not in route.allowed_chat_ids:
            logging.warning("ignored unauthorized chat_id=%s route=%s", chat_id, route.route_id)
            return

        client = TelegramClient(route)
        if callback_query_id is not None:
            try:
                client.answer_callback_query(callback_query_id)
            except Exception:
                logging.exception(
                    "failed to answer Telegram callback query route=%s chat_id=%s",
                    route.route_id,
                    chat_id,
                )

        if not isinstance(text, str):
            self.append_inbound_conversation_event(route, update, message, None, None)
            attachment = extract_telegram_attachment(message)
            if attachment is None:
                logging.info(
                    "stored unsupported non-text telegram message route=%s chat_id=%s",
                    route.route_id,
                    chat_id,
                )
                return
            try:
                if (
                    attachment.file_size is not None
                    and attachment.file_size > self.attachment_cache.max_file_bytes
                ):
                    raise ValueError("파일이 허용 크기 20MiB를 초과합니다.")
                content = client.download_file(
                    attachment.file_id,
                    self.attachment_cache.max_file_bytes,
                )
                cached = self.attachment_cache.store(
                    route.route_id,
                    chat_id,
                    attachment,
                    content,
                )
                logging.info(
                    "cached Telegram attachment route=%s chat_id=%s attachment_id=%s size=%s",
                    route.route_id,
                    chat_id,
                    cached.attachment_id,
                    cached.size,
                )
            except ValueError as exc:
                client.send_message(chat_id, f"파일을 저장할 수 없습니다: {exc}")
                return
            except (OSError, RuntimeError):
                logging.exception(
                    "failed to cache Telegram attachment route=%s chat_id=%s",
                    route.route_id,
                    chat_id,
                )
                client.send_message(
                    chat_id,
                    "파일을 저장하지 못했습니다. 게이트웨이 로그를 확인해 주세요.",
                )
                return

            caption_prompt = (attachment.caption or "").strip()
            if not caption_prompt:
                client.send_message(
                    chat_id,
                    f"📎 저장했습니다: {cached.file_name}\n다음 일반 메시지로 작업을 지시해 주세요.",
                )
                self.append_outbound_conversation_event(
                    route,
                    chat_id,
                    "sendMessage",
                    f"attachment cached: {cached.file_name}",
                    source_path="attachment_cache",
                    extra={"attachment_id": cached.attachment_id, "size": cached.size},
                )
                return

            pending_attachments = self.attachment_cache.list_pending(route.route_id, chat_id)
            resolved = self.router.resolve(route.route_id, caption_prompt)
            codex_text = build_codex_input_text(resolved.text, message)
            codex_text = build_codex_attachment_input_text(codex_text, pending_attachments)
            payload = {
                "source": "telegram",
                "gateway_version": self.config.version,
                "route": route.route_id,
                "update_id": update.get("update_id"),
                "message_id": attachment.message_id,
                "chat_id": chat_id,
                "user_id": str(sender.get("id", "")),
                "username": sender.get("username"),
                "text": codex_text,
                "raw_message": message,
            }
            try:
                logging.info(
                    "routing attachment caption route=%s chat_id=%s url=%s attachment_count=%s",
                    route.route_id,
                    chat_id,
                    resolved.url,
                    len(pending_attachments),
                )
                response = self.codex.post_message(resolved.url, payload)
            except (OSError, RuntimeError):
                logging.exception(
                    "failed to submit cached Telegram attachment caption route=%s chat_id=%s",
                    route.route_id,
                    chat_id,
                )
                client.send_message(
                    chat_id,
                    "파일은 저장했지만 Codex 실행에 실패했습니다. "
                    "다음 일반 메시지로 다시 지시해 주세요.",
                )
                return

            if pending_attachments:
                self.attachment_cache.mark_consumed(pending_attachments)
            reply_text = None
            if response:
                reply_text = response.get("reply_text") or response.get("text")
            if reply_text:
                client.send_message(chat_id, str(reply_text))
                self.append_outbound_conversation_event(
                    route,
                    chat_id,
                    "sendMessage",
                    str(reply_text),
                    source_path="codex_reply",
                )
            elif route.ack_text:
                client.send_message(chat_id, route.ack_text)
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
        self.append_inbound_conversation_event(route, update, message, text, routed_text)

        if route.echo_mode:
            logging.info("echoing telegram message route=%s chat_id=%s message_id=%s", route.route_id, chat_id, message_id)
            client.send_message(chat_id, text)
            self.append_outbound_conversation_event(route, chat_id, "sendMessage", text, source_path="echo")
            return

        pending_attachments: tuple[CachedTelegramAttachment, ...] = ()
        if not routed_text.strip().startswith("/"):
            pending_attachments = self.attachment_cache.list_pending(route.route_id, chat_id)

        resolved = self.router.resolve(route.route_id, routed_text)
        codex_text = build_codex_input_text(resolved.text, message)
        codex_text = build_codex_attachment_input_text(codex_text, pending_attachments)
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
        if pending_attachments:
            self.attachment_cache.mark_consumed(pending_attachments)

        reply_text = None
        if response:
            reply_text = response.get("reply_text") or response.get("text")
        if reply_text:
            client.send_message(chat_id, str(reply_text))
            self.append_outbound_conversation_event(route, chat_id, "sendMessage", str(reply_text), source_path="codex_reply")
        elif route.ack_text:
            client.send_message(chat_id, route.ack_text)
            self.append_outbound_conversation_event(route, chat_id, "sendMessage", route.ack_text, source_path="ack")

    def append_inbound_conversation_event(
        self,
        route: RouteConfig,
        update: dict[str, Any],
        message: dict[str, Any],
        text: str | None,
        routed_text: str | None,
    ) -> None:
        event_datetime = telegram_datetime(message.get("date")) or datetime.now(KST)
        event = {
            "recorded_at": datetime.now(KST).isoformat(),
            "occurred_at": event_datetime.isoformat(),
            "direction": "inbound",
            "type": "telegram_message",
            "route": route.route_id,
            "update_id": update.get("update_id"),
            "message_id": message.get("message_id"),
            "message_date": message.get("date"),
            "chat": summarize_telegram_chat(message.get("chat")),
            "from": summarize_telegram_user(message.get("from")),
            "text": text,
            "caption": message.get("caption") if isinstance(message.get("caption"), str) else None,
            "routed_text": routed_text,
            "content_types": [field for field in TELEGRAM_CONTEXT_CONTENT_FIELDS if field in message],
            "reply_context": telegram_reply_context(message),
            "raw_message": message,
        }
        self.append_conversation_event(event, event_datetime)

    def append_outbound_conversation_event(
        self,
        route: RouteConfig,
        chat_id: str,
        method: str,
        text: str | None,
        *,
        source_path: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        event_datetime = datetime.now(KST)
        event = {
            "recorded_at": event_datetime.isoformat(),
            "occurred_at": event_datetime.isoformat(),
            "direction": "outbound",
            "type": "telegram_send",
            "route": route.route_id,
            "chat": {"id": chat_id},
            "method": method,
            "source_path": source_path,
            "text": text,
        }
        if extra:
            event.update(extra)
        self.append_conversation_event(event, event_datetime)

    def append_conversation_event(self, event: dict[str, Any], event_datetime: datetime) -> None:
        log_dir = self.config.conversation_log_dir
        if log_dir is None:
            return

        path = conversation_log_path(log_dir, event_datetime)

        try:
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            with self.conversation_log_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as file:
                    file.write(line + "\n")
        except Exception:
            logging.exception("failed to append Telegram conversation log path=%s route=%s", path, event.get("route"))


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
