import json
import logging
import threading
from datetime import UTC, datetime

from .config import Config


class StateStore:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = config.state_dir / "default_session.json"
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def get_default_session(self) -> str | None:
        with self.lock:
            if not self.path.exists():
                return None
            try:
                data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError):
                logging.exception("failed to read session state")
                return None
            value = data.get("session_id")
            return str(value) if value else None

    def set_default_session(self, session_id: str) -> None:
        payload = {
            "session_id": session_id,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        tmp = self.path.with_suffix(".json.tmp")
        with self.lock:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            tmp.replace(self.path)
