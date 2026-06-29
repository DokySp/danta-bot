import json
import logging
from pathlib import Path


def session_ids(codex_home: Path) -> list[str]:
    ids: list[str] = []
    index_path = codex_home / "session_index.jsonl"
    if index_path.exists():
        ids.extend(session_ids_from_jsonl(index_path))

    sessions_root = codex_home / "sessions"
    if sessions_root.exists():
        session_files = sorted(
            sessions_root.rglob("*.jsonl"),
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
        )
        for path in session_files:
            session_id = session_id_from_session_file(path)
            if session_id:
                ids.append(session_id)

    seen: set[str] = set()
    unique_ids: list[str] = []
    for session_id in ids:
        if session_id in seen:
            continue
        seen.add(session_id)
        unique_ids.append(session_id)
    return unique_ids


def session_ids_from_jsonl(path: Path) -> list[str]:
    ids: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        value = item.get("id")
        if value:
            ids.append(str(value))
    return ids


def session_id_from_session_file(path: Path) -> str | None:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        logging.exception("failed to read codex session file path=%s", path)
        return None

    for line in lines[:20]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") != "session_meta":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        value = payload.get("id")
        if value:
            return str(value)

    stem = path.stem
    if "-" not in stem:
        return None
    candidate = stem.rsplit("-", 5)[-5:]
    return "-".join(candidate) if len(candidate) == 5 else None


def detect_new_session_id(before: list[str], after: list[str]) -> str | None:
    before_set = set(before)
    created = [session_id for session_id in after if session_id not in before_set]
    if created:
        return created[-1]
    if after and (not before or after[-1] != before[-1]):
        return after[-1]
    return None
