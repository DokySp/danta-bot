"""The only broker mutation path, with durable uncertainty and reservations."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from .config import HumanRequired, aware_time, canonical, digest, utcnow
from .store import Store, TERMINAL, WORKING


@dataclass(frozen=True)
class OrderIntent:
    run_id: str
    plan_id: str
    thesis_id: str
    instrument_id: str
    side: str
    quantity: int
    limit_price: Decimal | None
    expires_at: datetime | None
    reason: str
    account_version: int
    policy_hash: str
    reserve_cash: Decimal = Decimal(0)
    reserve_risk: Decimal = Decimal(0)
    plan_revision: int = 1

    def __post_init__(self):
        if type(self.quantity) is not int or self.quantity <= 0 or type(self.account_version) is not int:
            raise ValueError("Positive integer order quantity and integer state version required")
        if self.side not in {"BUY", "SELL"} or not all((self.plan_id, self.thesis_id, self.instrument_id, self.policy_hash)):
            raise ValueError("Invalid order identifiers/side")
        if self.side == "BUY" and (self.limit_price is None or self.expires_at is None):
            raise ValueError("Entry must have a fixed price cap and expiry")
        if self.side == "SELL" and self.limit_price is not None:
            raise ValueError("This strategy uses market exits")
        for value in (self.limit_price, self.reserve_cash, self.reserve_risk):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError("Finite nonnegative Decimal required")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("Positive entry price required")
        if self.expires_at is not None and self.expires_at.utcoffset() is None:
            raise ValueError("Aware expiry required")

    @property
    def id(self) -> str:
        return digest([self.plan_id, self.plan_revision, self.side])

    def payload(self) -> dict:
        return json.loads(canonical(asdict(self)))


def needed_quantity(actual: int, pending_buys: int, target: int) -> int:
    if any(type(item) is not int or item < 0 for item in (actual, pending_buys, target)):
        raise ValueError("Nonnegative integer quantities required")
    return max(0, target - actual - pending_buys)


class Executor:
    def __init__(self, store: Store, broker, *, mode: str,
                 authorize: Callable[[OrderIntent, str], None],
                 preflight: Callable[[OrderIntent, datetime], None],
                 validate: Callable[[OrderIntent, datetime], None] | None = None):
        self.store, self.broker, self.mode = store, broker, mode
        self.authorize, self.preflight = authorize, preflight
        self.validate = validate or preflight
        self.dispatch_lock = threading.RLock()
        expected = {"offline": "fixture", "paper": "paper", "broker_demo": "demo", "live": "live", "shadow": None}[mode]
        if broker.environment != expected:
            raise HumanRequired("Broker environment differs from execution mode")

    def _validate_state(self, intent: OrderIntent, now: datetime) -> None:
        if not self.store.get("reconciled") or not self.store.get("ownership_complete"):
            raise HumanRequired("Account/order/ownership reconciliation required")
        if intent.side == "BUY" and (self.store.get("paused") or self.store.get("drawdown_paused", False)):
            raise ValueError("NEW_RISK_PAUSED")
        if self.store.get("account_version") != intent.account_version:
            raise ValueError("STALE_ACCOUNT_VERSION")
        if intent.side == "BUY" and not (self.store.get("costs_complete", True) or self.store.get("account_cash_reconciled", False)):
            raise ValueError("ACTUAL_COST_RECONCILIATION_REQUIRED")
        if intent.side == "BUY" and now >= intent.expires_at:
            raise ValueError("ENTRY_EXPIRED")
        working = [row for row in self.store.working() if row["id"] != intent.id]
        same = [row for row in working if row["instrument_id"] == intent.instrument_id]
        if any(row["side"] != intent.side for row in same):
            raise HumanRequired("Opposite order must be canceled and reconciled first")
        if intent.side == "SELL":
            pending = sum(row["quantity"] - row["cumulative_quantity"] for row in same)
            if intent.quantity > self.store.quantity(intent.instrument_id) - pending:
                raise ValueError("STRATEGY_SELL_QUANTITY_EXCEEDED")
        elif intent.reserve_cash > Decimal(self.store.get("cash_krw")) - sum((Decimal(row["reserve_cash"]) for row in working), Decimal(0)):
            raise ValueError("INSUFFICIENT_UNRESERVED_CASH")

    def submit(self, intent: OrderIntent, now: datetime | None = None) -> dict:
        now = now or utcnow()
        with self.dispatch_lock:
            previous = self.store.read("SELECT id FROM intents WHERE idempotency_key=?", (intent.id,))
            if previous:
                return self.store.order(previous[0][0])
        # Provider collection may be slow; protection/reconciliation must keep running.
        self.authorize(intent, "submit")
        self.preflight(intent, now)
        with self.dispatch_lock:
            previous = self.store.read("SELECT id FROM intents WHERE idempotency_key=?", (intent.id,))
            if previous:
                return self.store.order(previous[0][0])
            with self.store.transaction():
                self._validate_state(intent, now)
                payload = intent.payload()
                self.store.db.execute("""INSERT INTO intents(
                    id,idempotency_key,plan_id,thesis_id,instrument_id,side,quantity,limit_price,expires_at,
                    state,account_version,policy_hash,reserve_cash,reserve_risk,payload)
                    VALUES (?,?,?,?,?,?,?,?,?,'VALIDATED',?,?,?,?,?)""",
                    (intent.id, intent.id, intent.plan_id, intent.thesis_id, intent.instrument_id, intent.side,
                     intent.quantity, str(intent.limit_price) if intent.limit_price is not None else None,
                     intent.expires_at.isoformat() if intent.expires_at else None, intent.account_version,
                     intent.policy_hash, str(intent.reserve_cash), str(intent.reserve_risk), canonical(payload)))
                self.store.event(intent.run_id, "INTENT_RESERVED", payload)
            # Persist SUBMITTING before the external call. A crash now is ambiguous.
            with self.store.transaction():
                self.store.db.execute("UPDATE intents SET state='SUBMITTING' WHERE id=?", (intent.id,))
            # Serialize the final local check and POST with pause/account writes.
            # No SQLite transaction spans broker I/O (the runtime has its own connection).
            with self.store.lock:
                try:
                    self.authorize(intent, "submit")
                    self.validate(intent, now)
                    self._validate_state(intent, now)
                    if self.store.order(intent.id)["state"] != "SUBMITTING":
                        raise ValueError("INTENT_INVALIDATED_BEFORE_SEND")
                except Exception as error:
                    with self.store.transaction():
                        self.store.db.execute("UPDATE intents SET state='INVALIDATED',reserve_cash='0',reserve_risk='0' WHERE id=?", (intent.id,))
                        self.store.bump_version()
                        self.store.event(intent.run_id, "ORDER_NOT_SENT", {"intent_id": intent.id, "reason": type(error).__name__}, notify=True)
                    raise
                try:
                    response = self.broker.submit(intent.payload())
                except Exception as error:
                    self._unknown(intent.id, type(error).__name__)
                    return self.store.order(intent.id)
            with self.store.transaction():
                if response.get("status") == "NOT_SENT":
                    self.store.db.execute("UPDATE intents SET state='INVALIDATED',reserve_cash='0',reserve_risk='0' WHERE id=?", (intent.id,))
                    self.store.bump_version()
                    self.store.event(intent.run_id, "ORDER_NOT_SENT", {"intent_id": intent.id, "reason": response.get("reason", "LOCAL_PRE_SEND_FAILURE")}, notify=True)
                elif response.get("status") == "REJECTED":
                    self.store.db.execute("UPDATE intents SET state='REJECTED',reserve_cash='0',reserve_risk='0' WHERE id=?", (intent.id,))
                    self.store.event(intent.run_id, "ORDER_REJECTED", {"intent_id": intent.id, "reason": response.get("reason", "BROKER_REJECTED")}, notify=True)
                elif response.get("status") == "ACKNOWLEDGED" and response.get("broker_id") and response.get("namespace"):
                    self.store.db.execute("UPDATE intents SET state='ACKNOWLEDGED',broker_id=?,broker_namespace=?,broker_metadata=? WHERE id=?", (response["broker_id"], response["namespace"], canonical(response.get("metadata", {})), intent.id))
                    self.store.bump_version()
                    self.store.event(intent.run_id, "ORDER_ACKNOWLEDGED", {"intent_id": intent.id, "broker_id": response["broker_id"]}, notify=True)
                else:
                    self.store.db.execute("UPDATE intents SET state='UNKNOWN' WHERE id=?", (intent.id,))
                    self.store.set("reconciled", False)
                    self.store.event(intent.run_id, "ORDER_UNKNOWN", {"intent_id": intent.id}, notify=True)
            return self.store.order(intent.id)

    def _unknown(self, intent_id: str, reason: str) -> None:
        with self.store.transaction():
            order = self.store.order(intent_id)
            self.store.db.execute("UPDATE intents SET state='UNKNOWN' WHERE id=?", (intent_id,))
            self.store.set("reconciled", False)
            self.store.event(json.loads(order["payload"])["run_id"], "ORDER_UNKNOWN", {"intent_id": intent_id, "reason": reason}, notify=True)

    def cancel(self, intent_id: str, now: datetime | None = None) -> dict:
        with self.dispatch_lock:
            order = self.store.order(intent_id)
            if order["state"] in TERMINAL or order["state"] == "CANCEL_REQUESTED":
                return order
            payload = json.loads(order["payload"])
            intent = OrderIntent(**{**payload, "limit_price": Decimal(payload["limit_price"]) if payload["limit_price"] else None,
                                    "reserve_cash": Decimal(payload["reserve_cash"]), "reserve_risk": Decimal(payload["reserve_risk"]),
                                    "expires_at": aware_time(payload["expires_at"]) if payload["expires_at"] else None})
            self.authorize(intent, "cancel")
            if not order["broker_id"] or order["state"] == "UNKNOWN":
                raise HumanRequired("Unknown submission must be reconciled before cancellation")
            with self.store.transaction():
                self.store.db.execute("UPDATE intents SET state='CANCEL_REQUESTED' WHERE id=?", (intent_id,))
                self.store.event(intent.run_id, "CANCEL_REQUESTED", {"intent_id": intent_id})
            try:
                self.authorize(intent, "cancel")
            except Exception as error:
                with self.store.transaction():
                    self.store.db.execute("UPDATE intents SET state=? WHERE id=?", (order["state"], intent_id))
                    self.store.event(intent.run_id, "CANCEL_NOT_SENT", {"intent_id": intent_id, "reason": type(error).__name__}, notify=True)
                raise
            try:
                response = self.broker.cancel({"broker_id": order["broker_id"], "namespace": order["broker_namespace"],
                                    "metadata": json.loads(order["broker_metadata"]),
                                    "remaining_quantity": order["quantity"] - order["cumulative_quantity"]})
                if response.get("status") in {"NOT_SENT", "REJECTED", "NO_REMAINING_QUANTITY"}:
                    with self.store.transaction():
                        self.store.db.execute("UPDATE intents SET state=? WHERE id=?", (order["state"], intent_id))
                        self.store.event(intent.run_id, "CANCEL_NOT_SENT", {"intent_id": intent_id, "reason": response.get("reason", response["status"])}, notify=True)
                elif response.get("status") != "ACKNOWLEDGED":
                    self._unknown(intent_id, "CANCEL_OUTCOME_UNCONFIRMED")
            except Exception as error:
                with self.store.transaction():
                    self.store.db.execute("UPDATE intents SET state='UNKNOWN' WHERE id=?", (intent_id,))
                    self.store.set("reconciled", False)
                    self.store.event(intent.run_id, "CANCEL_UNCERTAIN", {"intent_id": intent_id, "reason": type(error).__name__}, notify=True)
            # A cancellation ACK is not proof that a concurrent fill did not occur.
            return self.store.order(intent_id)

    def replace(self, *_args, **_kwargs):
        raise ValueError("ENTRY_AUTO_REPRICE_FORBIDDEN: create a new legally reviewed plan")

    def expire_entries(self, now: datetime) -> list[dict]:
        return [self.cancel(row["id"], now) for row in self.store.working()
                if row["side"] == "BUY" and row["expires_at"] and aware_time(row["expires_at"]) <= now]

    def invalidate_unsubmitted_entries(self, run_id: str, reason: str) -> None:
        with self.store.transaction():
            self.store.db.execute("UPDATE intents SET state='INVALIDATED',reserve_cash='0',reserve_risk='0' WHERE side='BUY' AND state IN ('PLANNED','VALIDATED')")
            self.store.bump_version()
            self.store.event(run_id, "UNSUBMITTED_ENTRIES_INVALIDATED", {"reason": reason})

    def reconcile(self, snapshot: dict | None = None) -> dict:
        """Snapshot has complete broker observations; never infer fills from prose."""
        with self.dispatch_lock:
            snapshot = snapshot if snapshot is not None else self.broker.snapshot()
            checked_at = snapshot.get('observed_at') or utcnow().isoformat()
            if snapshot.get("complete") is not True:
                with self.store.transaction():
                    self.store.set('account_checked_at', checked_at)
                    self.store.set("reconciled", False)
                    self.store.set("account_cash_reconciled", False)
                    diagnostics = snapshot.get("diagnostics") or snapshot.get("errors", [])
                    self.store.set("account_diagnostics", diagnostics)
                    self.store.event("reconcile", "ACCOUNT_INCOMPLETE", {"diagnostics": diagnostics}, notify=True)
                return {"status": "DATA_INCOMPLETE"}
            unknown = []
            orders_to_reconcile = self.store.working()
            working_ids = {order["id"] for order in orders_to_reconcile}
            observed_keys = {(item.get("namespace"), item.get("broker_id")) for item in snapshot["orders"] if item.get("broker_id")}
            observed_clients = {item.get("verified_client_intent_id") for item in snapshot["orders"] if item.get("verified_client_intent_id")}
            for row in self.store.read("SELECT * FROM intents"):
                # Closed orders still accept later broker revisions, but their
                # absence from a bounded history page is not a new UNKNOWN.
                if row["id"] not in working_ids and ((row["broker_namespace"], row["broker_id"]) in observed_keys or row["id"] in observed_clients):
                    orders_to_reconcile.append(dict(row))
            for order in orders_to_reconcile:
                candidates = [item for item in snapshot["orders"]
                              if item.get("broker_id") == order["broker_id"] and item.get("namespace") == order["broker_namespace"]] if order["broker_id"] else []
                if not order["broker_id"]:
                    # Matching requires broker-returned client identity, never a local-only key.
                    candidates = [item for item in snapshot["orders"] if item.get("verified_client_intent_id") == order["id"]]
                if len(candidates) != 1:
                    unknown.append(order["id"])
                    continue
                broker = candidates[0]
                if broker["instrument_id"] != order["instrument_id"] or broker["side"] != order["side"] or broker["quantity"] != order["quantity"]:
                    unknown.append(order["id"])
                    continue
                if not order["broker_id"]:
                    with self.store.transaction():
                        self.store.db.execute("UPDATE intents SET broker_id=?,broker_namespace=? WHERE id=?", (broker["broker_id"], broker["namespace"], order["id"]))
                self.store.apply_cumulative_fill(order["id"], quantity=broker["cumulative_quantity"],
                    notional=Decimal(broker["cumulative_notional"]), fees=Decimal(broker["cumulative_fees"]) if broker.get("cumulative_fees") is not None else None,
                    revision=broker["revision"], observed_at=broker["observed_at"], correction=broker.get("correction", False),
                    first_fill_at=broker.get("first_fill_at"), fill_session_id=broker.get("fill_session_id"),
                    fill_time_quality=broker.get("fill_time_quality", "UNKNOWN"),
                    account_cash_managed=snapshot.get("whole_account") is True)
                with self.store.transaction():
                    current = self.store.order(order["id"])
                    if broker["state"] in TERMINAL:
                        state = "FILLED" if current["cumulative_quantity"] == current["quantity"] else (
                            "PARTIAL_CANCELED" if current["cumulative_quantity"] else broker["state"])
                        self.store.db.execute("UPDATE intents SET state=?,reserve_cash='0',reserve_risk='0' WHERE id=?", (state, order["id"]))
                    elif current["state"] == "UNKNOWN":
                        self.store.db.execute("UPDATE intents SET state=? WHERE id=?", (broker["state"], order["id"]))
            ownership_ok = snapshot.get("ownership_complete") is True
            if "strategy_quantities" in snapshot:
                recorded = {row["instrument_id"]: self.store.quantity(row["instrument_id"]) for row in self.store.holdings()}
                actual = {key: value for key, value in snapshot["strategy_quantities"].items() if value}
                ownership_ok = ownership_ok and actual == recorded
            with self.store.transaction():
                self.store.set('account_checked_at', checked_at)
                self.store.set("ownership_complete", ownership_ok)
                self.store.set("reconciled", not unknown and ownership_ok)
                self.store.set("account_cash_reconciled", False)
                self.store.event("reconcile", "RECONCILIATION", {"unresolved_intents": unknown, "ownership_complete": ownership_ok}, notify=bool(unknown) or not ownership_ok)
                if unknown or not ownership_ok:
                    self.store.set('account_diagnostics', ['ORDER_RECONCILIATION_REQUIRED' if unknown else 'OWNERSHIP_RECONCILIATION_REQUIRED'])
            if unknown or not ownership_ok:
                raise HumanRequired("UNALLOCATED or UNKNOWN requires broker evidence/operator decision")
            if snapshot.get("whole_account") is True:
                try:
                    self.store.reconcile_account_cash(snapshot["account_cash"])
                except (KeyError, ValueError, TypeError, ArithmeticError) as error:
                    with self.store.transaction():
                        self.store.set("reconciled", False)
                        self.store.set('account_diagnostics', ['ACCOUNT_CASH_RECONCILIATION_REQUIRED'])
                    raise HumanRequired("ACCOUNT_CASH_RECONCILIATION_REQUIRED") from error
            with self.store.transaction():
                previous_diagnostics = self.store.get('account_diagnostics', [])
                self.store.set('account_checked_at', checked_at)
                self.store.set('account_diagnostics', [])
                self.store.set('account_succeeded_at', self.store.get('account_checked_at'))
                if previous_diagnostics:
                    self.store.event('reconcile', 'ACCOUNT_RECOVERED', {'checked_at': self.store.get('account_checked_at')}, notify=True)
            return {"status": "RECONCILED"}


class FixtureBroker:
    """Synthetic broker observations; calls are explicit and never access a socket."""
    environment = "fixture"

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.submissions = 0
        self.cancel_requests = 0

    def submit(self, intent: dict) -> dict:
        self.submissions += 1
        broker_id = f"fixture-{self.submissions}"
        self.orders[broker_id] = {**intent, "broker_id": broker_id, "namespace": "fixture:synthetic:session",
            "state": "ACKNOWLEDGED", "cumulative_quantity": 0, "cumulative_notional": "0",
            "cumulative_fees": "0", "revision": 0, "observed_at": utcnow().isoformat()}
        return {"status": "ACKNOWLEDGED", "broker_id": broker_id, "namespace": "fixture:synthetic:session"}

    def fill(self, broker_id: str, quantity: int, price: Decimal, fees: Decimal, observed_at: datetime) -> None:
        order = self.orders[broker_id]
        if type(quantity) is not int or quantity < order["cumulative_quantity"] or quantity > order["quantity"]:
            raise ValueError("Invalid fixture cumulative fill")
        if order["side"] == "BUY" and price > Decimal(order["limit_price"]):
            raise ValueError("Fixture cannot fill above the limit")
        order.update(cumulative_quantity=quantity, cumulative_notional=str(price * quantity), cumulative_fees=str(fees),
                     revision=order["revision"] + 1, observed_at=observed_at.isoformat(),
                     first_fill_at=(order.get("first_fill_at") or observed_at.isoformat()) if quantity else None,
                     fill_time_quality="EXACT" if quantity else "UNKNOWN",
                     state="FILLED" if quantity == order["quantity"] else "PARTIALLY_FILLED" if quantity else "ACKNOWLEDGED")

    def cancel(self, request: dict) -> dict:
        self.cancel_requests += 1
        order = self.orders[request["broker_id"]]
        order["state"] = "CANCELED"
        order["revision"] += 1
        return {"status": "ACKNOWLEDGED"}

    def snapshot(self) -> dict:
        return {"complete": True, "ownership_complete": True, "orders": list(self.orders.values())}
