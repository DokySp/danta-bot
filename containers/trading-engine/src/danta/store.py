"""SQLite ledger. Transactions contain no network or model calls."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from .config import ROOT, HumanRequired, aware_time, canonical, digest, utcnow

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "PARTIAL_CANCELED", "INVALIDATED"}
WORKING = {"PLANNED", "VALIDATED", "SUBMITTING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN", "CANCEL_REQUESTED"}


def ensure_local(path: Path) -> str:
    """Inspect the actual longest matching mount, including bind mount targets."""
    resolved = path.resolve()
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        mount = Path(left.split()[4].replace("\\040", " ").replace("\\134", "\\"))
        if resolved == mount or mount in resolved.parents:
            mounts.append((len(str(mount)), right.split()[0]))
    if not mounts:
        raise HumanRequired("Storage filesystem could not be established", "SPEC_GAP")
    filesystem = max(mounts)[1]
    if filesystem.startswith(("nfs", "cifs", "smb", "9p", "fuse", "ceph", "gluster")) or filesystem == "autofs":
        raise HumanRequired("SQLite requires verified local storage", "SPEC_GAP")
    return filesystem


class WriterLock:
    def __init__(self, mode: str, account_identity: str, directory: Path | None = None):
        directory = directory or Path(f"/tmp/danta-writers-{os.getuid()}")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HumanRequired("Writer lock directory is not private")
        # account_identity is the broker-confirmed identity, not a profile alias.
        key = hashlib.sha256(f"{mode}:{account_identity}".encode()).hexdigest()
        self.fd = os.open(directory / f"{key}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.fd)
            self.fd = None
            raise HumanRequired("SECOND_WRITER_REJECTED") from error

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Store:
    def __init__(self, path: str | Path, *, mode: str, account_identity: str,
                 initial_cash: Decimal = Decimal(0), lock_dir: Path | None = None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.filesystem = ensure_local(self.path)
        self.writer = WriterLock(mode, account_identity, lock_dir)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript((ROOT / "migrations/001_initial.sql").read_text())
        if "broker_metadata" not in {row[1] for row in self.db.execute("PRAGMA table_info(intents)")}:
            self.db.execute("ALTER TABLE intents ADD COLUMN broker_metadata TEXT NOT NULL DEFAULT '{}'")
        os.chmod(self.path, 0o600)
        scope = {"mode": mode, "identity_hash": digest(account_identity)}
        try:
            with self.transaction():
                existing = self.get("scope")
                if existing and existing != scope:
                    raise HumanRequired("Database mode/account namespace mismatch")
                if existing is None:
                    if not initial_cash.is_finite() or initial_cash < 0:
                        raise ValueError("Invalid initial cash")
                    self.set("scope", scope)
                    self.set("cash_krw", str(initial_cash))
                    self.set("account_version", 0)
                    self.set("paused", False)
                    self.set("reconciled", mode in {"offline", "paper"})
                    self.set("ownership_complete", True)
                    self.set("costs_complete", True)
                    self.event("bootstrap", "BOOTSTRAP", {"scope": scope, "cash_krw": str(initial_cash)})
                # A process may have died on either side of transmission.
                self.db.execute("UPDATE intents SET state='UNKNOWN' WHERE state='SUBMITTING'")
                if self.db.execute("SELECT 1 FROM intents WHERE state IN ('UNKNOWN','CANCEL_REQUESTED')").fetchone():
                    self.set("reconciled", False)
        except Exception:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, canonical(value)))

    def bump_version(self) -> int:
        version = self.get("account_version") + 1
        self.set("account_version", version)
        return version

    def event(self, run_id: str, kind: str, payload: dict, *, notify: bool = False) -> int:
        cursor = self.db.execute("INSERT INTO journal(created_at,run_id,kind,payload) VALUES (?,?,?,?)", (utcnow().isoformat(), run_id, kind, canonical(payload)))
        if notify:
            self.db.execute("INSERT INTO outbox(event_key,payload) VALUES (?,?)", (str(cursor.lastrowid), canonical({"kind": kind, **payload})))
        return cursor.lastrowid

    def queue_document(self, event_key, filename, content, *, route=None, chat_id=None):
        """Snapshot generated HTML in the caller's transaction; never follow queued paths."""
        from .safety import reject_credentials
        document = {"filename": filename, "content": content}
        reject_credentials(document)
        self.db.execute("INSERT OR IGNORE INTO outbox(event_key,payload) VALUES (?,?)",
            (event_key, canonical({"route": route, "chat_id": chat_id, "document": document})))

    def accept_request(self, key: str, payload: dict) -> tuple[str, bool]:
        body_hash = digest(payload)
        with self.transaction():
            previous = self.db.execute("SELECT * FROM requests WHERE request_key=?", (key,)).fetchone()
            if previous:
                if previous["body_hash"] != body_hash:
                    raise ValueError("DUPLICATE_KEY_DIFFERENT_BODY")
                return previous["request_id"], False
            request_id = str(uuid4())
            self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,NULL)", (key, body_hash, request_id, canonical(payload), "ACCEPTED"))
            self.event(request_id, "REQUEST_ACCEPTED", {"request_key": key})
            return request_id, True

    def working(self, instrument_id: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT * FROM intents ORDER BY rowid").fetchall()
        return [dict(row) for row in rows if row["state"] in WORKING and (instrument_id is None or row["instrument_id"] == instrument_id)]

    def holdings(self, owner: str = "strategy") -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM holdings WHERE owner=? AND quantity>0", (owner,))]

    def quantity(self, instrument: str, owner: str = "strategy") -> int:
        return self.db.execute("SELECT COALESCE(SUM(quantity),0) FROM holdings WHERE instrument_id=? AND owner=?", (instrument, owner)).fetchone()[0]

    def reservation(self, name: str = "reserve_cash") -> Decimal:
        if name not in {"reserve_cash", "reserve_risk"}:
            raise ValueError("Unknown reservation")
        return sum((Decimal(row[name]) for row in self.working()), Decimal(0))

    def order(self, intent_id: str) -> dict:
        row = self.db.execute("SELECT * FROM intents WHERE id=?", (intent_id,)).fetchone()
        if not row:
            raise KeyError(intent_id)
        return dict(row)

    def apply_cumulative_fill(self, intent_id: str, *, quantity: int, notional: Decimal,
                              fees: Decimal | None, revision: int, observed_at: str,
                              correction: bool = False, fill_session_id: str | None = None,
                              fill_time_quality: str = "UNKNOWN", first_fill_at: str | None = None) -> bool:
        if type(quantity) is not int or type(revision) is not int or quantity < 0:
            raise ValueError("Invalid cumulative cursor")
        if any(not value.is_finite() or value < 0 for value in (notional, fees) if value is not None):
            raise ValueError("Invalid cumulative amount")
        observed_at = aware_time(observed_at).isoformat()
        if fill_time_quality not in {"EXACT", "FIRST_OBSERVED", "UNKNOWN"} or (first_fill_at is not None) != (fill_time_quality == "EXACT"):
            raise ValueError("Invalid fill time quality")
        if first_fill_at is not None:
            first_fill_at = aware_time(first_fill_at).isoformat()
            if first_fill_at > observed_at:
                raise ValueError("First fill cannot follow its observation")
        with self.transaction():
            order = self.order(intent_id)
            if revision <= order["broker_revision"]:
                return False
            old_q = order["cumulative_quantity"]
            if quantity > order["quantity"]:
                raise HumanRequired("Broker cumulative fill exceeds intent")
            if quantity < old_q and not correction:
                return False
            old_notional = Decimal(order["cumulative_notional"])
            old_fees = Decimal(order["cumulative_fees"])
            fee_settlement = fees is not None and self.get(f"unconfirmed_cost:{intent_id}", False)
            cost_status_changed = fee_settlement or (fees is None and quantity > 0
                and not self.get(f"unconfirmed_cost:{intent_id}", False))
            if fees is None and quantity > 0:
                self.set("costs_complete", False)
                self.set(f"unconfirmed_cost:{intent_id}", True)
                # This retains the last confirmed amount, not a claim of zero fees.
                fees = old_fees
            elif fees is None:
                fees = old_fees
            elif fee_settlement:
                self.set(f"unconfirmed_cost:{intent_id}", False)
                uncertain = self.db.execute("SELECT value FROM meta WHERE key LIKE 'unconfirmed_cost:%'").fetchall()
                self.set("costs_complete", not any(json.loads(row[0]) for row in uncertain))
            if not correction and (notional < old_notional or quantity == old_q and notional != old_notional or
                    not fee_settlement and (fees < old_fees or quantity == old_q and fees != old_fees)):
                raise HumanRequired("Cumulative average/cost changed without broker correction evidence")
            dq, dn, df = quantity - old_q, notional - old_notional, fees - old_fees
            time_changed = False
            if quantity > 0 and order["side"] == "BUY":
                key = f"first-fill:{order['thesis_id']}"
                previous = self.db.execute("SELECT payload FROM observations WHERE id=?", (key,)).fetchone()
                evidence = json.loads(previous[0]) if previous else {
                    "intent_id": intent_id, "observed_at": observed_at, "first_fill_at": first_fill_at,
                    "fill_session_id": fill_session_id, "time_quality": fill_time_quality}
                before = canonical(evidence) if previous else None
                if evidence["intent_id"] == intent_id:
                    if first_fill_at is not None:
                        evidence.update(first_fill_at=first_fill_at, time_quality="EXACT")
                    elif evidence.get("time_quality") == "UNKNOWN" and fill_time_quality == "FIRST_OBSERVED":
                        evidence["time_quality"] = fill_time_quality
                    if fill_session_id is not None and (first_fill_at is not None or evidence.get("fill_session_id") is None):
                        evidence["fill_session_id"] = fill_session_id
                time_changed = before != canonical(evidence)
                if time_changed:
                    self.db.execute("INSERT INTO observations VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET available_at=excluded.available_at,payload=excluded.payload",
                        (key, "FIRST_FILL", observed_at, canonical(evidence)))
            if dq == 0 and dn == 0 and df == 0:
                self.db.execute("UPDATE intents SET broker_revision=? WHERE id=?", (revision, intent_id))
                if time_changed or cost_status_changed:
                    self.bump_version()
                    self.event(json.loads(order["payload"])["run_id"], "FILL_EVIDENCE_UPDATED",
                        {"intent_id": intent_id, "first_fill_at": first_fill_at, "fill_time_quality": fill_time_quality,
                         "fee_settlement": fee_settlement, "costs_complete": self.get("costs_complete"),
                         "observed_at": observed_at})
                return time_changed or cost_status_changed
            sign = 1 if order["side"] == "BUY" else -1
            holding = self.db.execute("SELECT quantity,cost_basis FROM holdings WHERE instrument_id=? AND owner='strategy' AND thesis_id=?", (order["instrument_id"], order["thesis_id"])).fetchone()
            held, basis = (holding[0], Decimal(holding[1])) if holding else (0, Decimal(0))
            new_quantity = held + sign * dq
            if new_quantity < 0:
                raise HumanRequired("Fill would consume external ownership or oversell")
            if sign == 1:
                basis += dn + df
            elif dq and held:
                basis *= Decimal(new_quantity) / Decimal(held)
            self.db.execute("INSERT INTO holdings VALUES (?,'strategy',?,?,?) ON CONFLICT(instrument_id,owner,thesis_id) DO UPDATE SET quantity=excluded.quantity,cost_basis=excluded.cost_basis", (order["instrument_id"], order["thesis_id"], new_quantity, str(basis)))
            cash = Decimal(self.get("cash_krw")) - sign * dn - df
            if cash < 0:
                raise HumanRequired("Fill would overdraw attributed strategy cash")
            self.set("cash_krw", str(cash))
            payload = json.loads(order["payload"])
            remaining = order["quantity"] - quantity
            state = "FILLED" if not remaining else ("CANCEL_REQUESTED" if order["state"] == "CANCEL_REQUESTED" else "PARTIALLY_FILLED")
            if order["state"] in {"CANCELED", "PARTIAL_CANCELED", "EXPIRED"} and remaining:
                state = "PARTIAL_CANCELED"
            reserve_scale = Decimal(remaining) / Decimal(order["quantity"]) if state in WORKING else Decimal(0)
            self.db.execute("UPDATE intents SET cumulative_quantity=?, cumulative_notional=?,cumulative_fees=?,broker_revision=?,state=?,reserve_cash=?,reserve_risk=? WHERE id=?", (quantity, str(notional), str(fees), revision, state, str(Decimal(payload["reserve_cash"]) * reserve_scale), str(Decimal(payload["reserve_risk"]) * reserve_scale), intent_id))
            self.bump_version()
            self.event(payload["run_id"], "FILL_CORRECTION" if correction else "CUMULATIVE_FILL", {"intent_id": intent_id, "quantity_delta": dq, "notional_delta_krw": str(dn), "fee_delta_krw": str(df), "cumulative_quantity": quantity, "observed_at": observed_at}, notify=True)
            return True

    def backup(self, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)
        with self.lock, sqlite3.connect(destination) as target:
            self.db.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity failed")
        os.chmod(destination, 0o600)

    @staticmethod
    def restore(source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError("Restore requires an unused destination")
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as backup, sqlite3.connect(destination) as target:
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Invalid backup")
            backup.backup(target)
        os.chmod(destination, 0o600)

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        self.writer.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
