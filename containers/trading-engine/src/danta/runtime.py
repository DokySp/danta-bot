"""Approved external adapters and explicit provider-to-domain normalization.

Importing this module reads no environment, authentication, account or network.
See docs/runtime-contract.md for the required, hash-bound capability manifest.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import time
from copy import copy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from uuid import uuid4

from .adapters import AdapterError, http_transport
from .adapters.codex_cli import CodexAdapter
from .adapters.disclosures import DartAdapter, ORIGIN
from .adapters.kis import BASE_URLS, MASTER_ORIGIN, KisAdapter, KisCredentials, KisTokenCache
from .application import MarketBundle, code_identity
from .config import HumanRequired, ROOT, aware_time, canonical, digest, load_secrets, utcnow
from .decision import DecisionProposal, document_scope, freeze_documents, validate_proposal
from .market import EventRegistry, SessionCalendar, calculate_features
from .models import CostSchedule, DailyBar, EventRecord, Instrument, MarketFact, Quote, Session
from .portfolio import buy_commission, sell_cost, slippage
from .strategy import monitor_quote_max_age, quote_fresh
from .safety import reject_credentials

SEOUL = ZoneInfo("Asia/Seoul")
AUTH_ERRORS = {"AUTH_FAILED", "AUTHORIZATION_REQUIRED", "AUTH_CREDENTIALS_REQUIRED", "DART_AUTH_REQUIRED", "OFFLINE_NETWORK_BLOCKED"}
DIAGNOSTIC_FIELDS = ('endpoint', 'http_status', 'provider_code', 'provider_message', 'requested_at',
                     'elapsed_seconds', 'attempt_count', 'transport_error', 'failed_page', 'method', 'tr_id', 'request_stage', 'field',
                     'retry_after_seconds', 'request_sent')


def _failure_diagnostic(error, endpoint, *, reason=None, field=None):
    detail = getattr(error, 'code', 'PROVIDER_FIELD_MISSING' if isinstance(error, KeyError) else 'PROVIDER_FIELD_UNVERIFIED')
    if not isinstance(error, AdapterError) and isinstance(error, ValueError) and re.fullmatch(r'[A-Z][A-Z_]{1,79}', str(error)):
        detail = str(error)
    diagnostic = {'endpoint': endpoint, 'reason': reason or detail, 'detail': detail,
                  'error_type': type(error).__name__, **{key: value for key, value in
                    getattr(error, 'diagnostic', {}).items() if key in DIAGNOSTIC_FIELDS}}
    if field is None and isinstance(error, KeyError) and error.args:
        field = error.args[0]
    if isinstance(field, str) and re.fullmatch(r'[a-z][a-z0-9_.]{0,63}', field):
        diagnostic['field'] = field
    return diagnostic


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("BOOLEAN_NOT_MONEY")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("INVALID_PROVIDER_AMOUNT")
    return number


def _quantity(value):
    if isinstance(value, bool) or not re.fullmatch(r"\d+", str(value)):
        raise ValueError("INVALID_PROVIDER_QUANTITY")
    return int(value)


def _field(record, path):
    if not isinstance(path, str) or not path or any(not part for part in path.split(".")):
        raise ValueError("MISSING_VERIFIED_FIELD_MAPPING")
    result = record
    for part in path.split("."):
        result = result[part]
    return result


def _timestamp(day, hour):
    day = date.fromisoformat(str(day))
    text = str(hour).replace(":", "")
    if not re.fullmatch(r"\d{6}", text):
        raise ValueError("PROVIDER_OBSERVATION_TIME_UNVERIFIED")
    return datetime(day.year, day.month, day.day, int(text[:2]), int(text[2:4]), int(text[4:]), tzinfo=SEOUL)


def _json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("DUPLICATE_MANIFEST_KEY")
            value[key] = item
        return value
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("NONFINITE_JSON")))


class RuntimeState:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path,timeout=30,isolation_level=None,check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # Large disclosure documents must not hold the trading ledger's writer lock.
        cache_path = self.path.with_name(self.path.stem+'-cache.sqlite')
        self.cache_db = sqlite3.connect(cache_path,timeout=30,
                                       isolation_level=None,check_same_thread=False)
        cache_path.chmod(0o600)
        self.cache_db.execute("CREATE TABLE IF NOT EXISTS runtime_cache(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        migrate_legacy = not self.cache_db.execute('SELECT 1 FROM runtime_cache LIMIT 1').fetchone()
        if (migrate_legacy and
                self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_cache'").fetchone()):
            self.cache_db.execute('BEGIN IMMEDIATE')
            try:
                self.cache_db.executemany('INSERT INTO runtime_cache VALUES (?,?)',
                                         self.db.execute("SELECT namespace,payload FROM runtime_cache WHERE namespace<>'disclosure_records'").fetchall())
                self.cache_db.execute('COMMIT')
            except BaseException:
                self.cache_db.execute('ROLLBACK')
                raise
        from .disclosure_cache import load_records
        # ponytail: keep receipt metadata in memory; query by issuer if this index outgrows RAM.
        records = load_records(self.cache_db, self.db)
        self.data = {row[0]:json.loads(row[1]) for row in self.cache_db.execute(
            "SELECT namespace,payload FROM runtime_cache WHERE namespace<>'disclosure_records'")}
        self.data['disclosure_records'] = records
        for namespace in ("observations","events","circuit"):
            self.data.setdefault(namespace,{})

    def save(self, namespaces=None):
        from .disclosure_cache import write_record
        with self.lock:
            keys = tuple(namespaces if namespaces is not None else self.data)
            values = [(key, canonical(self.data[key])) for key in keys if key != 'disclosure_records']
            records = {}
            self.cache_db.execute("BEGIN IMMEDIATE")
            try:
                if 'disclosure_records' in keys:
                    for receipt, record in self.data['disclosure_records'].items():
                        records[receipt] = write_record(self.cache_db, receipt, record)
                self.cache_db.executemany("INSERT INTO runtime_cache VALUES (?,?) ON CONFLICT(namespace) DO UPDATE SET payload=excluded.payload", values)
                self.cache_db.execute("COMMIT")
                self.data['disclosure_records'].update(records)
            except BaseException:
                self.cache_db.execute("ROLLBACK")
                raise

    def save_disclosure(self, receipt, record):
        from .disclosure_cache import write_record
        # The provider hash covers original bytes; this hash covers decoded tool text.
        record = {**record, 'documents': {key: dict(document, content_sha256=hashlib.sha256(
            document['content'].encode('utf-8')).hexdigest()) if 'content' in document else document
            for key, document in record.get('documents', {}).items()}}
        with self.lock:
            return write_record(self.cache_db, receipt, record)

    def disclosure_documents(self, receipt):
        with self.lock:
            row = self.cache_db.execute('SELECT documents FROM disclosure_records WHERE receipt=?', (receipt,)).fetchone()
        return json.loads(row[0]) if row else {}

    def close(self):
        self.cache_db.close()
        self.db.close()

    def known_order(self,namespace,broker_id):
        with self.lock:
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents'").fetchone():
                return None
            row = self.db.execute("SELECT * FROM intents WHERE broker_namespace=? AND broker_id=?",(namespace,broker_id)).fetchone()
            return dict(row) if row else None


class PriorityTransport:
    """One account-wide request budget; trading/quotes precede bulk market reads."""
    def __init__(self, transport, *, minimum_interval_seconds, maximum_queue_seconds):
        self.transport = transport
        self.interval = float(_decimal(minimum_interval_seconds))
        self.maximum_wait = float(_decimal(maximum_queue_seconds))
        if self.interval <= 0 or not 0 < self.maximum_wait <= 5:
            raise HumanRequired("Approved rate/monitor budget is required")
        self.condition = threading.Condition()
        self.queue, self.sequence, self.next_at = [], 0, 0.0
        self.cooldown_until = 0.0
        self.rate_limit_failures = 0
        self.rate_limit_diagnostic = {}
        self.fixture_only = getattr(transport, "fixture_only", False)

    def __call__(self, method, url, headers=None, body=None, timeout=15, *, before_send=None):
        priority = 0 if "/trading/" in url or "inquire-asking-price" in url or "inquire-price?" in url else 1
        with self.condition:
            if time.monotonic() < self.cooldown_until:
                raise AdapterError('RATE_LIMITED', diagnostic={**self.rate_limit_diagnostic,
                    'retry_after_seconds': round(self.cooldown_until-time.monotonic(), 3),
                    'request_stage': 'RATE_COOLDOWN', 'request_sent': False})
            self.sequence += 1
            ticket = (priority, self.sequence)
            self.queue.append(ticket)
            deadline = time.monotonic()+self.maximum_wait
            while min(self.queue) != ticket or time.monotonic() < self.next_at:
                if time.monotonic() < self.cooldown_until:
                    self.queue.remove(ticket)
                    self.condition.notify_all()
                    raise AdapterError('RATE_LIMITED', diagnostic={**self.rate_limit_diagnostic,
                        'retry_after_seconds': round(self.cooldown_until-time.monotonic(), 3),
                        'request_stage': 'RATE_COOLDOWN', 'request_sent': False})
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    self.queue.remove(ticket)
                    self.condition.notify_all()
                    raise AdapterError("MONITOR_DEGRADED_RATE_BUDGET")
                self.condition.wait(min(remaining, max(self.next_at-time.monotonic(), 0.01)))
            self.queue.remove(ticket)
            self.next_at = time.monotonic()+self.interval
            self.condition.notify_all()
        if before_send is not None:
            before_send()
        response = self.transport(method, url, headers, body, timeout)
        try:
            code = response.json().get('msg_cd')
        except (AdapterError, AttributeError):
            code = None
        limited = isinstance(code, str) and code in {'EGW00201', 'EGW00215'}
        if response.status == 429 or limited:
            with self.condition:
                if time.monotonic() - self.cooldown_until >= 60:
                    self.rate_limit_failures = 0
                self.rate_limit_failures = min(self.rate_limit_failures + 1, 5)
                headers_lower = {key.lower(): value for key, value in response.headers.items()}
                retry = headers_lower.get('retry-after', '')
                delay = max(min(5 * 2 ** (self.rate_limit_failures-1), 60),
                            min(int(retry), 86400) if retry.isdigit() else 0)
                self.cooldown_until = max(self.cooldown_until, time.monotonic()+delay)
                self.next_at = max(self.next_at, self.cooldown_until)
                self.rate_limit_diagnostic = {}
                if limited:
                    self.rate_limit_diagnostic['provider_code'] = code
                self.condition.notify_all()
        return response

    request_checked = __call__


class KisBrokerPort:
    def __init__(self, adapter, manifest, state, *, clock=utcnow):
        self.adapter, self.manifest, self.state, self.clock = adapter, manifest, state, clock
        self.environment = "live" if adapter.environment == "real" else "demo"
        self.store = None
        self.latest_bundle = None
        self.snapshot_lock = threading.RLock()
        self.snapshot_condition = threading.Condition(self.snapshot_lock)
        self.snapshot_inflight = None
        self.snapshot_cache = None
        self.snapshot_generation = 0

    def _invalidate_snapshot(self):
        with self.snapshot_lock:
            self.snapshot_generation += 1
            self.snapshot_cache = None

    def bind_store(self,store):
        self.store = store

    def _namespace(self, session_date, venue="KRX"):
        return f"{self.adapter.environment}:{self.manifest['account_alias']}:{session_date}:{venue}"

    def submit(self, intent):
        self._invalidate_snapshot()
        instrument = intent["instrument_id"]
        ticker = instrument.removeprefix("KRX:")
        quote = self.latest_bundle.quotes.get(instrument) if self.latest_bundle else None
        session = self.latest_bundle.calendar.active(self.clock()) if self.latest_bundle else None
        if quote is None or session is None:
            return {"status":"NOT_SENT","reason":"ORDER_MARKET_VALIDITY_UNAVAILABLE"}
        deadline = min(quote.observed_at + timedelta(seconds=5), session.closes_at)
        if intent.get("expires_at"):
            deadline = min(deadline, aware_time(intent["expires_at"]))
        if intent["side"] == "BUY":
            field = None
            try:
                power = self.adapter.read_buying_power(ticker, intent["limit_price"])
                field = 'nrcvb_buy_qty'
                if _quantity(power[field]) < intent["quantity"]:
                    return {"status":"NOT_SENT", "reason":"NO_MARGIN_BUYING_POWER_EXCEEDED"}
                field = 'nrcvb_buy_amt'
                if _decimal(power[field]) < intent["quantity"] * _decimal(intent["limit_price"]):
                    return {"status":"NOT_SENT", "reason":"NO_MARGIN_BUYING_POWER_EXCEEDED"}
            except (AdapterError, ValueError, KeyError, TypeError, ArithmeticError) as error:
                return {"status":"NOT_SENT", "reason":"NO_MARGIN_BUYING_POWER_UNVERIFIED",
                        "diagnostics": [_failure_diagnostic(error, 'inquire-psbl-order',
                            reason='NO_MARGIN_BUYING_POWER_UNVERIFIED', field=field)]}
        result = self.adapter.submit(ticker, intent["side"], intent["quantity"], limit_price=intent["limit_price"], valid_until=deadline)
        if result.status != "ACKNOWLEDGED":
            return {"status": result.status, "reason": result.code,
                    'diagnostics': [{'reason': result.code, **result.diagnostic}] if result.diagnostic else []}
        namespace = self._namespace(self.clock().astimezone(SEOUL).date().isoformat())
        if not result.organization:
            return {"status": "UNKNOWN", "reason": "ORDER_ORGANIZATION_UNVERIFIED"}
        return {"status": "ACKNOWLEDGED", "broker_id": result.order_id, "namespace": namespace,
                "metadata":{"organization":result.organization}}

    def cancel(self, request):
        self._invalidate_snapshot()
        known = self.state.known_order(request["namespace"],request["broker_id"])
        if known is None:
            raise HumanRequired("Order organization/ownership requires reconciliation")
        quantity = _quantity(request["remaining_quantity"])
        if not quantity:
            return {"status": "NO_REMAINING_QUANTITY"}
        metadata = request.get("metadata") or json.loads(known.get("broker_metadata") or "{}")
        if not metadata.get("organization"):
            raise HumanRequired("Broker organization metadata is unverified")
        try:
            result = self.adapter.read_cancelable_orders()
            if result.quality != 'COMPLETE':
                error = AdapterError(result.metadata.get('error', 'BROKER_PAGINATION_INCOMPLETE'), diagnostic=result.metadata)
                return {'status':'NOT_SENT', 'reason':'CANCELABLE_ORDER_UNVERIFIED',
                        'diagnostics':[_failure_diagnostic(error, 'inquire-psbl-rvsecncl', reason='CANCELABLE_ORDER_UNVERIFIED')]}
            matches = [row for row in result.records if str(row.get("odno")) == request["broker_id"]
                       and str(row.get("ord_gno_brno")) == metadata["organization"]]
            if len(matches) != 1:
                return {"status":"NOT_SENT", "reason":"CANCELABLE_ORDER_UNVERIFIED"}
            possible = _quantity(matches[0]["psbl_qty"])
            if possible < quantity:
                return {"status":"NOT_SENT", "reason":"CANCELABLE_QUANTITY_CHANGED"}
        except (AdapterError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            return {"status":"NOT_SENT", "reason":"CANCELABLE_ORDER_UNVERIFIED",
                    "diagnostics": [_failure_diagnostic(error, 'inquire-psbl-rvsecncl', reason='CANCELABLE_ORDER_UNVERIFIED')]}
        result = self.adapter.cancel(request["broker_id"], metadata["organization"], quantity,
                                     order_type="00" if known["side"] == "BUY" else "01")
        return {"status": result.status, "reason": result.code}

    def _supplements(self):
        reference = self.manifest["bootstrap"].get("settled_observations_path")
        if reference is None:
            return {}
        value = _json(reference)
        if value.get("account_alias") != self.manifest["account_alias"] or value.get("environment") != self.adapter.environment or value.get("verified") is not True:
            raise HumanRequired("Settlement evidence scope/verification mismatch")
        return value["orders"]

    @staticmethod
    def _order_status(record, quantity, cumulative, *, strict=False, expired=False):
        aliases = [_quantity(record[key]) for key in ("cncl_cfrm_qty", "cnc_cfrm_qty") if key in record]
        if not aliases or len(set(aliases)) != 1:
            raise ValueError("CANCEL_QUANTITY_UNVERIFIED")
        canceled = aliases[0]
        rejected = _quantity(record["rjct_qty"]) if strict or "rjct_qty" in record else 0
        remaining = _quantity(record["rmn_qty"]) if strict or "rmn_qty" in record else quantity-cumulative-canceled-rejected
        accounted = cumulative+canceled+rejected+remaining
        expired_remainder = expired and remaining == 0 and accounted < quantity
        if quantity <= 0 or min(cumulative, canceled, rejected, remaining) < 0 or accounted != quantity and not expired_remainder:
            raise ValueError("CONFLICTING_ORDER_QUANTITIES")
        canceled_flag = record.get("cncl_yn", "")
        if canceled_flag not in {"", "Y", "N"} or canceled_flag == "Y" and not canceled:
            raise ValueError("CONFLICTING_ORDER_CANCELLATION")
        if expired_remainder:
            return "EXPIRED"
        return ("FILLED" if cumulative == quantity else "PARTIAL_CANCELED" if not remaining and cumulative
                else "CANCELED" if not remaining and canceled else "REJECTED" if not remaining and rejected
                else "PARTIALLY_FILLED" if cumulative else "ACKNOWLEDGED")

    @staticmethod
    def order_fingerprint(order):
        return digest({key: order[key] for key in ("namespace", "broker_id", "instrument_id", "side", "quantity",
                       "cumulative_quantity", "cumulative_notional", "state", "metadata")})

    @staticmethod
    def _active_reservation(record, today):
        # A blank end date is a one-day reservation, not a perpetual order.
        # KIS documents a maximum 30-day lifetime for period reservations.
        day = date.fromisoformat(record["rsvn_ord_ord_dt"])
        raw_end = record.get("rsvn_end_dt", "")
        end = date.fromisoformat(raw_end) if raw_end not in {"", "00000000"} else day
        canceled = record.get("cncl_ord_dt", "")
        if canceled not in {"", "00000000"}:
            date.fromisoformat(canceled)
            return False
        if end < today:
            return False
        quantity, filled = _quantity(record["ord_rsvn_qty"]), _quantity(record["tot_ccld_qty"])
        if not quantity or filled > quantity:
            raise ValueError("RESERVATION_QUANTITIES_UNVERIFIED")
        if quantity == filled:
            return False
        # A one-day reservation with a returned regular order number has been
        # converted; its working quantity is reconciled in the ordinary census.
        number = str(record.get("odno", "")).strip()
        if end == day and day <= today and number.isdigit() and int(number):
            return False
        return True

    def census(self):
        """Read the whole domestic cash account; no ownership or historical trades are invented."""
        now, bootstrap = self.clock(), self.manifest["bootstrap"]
        today = now.astimezone(SEOUL).date()
        start = max(date.fromisoformat(bootstrap.get("orders_since") or today.isoformat()), today-timedelta(days=80))
        try:
            orders = self.adapter.read_orders(start, today)
            cancelable = self.adapter.read_cancelable_orders()
            reservations = self.adapter.read_reservations(today-timedelta(days=31), today+timedelta(days=31))
            try:
                costs = self.adapter.read_daily_costs(start, today)
            except AdapterError:
                costs = None
            # Read balances last so executions observed above are reflected in positions/cash.
            account = self.adapter.read_account()
            failures = [{"endpoint": name, "quality": result.quality,
                         "reason": result.metadata.get("error", "BROKER_PAGINATION_INCOMPLETE"),
                        **{key: result.metadata[key] for key in DIAGNOSTIC_FIELDS if key in result.metadata}}
                        for name, result in (("balance", account), ("orders", orders),
                                             ("cancelable", cancelable), ("reservations", reservations))
                        if result.quality != "COMPLETE"]
            if failures:
                return {"complete": False, "errors": sorted({item['reason'] for item in failures}),
                        "diagnostics": failures, "orders": [], "reservations": []}
            fields = ("prvs_rcdl_excc_amt", "tot_evlu_amt", "evlu_amt_smtl_amt", "nass_amt", "tot_loan_amt", "cma_evlu_amt")
            summaries = []
            for page in account.metadata["summaries"]:
                if not isinstance(page, list) or len(page) != 1:
                    raise ValueError("ACCOUNT_SUMMARY_UNVERIFIED")
                summaries.append({key: str(_decimal(page[0][key])) for key in fields})
            if not summaries or any(value != summaries[0] for value in summaries):
                raise ValueError("ACCOUNT_SUMMARY_CHANGED_DURING_PAGINATION")
            summary = summaries[0]
            cash, nav, valuation = (_decimal(summary[key]) for key in fields[:3])
            if nav != cash + valuation or _decimal(summary["nass_amt"]) != nav:
                raise ValueError("ACCOUNT_NAV_RECONCILIATION_FAILED")
            if _decimal(summary["tot_loan_amt"]) or _decimal(summary["cma_evlu_amt"]):
                raise ValueError("NON_CASH_ACCOUNT_ASSETS_UNSUPPORTED")
            quantities, sellable, prices, values = {}, {}, {}, {}
            for record in account.records:
                instrument = "KRX:" + str(record["pdno"])
                if not re.fullmatch(r"KRX:[A-Z0-9]{6}", instrument) or instrument in quantities:
                    raise ValueError("DUPLICATE_OR_INVALID_ACCOUNT_POSITION")
                quantities[instrument], sellable[instrument] = _quantity(record["hldg_qty"]), _quantity(record["ord_psbl_qty"])
                prices[instrument], values[instrument] = str(_decimal(record["prpr"])), _decimal(record["evlu_amt"])
                if sellable[instrument] > quantities[instrument] or quantities[instrument] and _decimal(prices[instrument]) <= 0:
                    raise ValueError("INVALID_ACCOUNT_POSITION")
                if _decimal(record.get("loan_amt", "0")):
                    raise ValueError("NON_CASH_ACCOUNT_ASSETS_UNSUPPORTED")
            if sum(values.values(), Decimal(0)) != valuation:
                raise ValueError("ACCOUNT_VALUATION_RECONCILIATION_FAILED")
            active = {(str(row["ord_gno_brno"]), str(row["odno"])) for row in cancelable.records if _quantity(row["psbl_qty"])}
            normalized, records_by_key = [], {}
            for record in orders.records:
                day = date.fromisoformat(str(record["ord_dt"]))
                venues = {record[key] for key in ("excg_id_dvsn_cd", "excg_id_dvsn_Cd") if record.get(key)}
                if len(venues) != 1 or not venues <= {"KRX", "NXT", "SOR"} or day > today:
                    raise ValueError("ORDER_VENUE_OR_DATE_UNVERIFIED")
                venue = venues.pop()
                quantity, cumulative = _quantity(record["ord_qty"]), _quantity(record["tot_ccld_qty"])
                notional = _decimal(record["tot_ccld_amt"])
                if bool(cumulative) != bool(notional):
                    raise ValueError("INVALID_CUMULATIVE_OBSERVATION")
                organization, broker_id = str(record["ord_gno_brno"]), str(record["odno"])
                if not organization.isdigit() or not broker_id.isdigit():
                    raise ValueError("ORDER_IDENTITY_UNVERIFIED")
                expired = (day < today
                        and record.get("ord_dvsn_cd") in {"00", "01"} and (organization, broker_id) not in active
                        and (not record.get("rsvn_ord_end_dt") or str(record["rsvn_ord_end_dt"]) in {"00000000", ""}
                             or date.fromisoformat(str(record["rsvn_ord_end_dt"])) < today))
                status = self._order_status(record, quantity, cumulative, strict=True, expired=expired)
                if expired and status in {"ACKNOWLEDGED", "PARTIALLY_FILLED"}:
                    status = "EXPIRED"
                item = {"namespace": self._namespace(day.isoformat(), venue), "broker_id": broker_id,
                        "instrument_id": "KRX:"+str(record["pdno"]), "side": {"01":"SELL", "02":"BUY"}[record["sll_buy_dvsn_cd"]],
                        "quantity": quantity, "cumulative_quantity": cumulative, "cumulative_notional": str(notional),
                        "state": status, "metadata": {"organization": organization, "original_order_id": str(record.get("orgn_odno", "")),
                                                       "venue": venue, "order_type": record.get("ord_dvsn_cd")},
                        "observed_at": orders.retrieved_at.isoformat(), "cumulative_fees": None if cumulative else "0",
                        "first_fill_at": None, "fill_time_quality": "FIRST_OBSERVED" if cumulative else "UNKNOWN",
                        "fill_session_id": day.isoformat()}
                item["key"] = item["namespace"]+":"+broker_id
                record_hash = digest(record)
                if item["key"] in records_by_key:
                    # Repeated history rows must agree in every provider field,
                    # including fields not used by our normalization.
                    if records_by_key[item["key"]] != record_hash:
                        raise ValueError("DUPLICATE_BROKER_ORDER")
                    continue
                records_by_key[item["key"]] = record_hash
                item["fingerprint"] = self.order_fingerprint(item)
                item["terminal"] = status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "PARTIAL_CANCELED"}
                normalized.append(item)
            # An active cancelable order missing from the history is incomplete evidence.
            if not active <= {(row["metadata"]["organization"], row["broker_id"]) for row in normalized}:
                raise ValueError("ACTIVE_ORDER_MISSING_FROM_HISTORY")
            daily_costs, cost_quality = {}, "UNCONFIRMED"
            try:
                if costs is not None and costs.quality == "COMPLETE":
                    for record in costs.records:
                        day = date.fromisoformat(str(record["trad_dt"])).isoformat()
                        if day in daily_costs or not start <= date.fromisoformat(day) <= today:
                            raise ValueError("DUPLICATE_OR_INVALID_COST_DAY")
                        daily_costs[day] = {key: str(_decimal(record[key])) for key in ("buy_amt", "sll_amt", "fee", "tl_tax", "loan_int")}
                    cost_quality = "BROKER_REPORTED"
            except (AdapterError, ValueError, KeyError, TypeError, ArithmeticError):
                daily_costs = {}
            return {"complete": True, "errors": [], "quantities": quantities, "sellable_quantities": sellable,
                    "prices": prices, "economic_cash": str(cash), "account_nav": str(nav),
                    "account_observed_at": account.retrieved_at.isoformat(), "daily_costs": daily_costs,
                    "cost_quality": cost_quality, "orders": normalized,
                    "reservations": [row for row in reservations.records if self._active_reservation(row, today)]}
        except (AdapterError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            return {"complete": False, "errors": [getattr(error, "code", str(error) if isinstance(error, ValueError) else "PROVIDER_FIELD_UNVERIFIED")],
                    "diagnostics": [_failure_diagnostic(error, 'account_normalization')],
                    "orders": [], "reservations": []}

    def _whole_snapshot(self):
        census = self.census()
        if not census["complete"]:
            return {**census, "ownership_complete": False}
        bootstrap, normalized, errors = self.manifest["bootstrap"], [], []
        if bootstrap.get("external_quantities") or bootstrap.get("external_order_keys"):
            errors.append("WHOLE_ACCOUNT_OWNERSHIP_CONFLICT")
        baseline = bootstrap.get("baseline_orders", {})
        for item in census["orders"]:
            known = self.state.known_order(item["namespace"], item["broker_id"])
            if known is None:
                if item["state"] in {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "PARTIAL_CANCELED"} and baseline.get(item["key"]) == item["fingerprint"]:
                    continue
                errors.append("UNALLOCATED_BROKER_ORDER")
                continue
            if json.loads(known.get("broker_metadata") or "{}").get("organization") != item["metadata"]["organization"]:
                errors.append("ORDER_ORGANIZATION_MISMATCH")
                continue
            with self.state.lock:
                revision = self.state.data["observations"].setdefault(item["key"], {"revision": 0, "last_observation_hash": None})
                if revision["last_observation_hash"] != item["fingerprint"]:
                    revision.update(revision=revision["revision"]+1, last_observation_hash=item["fingerprint"])
                    self.state.save(("observations",))
                item["revision"] = revision["revision"]
            normalized.append(item)
        if census["reservations"]:
            errors.append("ACTIVE_RESERVATION_ORDER")
        available, field, diagnostics = Decimal(0), None, []
        try:
            fields = self.manifest.get("normalization", {}).get("account", {})
            power = self.adapter.read_buying_power(fields.get("resource_symbol", "005930"), fields.get("resource_price", "1"))
            field = 'nrcvb_buy_amt'
            available = min(_decimal(census["economic_cash"]), _decimal(power["nrcvb_buy_amt"]))
        except (AdapterError, ValueError, KeyError, TypeError, ArithmeticError) as error:
            errors.append("NO_MARGIN_BUYING_POWER_UNVERIFIED")
            diagnostics.append(_failure_diagnostic(error, 'inquire-psbl-order',
                reason='NO_MARGIN_BUYING_POWER_UNVERIFIED', field=field))
        return {"complete": not errors, "ownership_complete": not errors, "errors": errors,
                "diagnostics": diagnostics + [reason for reason in errors if reason != 'NO_MARGIN_BUYING_POWER_UNVERIFIED'],
                "orders": normalized, "strategy_quantities": census["quantities"], "strategy_sellable_quantities": census["sellable_quantities"],
                "broker_available_cash": str(available), "broker_cash_reserves_orders": not errors, "whole_account": True,
                "account_cash": {"cash_krw": census["economic_cash"], "observed_at": census["account_observed_at"],
                                 "source": "KIS:inquire-balance:prvs_rcdl_excc_amt", "daily_costs": census["daily_costs"],
                                 "cost_quality": census["cost_quality"]}}

    def snapshot(self, *, maximum_age_seconds=0):
        # Coalesce concurrent readers without holding a cache or ledger lock
        # during provider I/O. Order invalidation prevents reusing that flight.
        while True:
            ledger_version = self.store.get('account_version') if self.store else None
            working = bool(self.store and self.store.working())
            observed_at, refresh_order = self.clock(), time.monotonic_ns()
            with self.snapshot_condition:
                cached = self.snapshot_cache
                if (maximum_age_seconds > 0 and cached and not working and
                        0 <= (observed_at-aware_time(cached['observed_at'])).total_seconds() < maximum_age_seconds and
                        cached.get('ledger_version') == ledger_version and
                        not any(row['state'] not in {'FILLED','CANCELED','REJECTED','EXPIRED','PARTIAL_CANCELED'} for row in cached.get('orders', []))):
                    return cached
                flight = self.snapshot_inflight
                if flight is None:
                    generation = self.snapshot_generation
                    flight = self.snapshot_inflight = {'generation': generation, 'ledger_version': ledger_version, 'done': False}
                    break
                while not flight['done']:
                    self.snapshot_condition.wait()
                shared = (flight if flight['generation'] == self.snapshot_generation
                          and flight['ledger_version'] == ledger_version else None)
            current_version = self.store.get('account_version') if self.store else None
            if shared is not None and current_version == ledger_version:
                if 'error' in shared:
                    raise shared['error']
                return shared['value']
        try:
            value = self._snapshot()
            value.update(observed_at=observed_at.isoformat(), refresh_order=refresh_order, ledger_version=ledger_version)
            current_version = self.store.get('account_version') if self.store else None
            with self.snapshot_condition:
                flight['value'] = value
                self.snapshot_cache = (value if generation == self.snapshot_generation and current_version == ledger_version
                    and value.get('complete') and value.get('ownership_complete') else None)
            return value
        except BaseException as error:
            with self.snapshot_condition:
                flight['error'] = error
            raise
        finally:
            with self.snapshot_condition:
                flight['done'] = True
                self.snapshot_inflight = None
                self.snapshot_condition.notify_all()

    def _snapshot(self):
        if self.manifest["bootstrap"].get("whole_account") is True:
            return self._whole_snapshot()
        now = self.clock()
        mapping = self.manifest["normalization"]
        # The buying-power query must follow the orders it is claimed to cover.
        orders = self.adapter.read_orders(date.fromisoformat(self.manifest["bootstrap"]["orders_since"]), now.astimezone(SEOUL).date())
        account = self.adapter.read_account(resource_symbol=mapping["account"]["resource_symbol"],
                                            resource_price=mapping["account"]["resource_price"])
        errors, normalized = [], []
        if account.quality != "COMPLETE" or orders.quality != "COMPLETE":
            failures = [{'endpoint': name, 'quality': result.quality,
                         'reason': result.metadata.get('error', 'BROKER_PAGINATION_INCOMPLETE'),
                         **{key: result.metadata[key] for key in DIAGNOSTIC_FIELDS if key in result.metadata}}
                        for name, result in (('balance', account), ('orders', orders)) if result.quality != 'COMPLETE']
            return {"complete": False, "ownership_complete": False, "orders": [],
                    "errors": sorted({item['reason'] for item in failures}), 'diagnostics': failures}
        try:
            resources = _decimal(_field(account.metadata["orderable_resources"], mapping["account"]["available_cash"]))
            actual = {}
            sellable = {}
            for record in account.records:
                instrument = "KRX:"+str(_field(record,mapping["account"]["symbol"]))
                if instrument in actual:
                    raise ValueError("DUPLICATE_ACCOUNT_POSITION")
                actual[instrument] = _quantity(_field(record,mapping["account"]["quantity"]))
                sellable[instrument] = _quantity(_field(record,mapping["account"]["sellable_quantity"]))
            supplements = self._supplements()
            external = {key:_quantity(value) for key,value in self.manifest["bootstrap"]["external_quantities"].items()}
            strategy = {key:quantity-external.get(key,0) for key,quantity in actual.items()}
            if any(value < 0 for value in strategy.values()) or any(actual.get(key,0) < value for key,value in external.items()):
                raise ValueError("OWNERSHIP_CHANGED_OUTSIDE_STRATEGY")
            owned_symbols = set(self.manifest["bootstrap"]["strategy_quantities"])
            if self.state.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents'").fetchone():
                owned_symbols.update(row[0] for row in self.state.db.execute("SELECT DISTINCT instrument_id FROM intents"))
            ownership_complete = not any(quantity and instrument not in owned_symbols for instrument,quantity in strategy.items())
            for record in orders.records:
                fields = mapping["orders"]
                session_date = date.fromisoformat(str(_field(record,fields["session_date"]))).isoformat()
                namespace = self._namespace(session_date)
                broker_id = str(_field(record,fields["broker_id"]))
                key = namespace+":"+broker_id
                quantity = _quantity(_field(record,fields["quantity"]))
                cumulative = _quantity(_field(record,fields["cumulative_quantity"]))
                notional = _decimal(_field(record,fields["cumulative_notional"]))
                if cumulative > quantity or (not cumulative and notional):
                    raise ValueError("INVALID_CUMULATIVE_OBSERVATION")
                known = self.state.known_order(namespace,broker_id)
                if known is None:
                    if key in self.manifest["bootstrap"]["external_order_keys"]:
                        continue
                    errors.append("UNALLOCATED_BROKER_ORDER")
                    continue
                supplement = supplements.get(key)
                if cumulative:
                    fees,first_fill,observed,fill_quality = None,None,orders.retrieved_at,"FIRST_OBSERVED"
                    if supplement:
                        if (supplement.get("verified") is not True or not supplement.get("source") or
                                not supplement.get("source_sha256") or _quantity(supplement["cumulative_quantity"]) != cumulative or
                                _decimal(supplement["cumulative_notional"]) != notional):
                            raise ValueError("SETTLEMENT_EVIDENCE_MISMATCH")
                        if supplement.get("actual_cumulative_fees") is not None:
                            fees = _decimal(supplement["actual_cumulative_fees"])
                        if supplement.get("first_fill_at") is not None:
                            first_fill = aware_time(supplement["first_fill_at"])
                            fill_quality = "EXACT"
                        observed = aware_time(supplement["observed_at"])
                        if observed > now or first_fill is not None and (first_fill > observed or
                                fields["day_order_fill_session_verified"] and first_fill.astimezone(SEOUL).date().isoformat() != session_date):
                            raise ValueError("INVALID_FILL_TIMESTAMPS")
                else:
                    # No executions have occurred. The approved fee contract is trade-based.
                    fees, first_fill, observed, fill_quality = Decimal(0), None, orders.retrieved_at,"UNKNOWN"
                side = fields["side_codes"][str(_field(record,fields["side"]))]
                status = self._order_status(record, quantity, cumulative)
                item = {"broker_id":broker_id,"namespace":namespace,"instrument_id":"KRX:"+str(_field(record,fields["symbol"])),
                        "side":side,"quantity":quantity,"cumulative_quantity":cumulative,"cumulative_notional":str(notional),
                        "cumulative_fees":str(fees) if fees is not None else None,"observed_at":observed.isoformat(),"first_fill_at":first_fill.isoformat() if first_fill else None,
                        "fill_time_quality":fill_quality,"fill_session_id":session_date if fields["day_order_fill_session_verified"] else None,
                        "state":status,"correction": bool(supplement and supplement.get("correction") is True)}
                with self.state.lock:
                    revision = self.state.data["observations"].setdefault(key,{"revision":0,"last_observation_hash":None})
                    fingerprint = digest({key:value for key,value in item.items() if key != "observed_at"})
                    if fingerprint != revision.get("last_observation_hash"):
                        revision["revision"] += 1
                        revision["last_observation_hash"] = fingerprint
                    item["revision"] = revision["revision"]
                    self.state.save(("observations",))
                normalized.append(item)
            return {"complete": not errors,"ownership_complete": not errors and ownership_complete,"strategy_quantities":strategy,
                    "strategy_sellable_quantities": {key:min(quantity,sellable.get(key,0)) for key,quantity in strategy.items()},
                    "orders":normalized,"errors":errors,"broker_available_cash":str(resources),
                    "broker_cash_reserves_orders": not errors and ownership_complete and mapping["account"]["available_cash"] == "nrcvb_buy_amt"}
        except (ValueError,KeyError,TypeError,AdapterError) as error:
            return {"complete":False,"ownership_complete":False,"orders":normalized,
                    "diagnostics": [_failure_diagnostic(error, 'account_normalization')],
                    "errors":[getattr(error,"code",str(error) if isinstance(error,ValueError) else "PROVIDER_FIELD_UNVERIFIED")]}


class ExternalRuntime:
    def __init__(self, config, approval, manifest, kis, dart, codex, state, *, clock=utcnow):
        self.config,self.approval,self.manifest = config,approval,manifest
        self.kis,self.dart,self.codex,self.state,self.clock = kis,dart,codex,state,clock
        self.profile = config.research if config.mode != "live" else config.data["strategy"]["strategy"]["live_mandate"]["accepted_risk_policy"]
        self.calendar = SessionCalendar([Session.model_validate_json(canonical(row)) for row in manifest["calendar"]["sessions"]],
                                       provenance=manifest["calendar"]["source"],verified=True,synthetic=False)
        self.broker = KisBrokerPort(kis,manifest,state,clock=clock)
        self.daily_cache = None
        self.disclosure_cache = None
        self.disclosure_diagnostics = []
        self.latest_bundle = None
        self.quote_depth = {}
        self.collect_lock = threading.RLock()
        self.publish_lock = threading.RLock()
        self.quote_subscription_lock = threading.RLock()
        self.entry_quote_symbols = set()
        self.instrument_names = {}
        self.verified_models = {config.app['model']['model_id']}
        self.model_probe_lock = threading.Lock()

    def _model_for_call(self):
        if not self.config.model_reload_baseline:
            return self.codex
        settings = self.config.model_settings()
        if not settings['model_id'] or not settings['reasoning_effort']:
            raise AdapterError('MODEL_CONFIGURATION_UNSET')
        # Each call owns its selection; another chat/review or config edit cannot mutate it.
        adapter = copy(self.codex)
        adapter.model_id, adapter.reasoning_effort = settings['model_id'], settings['reasoning_effort']
        adapter.timeout = settings['timeout_seconds']
        adapter.run_budget_seconds = (adapter.timeout * (1 + settings['transient_retries'] + settings['schema_repair_attempts'])
            + settings['retry_delay_seconds'] * settings['transient_retries'] + 30)
        with self.model_probe_lock:
            if adapter.model_id not in self.verified_models:
                from .adapters.isolation_probe import probe
                try:
                    probe(adapter.model_id)
                except AssertionError:
                    raise AdapterError('MODEL_ISOLATION_PROBE_FAILED') from None
                self.verified_models.add(adapter.model_id)
        expected = (adapter.model_id, adapter.reasoning_effort, adapter.auth_mode)
        def authorize(_operation, model, effort, auth_mode):
            self.config.assert_current()
            self.config.require_external('model_call', self.approval)
            if (model, effort, auth_mode) != expected:
                raise HumanRequired('Runtime model identity changed')
        adapter.authorize = authorize
        return adapter

    def _instruments(self, now):
        mapping = self.manifest["normalization"]["instruments"]
        instruments, diagnostics = [], []
        for board in self.profile["universe"]["boards"]:
            result = self.kis.read_instruments(board)
            if result.quality != "COMPLETE":
                raise HumanRequired("FULL_UNIVERSE_COLLECTION_INCOMPLETE")
            for row in result.records:
                ticker = row["symbol"]
                if mapping.get("normalizer") == "kis_master_field_codes_v1":
                    from .deployment_sources import normalize_master_flags
                    flags = normalize_master_flags(row)
                    kind, status, codes_known = flags["kind"], flags["status"], flags["codes_known"]
                else:
                    flags = [row.get(key) for key in ("etp","spac","halted","liquidation","managed","preferred")]
                    codes_known = all(value in mapping["true_codes"]+mapping["false_codes"] for value in flags)
                    true = lambda key: row.get(key) in mapping["true_codes"]
                    kind = "common_stock" if row.get("group") in mapping["common_groups"] and not true("etp") and not true("spac") and not true("preferred") else "excluded_instrument"
                    status = "UNKNOWN" if not codes_known else "HALTED" if true("halted") else "DELISTING" if true("liquidation") else "ADMINISTRATIVE" if true("managed") else "NORMAL"
                issuer = mapping["issuer_by_symbol"].get(ticker)
                if isinstance(row.get("name"), str) and row["name"].strip():
                    self.instrument_names["KRX:" + ticker] = row["name"].strip()
                sector = mapping["sector_by_industry"].get(row.get("industry"))
                instruments.append(Instrument(instrument_id="KRX:"+ticker,issuer_id=issuer or "UNVERIFIED:"+ticker,
                    board=board,kind=kind,venue="KRX",sector=sector,status=status,status_verified=codes_known and bool(issuer),
                    classification_source=mapping["source"],effective_at=result.retrieved_at))
                if kind != "common_stock" or status != "NORMAL" or not issuer or not sector:
                    diagnostics.append({"instrument_id":"KRX:"+ticker,"reason":"UNIVERSE_STATUS_OR_CLASSIFICATION_EXCLUDED"})
        return instruments,diagnostics

    def _bars(self, result, *, index=False, expected=None):
        basis = self.manifest["normalization"]["bars"]
        if self.manifest.get("automatic"):
            from .deployment_sources import validate_bar_observations
            observed = validate_bar_observations(result, expected, index=index)
            basis = {**basis, **observed}
        rows = []
        for raw in result.records:
            session_id = date.fromisoformat(str(raw["stck_bsop_date"])).isoformat()
            session = self.calendar.session(session_id)
            from .deployment_sources import daily_bar_available_at
            if daily_bar_available_at(session) > result.retrieved_at:
                continue
            names = ("bstp_nmix_hgpr","bstp_nmix_lwpr","bstp_nmix_prpr") if index else ("stck_hgpr","stck_lwpr","stck_clpr")
            rows.append(DailyBar(session_id=session_id,opens_at=session.opens_at,closes_at=session.closes_at,
                available_at=result.retrieved_at,high=_decimal(raw[names[0]]),low=_decimal(raw[names[1]]),close=_decimal(raw[names[2]]),
                turnover=_decimal(raw["acml_tr_pbmn"]),complete=True,adjustment_basis=basis["index_basis"] if index else basis["stock_basis"],
                ohlc_consistently_adjusted=basis["consistent_ohlc_verified"],source=basis["source"]))
        return sorted(rows,key=lambda bar:bar.closes_at)

    def _quote(self,instrument,*,depth=None,maximum_age_seconds=None):
        if maximum_age_seconds is None:
            maximum_age_seconds = self.profile["orders"]["quote_max_age_seconds"]
        fields = self.manifest["normalization"]["quote"]
        streaming = fields.get("transport", "rest") == "websocket"
        read = self.kis.stream_quote if streaming else self.kis.quote
        ticker = instrument.instrument_id.removeprefix("KRX:")
        polled = False
        try:
            result = read(ticker)
        except AdapterError as error:
            if not streaming or error.code != "STREAM_QUOTE_STALE":
                raise
            result = self.kis.poll_quote(ticker)
            polled = True
            streaming = False
            fields = {"session_date": "session_date", "observed_time": "asking.aspr_acpt_hour",
                      "bid": "asking.bidp1", "ask": "asking.askp1",
                      "bid_quantity": "asking.bidp_rsqn1", "ask_quantity": "asking.askp_rsqn1",
                      "source": result.metadata["source"]}
            # A fresh tick may arrive while REST is in flight; do not discard it
            # merely because that REST book still carries an older provider time.
            try:
                current = read(ticker)
            except AdapterError as error:
                if error.code != 'STREAM_QUOTE_STALE':
                    raise
            else:
                result, polled, streaming = current, False, True
                fields = self.manifest['normalization']['quote']
        if result.quality != "COMPLETE" or len(result.records) != 1:
            raise ValueError("QUOTE_FETCH_INCOMPLETE")
        raw = result.records[0]
        if result.metadata.get('tr_id') == 'H0STASP0':
            fields = {**fields,'observed_time':'BSOP_HOUR','source':'KIS:H0STASP0'}
        if streaming and raw.get("MARKET_CLS_CODE") != "2":
            raise ValueError("QUOTE_MARKET_IS_NOT_REGULAR")
        observed = _timestamp(_field(raw,fields["session_date"]),_field(raw,fields["observed_time"]))
        session = self.calendar.session(observed.date().isoformat())
        if not session.opens_at <= observed < session.closes_at:
            raise ValueError("QUOTE_OBSERVATION_OUTSIDE_SESSION")
        (self.quote_depth if depth is None else depth)[instrument.instrument_id] = {
            "bid":_quantity(_field(raw,fields["bid_quantity"])) if fields.get("bid_quantity") else 0,
            "ask":_quantity(_field(raw,fields["ask_quantity"])) if fields.get("ask_quantity") else 0}
        quote = Quote(instrument_id=instrument.instrument_id,venue="KRX",observed_at=observed,received_at=result.retrieved_at,
                     bid=_decimal(_field(raw,fields["bid"])),ask=_decimal(_field(raw,fields["ask"])),source=fields["source"])
        if polled and not quote_fresh(quote,self.clock(),maximum_age_seconds):
            raise ValueError("STALE_QUOTE")
        return quote

    def _subscribe_quotes(self, account, candidates=None):
        if self.manifest["normalization"]["quote"].get("transport", "rest") != "websocket":
            return set()
        with self.quote_subscription_lock:
            if candidates is not None:
                self.entry_quote_symbols = set(candidates)
            protected = self._protection_symbols(account)
            targets = protected | self.entry_quote_symbols
            excluded = set()
            # One app key supports 41 registrations. Preserve protection rather
            # than inventing a strategy ranking to discard excess candidates.
            if len(targets) > 41:
                excluded = targets - protected
                targets = protected
            if len(targets) > 41:
                excluded |= targets
                targets = set()
            if self.calendar.active(self.clock()) is None:
                targets = set()
            self.kis.subscribe_quotes(sorted(value.removeprefix("KRX:") for value in targets))
            return excluded

    def _wait_for_stream_quotes(self, instruments):
        if (not instruments or self.calendar.active(self.clock()) is None or
                self.manifest["normalization"]["quote"].get("transport", "rest") != "websocket"):
            return
        pending = {item.instrument_id.removeprefix("KRX:") for item in instruments}
        deadline = time.monotonic() + 30
        # Connection/ACK waiting is independent of the 5-second quote age limit.
        # Only full collection waits for initial ticks. Protection reads the
        # background cache immediately and never waits for a connection.
        while pending:
            for ticker in tuple(pending):
                try:
                    self.kis.stream_quote(ticker)
                    pending.remove(ticker)
                except AdapterError as error:
                    if isinstance(error, HumanRequired) or error.code in AUTH_ERRORS:
                        raise
                    if error.code == 'STREAM_QUOTE_STALE':
                        # The live session date is known; _quote can refresh the REST book now.
                        pending.remove(ticker)
            if not pending or time.monotonic() >= deadline:
                return
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def close(self):
        self.kis.close()
        self.state.close()

    def _official_ir_documents(self, receipt, record, now):
        from .adapters.disclosures import linked_official_ir_urls

        domains = self.config.app['market']['official_ir_domains']
        if not domains:
            return record
        scope_hash = digest({'version': 1, 'domains': sorted(domains)})
        if record.get('official_ir_scope') == scope_hash and (record.get('official_ir_complete') or
                (now - aware_time(record['official_ir_checked_at'])).total_seconds() < 180):
            return record
        documents = self.state.disclosure_documents(receipt)
        originals = [item for item in documents.values() if item.get('interpretation_status') == 'RAW_OFFICIAL_DOCUMENT']
        urls, rejected = linked_official_ir_urls(originals, domains)
        diagnostics = []
        common = {'source': 'OFFICIAL_IR', 'instrument_id': record['event']['instrument_id'], 'receipt_id': receipt}
        if rejected:
            diagnostics.append({**common, 'reason': 'OFFICIAL_IR_LINK_OUTSIDE_ALLOWED_SCOPE',
                                'link_count': rejected, 'affects_current_evidence': False})
        if len(urls) > 8:
            diagnostics.append({**common, 'reason': 'OFFICIAL_IR_LINK_LIMIT',
                                'link_count': len(urls), 'affects_current_evidence': True})
        retry = False
        retained = {item['source'] for item in documents.values()
                    if item.get('interpretation_status') == 'RAW_OFFICIAL_IR_DOCUMENT'}
        for url in urls[:8]:
            if url in retained:
                continue  # Keep the first observed version and its original availability.
            try:
                result = self.dart.read_official_ir(url)
                if result.quality != 'COMPLETE' or len(result.records) != 1:
                    raise AdapterError('OFFICIAL_IR_FETCH_INCOMPLETE')
                item = result.records[0]
                observed_at = aware_time(result.retrieved_at.isoformat()).isoformat()
                identity = 'official-ir:' + receipt + ':' + digest(url) + ':' + item['sha256']
                documents[identity] = {'fact_id': identity, 'instrument_id': record['event']['instrument_id'],
                    'corp_code': record['receipt']['corp_code'], 'receipt_id': receipt, 'source': item['url'],
                    'sha256': item['sha256'], 'content': item['text'], 'published_at': None,
                    'observed_at': observed_at, 'available_at': observed_at,
                    'timing_quality': 'UNCERTAIN', 'interpretation_status': 'RAW_OFFICIAL_IR_DOCUMENT'}
            except AdapterError as error:
                retry = retry or error.code not in {'OFFICIAL_IR_UNSUPPORTED_FORMAT', 'OFFICIAL_IR_UNSUPPORTED_ENCODING',
                                                    'OFFICIAL_IR_DOCUMENT_SIZE_LIMIT'}
                diagnostics.append({**common, 'reason': error.code, 'source_url_sha256': digest(url),
                                    'affects_current_evidence': True, **error.diagnostic})
        return self.state.save_disclosure(receipt, {**record, 'documents': documents,
            'official_ir_scope': scope_hash, 'official_ir_checked_at': now.isoformat(),
            'official_ir_complete': not retry, 'official_ir_diagnostics': diagnostics})

    def _document_allowed(self, document, instrument_ids):
        from .adapters.disclosures import official_ir_url

        if document['instrument_id'] not in instrument_ids:
            return False
        if document.get('interpretation_status') != 'RAW_OFFICIAL_IR_DOCUMENT':
            return True
        try:
            official_ir_url(document['source'], self.config.app['market']['official_ir_domains'])
            return True
        except AdapterError:
            return False

    def _events(self,instruments,now,*,since=None):
        from .adapters import FetchResult
        from .disclosure_parser import PARSER_VERSION, correction_reference, official_event_family, parse_official_event, resolve_correction_parent
        from .market import DataQualityError
        self.disclosure_diagnostics = [row for row in self.state.data.get("disclosure_diagnostics",[])
                                      if row.get('source') != 'OFFICIAL_IR']
        settings = self.manifest["disclosures"]
        cached = self.state.data.setdefault("disclosure_records",{})
        pending = self.state.data.setdefault('disclosure_pending_documents', {})
        last_poll = self.state.data.get("disclosure_last_poll")
        coverage = dict(self.state.data.get("disclosure_coverage",{}))
        scope = {item.instrument_id for item in instruments}
        held = set()
        with self.state.lock:
            if self.state.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='holdings'").fetchone():
                held = {row[0] for row in self.state.db.execute("SELECT instrument_id FROM holdings WHERE owner='strategy' AND quantity>0")}
        current = self.calendar.available_session(now)
        scoped = since is not None
        poll_due = scoped or last_poll is None or (now-aware_time(last_poll)).total_seconds() >= 180
        reparse_due = any(record.get('parser_version') != PARSER_VERSION and record['event']['instrument_id'] in scope
                          for record in cached.values())
        if poll_due or reparse_due:
            self.disclosure_diagnostics = []
            with self.state.lock:
                start = since or date.fromisoformat(self.state.data.get("disclosure_cursor_date",settings["start_date"]))
            # A resumed cursor is never silently truncated; DART range requests are split.
            result_rows, qualities = [],[]
            upper = now.astimezone(SEOUL).date()
            if not poll_due:
                start = upper + timedelta(days=1)
            corporations = sorted(code for code, symbol in settings['instrument_by_corp_code'].items() if symbol in scope) if scoped else [None]
            if scoped and {settings['instrument_by_corp_code'][code] for code in corporations} != scope:
                raise HumanRequired('DISCLOSURE_CORPORATION_UNVERIFIED')
            while start <= upper:
                end = min(upper,start+timedelta(days=89))
                for corporation in corporations:
                    try:
                        arguments = {'corp_code': corporation} if corporation else {}
                        listed_ids, expected_total = set(), None
                        while True:
                            result = self.dart.list_disclosures(start,end,**arguments)
                            if result.metadata.get('error') in AUTH_ERRORS:
                                raise AdapterError(result.metadata['error'])
                            total = result.metadata.get('total_count')
                            if expected_total is not None and total != expected_total:
                                raise AdapterError('DISCLOSURE_PAGINATION_CHANGED')
                            expected_total = total
                            ids = {row['rcept_no'] for row in result.records}
                            if listed_ids & ids:
                                raise AdapterError('DISCLOSURE_DUPLICATE_PAGE')
                            listed_ids.update(ids)
                            result_rows.extend(result.records)
                            if result.metadata.get('error') != 'PAGE_LIMIT':
                                break
                            if type(result.next_cursor) is not int or result.next_cursor <= arguments.get('cursor', 1):
                                raise AdapterError('DISCLOSURE_PAGINATION_STALLED')
                            arguments['cursor'] = result.next_cursor
                        if result.quality == 'COMPLETE' and expected_total is not None and len(listed_ids) != expected_total:
                            raise AdapterError('DISCLOSURE_COUNT_MISMATCH')
                        qualities.append(result.quality)
                        if result.quality not in {'COMPLETE','COMPLETE_NO_EVENT'}:
                            self.disclosure_diagnostics.append({'source':'DART','reason':result.metadata.get('error','DISCLOSURE_FETCH_INCOMPLETE')})
                    except (AdapterError,ValueError,KeyError,TypeError) as error:
                        if isinstance(error,HumanRequired) or isinstance(error,AdapterError) and error.code in AUTH_ERRORS:
                            raise
                        qualities.append('FETCH_FAILED')
                        self.disclosure_diagnostics.append({'source':'DART','reason':getattr(error,'code','MALFORMED_RESPONSE')})
                start = end+timedelta(days=1)
            all_complete = all(quality in {"COMPLETE","COMPLETE_NO_EVENT"} for quality in qualities)
            quality = "COMPLETE" if result_rows and all_complete else "COMPLETE_NO_EVENT" if all_complete else "PARTIAL"
            if poll_due:
                coverage = {instrument.instrument_id:(quality if not scoped or coverage.get(instrument.instrument_id) in {'COMPLETE','COMPLETE_NO_EVENT'} else 'PARTIAL') for instrument in instruments}
            verified_events = []
            if settings.get("verified_events_path"):
                extraction_path = Path(settings["verified_events_path"])
                if hashlib.sha256(extraction_path.read_bytes()).hexdigest() != settings.get("verified_events_sha256"):
                    raise HumanRequired("Official extraction file differs from approved evidence hash")
                verified_events = _json(extraction_path)["events"]
            by_receipt = {event["official_id"]:event for event in verified_events}
            corporation_map = settings["instrument_by_corp_code"]
            corp_by_instrument = {symbol: code for code, symbol in corporation_map.items()}
            rows = {}
            for receipt, record in cached.items():
                event = record['event']
                if record.get('parser_version') == PARSER_VERSION or event['instrument_id'] not in scope:
                    continue
                # Old caches kept the official title and receipt ID, but not the list row.
                title = next((fact['value'] for fact in record['facts'] if fact.get('unit') == 'official_report_title'), event.get('facts', {}).get('report_title', ''))
                rows[receipt] = record.get('receipt') or {
                    'rcept_no': receipt, 'rcept_dt': receipt[:8], 'report_nm': title,
                    'corp_code': corp_by_instrument.get(event['instrument_id']),
                    'correction_parent_id': (event.get('correction_of') or '').removeprefix('dart:') or None}
            rows.update({row['rcept_no']: row for row in [*pending.values(), *result_rows]})
            correction_lists = {}
            documents_changed = False
            for row in rows.values():
                instrument_id = corporation_map.get(row["corp_code"])
                if instrument_id not in scope:
                    continue
                receipt = row["rcept_no"]
                previous = cached.get(receipt)
                title_text = row.get("report_nm", "")
                if self.manifest.get("automatic"):
                    if not any(word in title_text for word in ("계약", "전망", "계획", "영업실적", "실적공시", "사업보고서", "분기보고서", "반기보고서")):
                        continue
                if previous and previous.get('parser_version') == PARSER_VERSION and receipt not in pending:
                    continue  # Corrections have their own receipt; never reset original availability.
                published_date = None
                try:
                    published_date = date.fromisoformat(str(row.get('rcept_dt', '')))
                except ValueError:
                    pass
                recent = True
                if published_date is not None and published_date <= upper:
                    try:
                        published_session = self.calendar.available_session(datetime.combine(published_date, datetime.min.time(), tzinfo=SEOUL))
                        recent = 1 <= current.ordinal - published_session.ordinal + 1 <= self.profile['signal']['max_event_age_sessions']
                    except DataQualityError:
                        recent = False
                try:
                    if previous:
                        originals = tuple({'content': item['content'].encode('utf-8'), 'sha256': item['sha256']}
                                          for item in self.state.disclosure_documents(receipt).values()
                                          if item.get('interpretation_status') != 'RAW_OFFICIAL_IR_DOCUMENT')
                        document = FetchResult(originals, 'COMPLETE', aware_time(previous['event']['observed_at']))
                    else:
                        document = self.dart.read_disclosure(receipt)
                    if document.quality != "COMPLETE" or not document.records:
                        raise ValueError("PRIMARY_SOURCE_FETCH_FAILED")
                    document_hashes = sorted(item["sha256"] for item in document.records)
                    first_seen = self.state.data["events"].setdefault(receipt,previous['event']['available_at'] if previous else document.retrieved_at.isoformat())
                    available = aware_time(first_seen)
                    timing = 'DATE_ONLY' if published_date is not None and published_date <= min(upper, available.astimezone(SEOUL).date()) else 'UNCERTAIN'
                    if '정정' in title_text and official_event_family(title_text) and (recent or instrument_id in held):
                        reference = correction_reference(list(document.records))
                        if reference and reference[1] <= upper:
                            key = (row['corp_code'], reference[1])
                            if key not in correction_lists:
                                try:
                                    originals = self.dart.list_disclosures(reference[1], reference[1], corp_code=row['corp_code'])
                                    if originals.quality not in {'COMPLETE', 'COMPLETE_NO_EVENT'}:
                                        raise AdapterError(originals.metadata.get('error', 'CORRECTION_LIST_INCOMPLETE'))
                                    correction_lists[key] = list(originals.records)
                                except AdapterError as error:
                                    if isinstance(error, HumanRequired) or error.code in AUTH_ERRORS:
                                        raise
                                    correction_lists[key] = []
                                    self.disclosure_diagnostics.append({'source':'DART','instrument_id':instrument_id,
                                                                       'receipt_id':receipt,'reason':error.code,
                                                                       'affects_current_evidence':recent})
                            row = {**row, 'correction_parent_id': resolve_correction_parent(row, list(document.records), correction_lists[key])}
                    uri = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo="+receipt
                    title = MarketFact(fact_id="dart-title:"+receipt,instrument_id=instrument_id,value=row.get("report_nm",""),unit="official_report_title",
                        source=uri,content_hash=digest(row),published_at=None,observed_at=document.retrieved_at,available_at=available,quality="VERIFIED")
                    facts = [title]
                    documents = {"dart-document:"+receipt+":"+item["sha256"]:{
                        "fact_id":"dart-document:"+receipt+":"+item["sha256"],"instrument_id":instrument_id,
                        "receipt_id":receipt,"source":uri,"sha256":item["sha256"],"content":item["content"].decode("utf-8",errors="replace"),
                        "available_at":available.isoformat(),"interpretation_status":"RAW_OFFICIAL_DOCUMENT"} for item in document.records}
                    normalized = by_receipt.get(receipt)
                    if normalized and normalized["source_hash"] in document_hashes:
                        event = EventRecord.model_validate_json(canonical({**normalized,"available_at":available,"observed_at":document.retrieved_at,"timing_quality":timing,"published_date":published_date.isoformat() if published_date else None}))
                        if event.instrument_id != instrument_id or set(event.fact_ids) != set(event.facts):
                            raise ValueError("EXPLICIT_INSTRUMENT_FACT_LINKS_REQUIRED")
                        for fact_id,value in event.facts.items():
                            facts.append(MarketFact(fact_id=fact_id,instrument_id=instrument_id,value=value,unit=event.comparison_basis,
                                source=event.source_uri,content_hash=event.source_hash,published_at=event.published_at,
                                observed_at=document.retrieved_at,available_at=available,quality="VERIFIED"))
                        reason = "APPROVED_SOURCE_EXTRACTION"
                    else:
                        event,parsed_facts,reason = parse_official_event(row,list(document.records),instrument_id,available)
                        facts.extend(parsed_facts)
                        if event is not None:
                            event = event.model_copy(update={"timing_quality":timing,"available_at":available,"published_date":published_date})
                        else:
                            event = EventRecord(event_id="dart:"+receipt,instrument_id=instrument_id,official_id=receipt,normalized_key="dart:"+receipt,
                                family="unclassified",source_uri=uri,source_hash=document_hashes[0],fact_ids=[title.fact_id],facts={"report_title":title.value},
                                comparison_basis="raw original; unsupported primary template",available_at=available,observed_at=document.retrieved_at,
                                official=True,primary_source_complete=False,timing_quality=timing,published_date=published_date,polarity="UNKNOWN")
                    cached[receipt] = self.state.save_disclosure(receipt, {"event":event.model_dump(mode="json"),"facts":[fact.model_dump(mode="json") for fact in facts],
                                       "documents":documents,"document_hashes":document_hashes,"parse_reason":reason,
                                       "receipt":row,"parser_version":PARSER_VERSION})
                    if reason == 'CORRECTION_RELATION_UNRESOLVED' and (recent or instrument_id in held):
                        pending[receipt] = row
                    else:
                        pending.pop(receipt, None)
                    documents_changed = True
                except (ValueError,KeyError,AdapterError) as error:
                    if isinstance(error,HumanRequired) or isinstance(error,AdapterError) and error.code in AUTH_ERRORS:
                        raise
                    if recent:
                        coverage[instrument_id] = "PARTIAL"
                    pending[receipt] = row
                    self.disclosure_diagnostics.append({"source":"DART","instrument_id":instrument_id,
                                                       "receipt_id":receipt,"reason":getattr(error,"code","PRIMARY_SOURCE_FETCH_FAILED"),
                                                       "affects_current_evidence":recent})
            with self.state.lock:
                if poll_due and all_complete and not scoped:
                    self.state.data["disclosure_cursor_date"] = upper.isoformat()
                if poll_due and not scoped:
                    self.state.data['disclosure_last_poll'] = self.clock().isoformat()
                self.state.data.setdefault('disclosure_coverage', {}).update(coverage)
                previous = [row for row in self.state.data.get('disclosure_diagnostics', [])
                            if scoped and row.get('instrument_id') not in scope]
                self.state.data['disclosure_diagnostics'] = previous + self.disclosure_diagnostics
                keys = ['disclosure_pending_documents', 'disclosure_coverage', 'disclosure_diagnostics']
                keys += [key for key in ('disclosure_cursor_date','disclosure_last_poll') if not scoped and key in self.state.data]
                if documents_changed:
                    keys += ['events','disclosure_records']
                self.state.save(keys)
        events,facts,documents = [],[],{}
        for receipt, record in list(cached.items()):
            event = EventRecord.model_validate_json(canonical(record["event"]))
            if event.instrument_id not in scope:
                continue
            try:
                age = self.calendar.event_age(event,current.session_id)
                recent = 1 <= age <= self.profile["signal"]["max_event_age_sessions"]
            except DataQualityError:
                recent = False
            if event.instrument_id not in held and not recent:
                continue
            if any('content_sha256' not in document for document in record['documents'].values()):
                originals = self.state.disclosure_documents(receipt)
                if set(originals) != set(record['documents']):
                    raise AdapterError('DISCLOSURE_CACHE_MISSING')
                record = self.state.save_disclosure(receipt, {**record, 'documents': originals})
            record = self._official_ir_documents(receipt, record, now)
            cached[receipt] = record
            ir_diagnostics = record.get('official_ir_diagnostics', []) if self.config.app['market']['official_ir_domains'] else []
            self.disclosure_diagnostics.extend(ir_diagnostics)
            events.append(event)
            facts.extend(MarketFact.model_validate_json(canonical(fact)) for fact in record["facts"])
            documents.update({key: value for key, value in record['documents'].items()
                              if self._document_allowed(value, scope)})
            if coverage.get(event.instrument_id) == "COMPLETE_NO_EVENT":
                coverage[event.instrument_id] = "COMPLETE"
            if recent and record.get('parse_reason') != 'OBSERVATION_ONLY_EVENT_FAMILY' and (
                    event.timing_quality == "UNCERTAIN" or not event.primary_source_complete):
                coverage[event.instrument_id] = "PARTIAL"
            if recent and any(item['affects_current_evidence'] for item in ir_diagnostics):
                coverage[event.instrument_id] = 'PARTIAL'
        with self.state.lock:
            previous = [row for row in self.state.data.get('disclosure_diagnostics', [])
                        if scoped and row.get('instrument_id') not in scope]
            diagnostics = list({canonical(row): row for row in previous + self.disclosure_diagnostics}.values())
            if self.state.data.get('disclosure_diagnostics') != diagnostics:
                self.state.data['disclosure_diagnostics'] = diagnostics
                self.state.save(('disclosure_diagnostics',))
        return events,facts,coverage,documents

    def refresh_decision(self, frozen):
        """Recheck the decision's issuers and account without rebuilding the universe."""
        with self.collect_lock:
            self.config.assert_current()
            scope = {row['instrument_id'] for row in frozen['events'] + frozen['facts']} | document_scope(frozen)
            current = self.refresh_protection()
            instruments = [current.instruments[symbol] for symbol in sorted(scope)]
            events, facts, coverage, documents = self._events(instruments, self.clock(),
                since=aware_time(frozen['created_at']).astimezone(SEOUL).date())
            if any(row.get('affects_current_evidence', True) for row in self.disclosure_diagnostics):
                raise AdapterError('DECISION_EVIDENCE_REFRESH_INCOMPLETE')
            bundle = copy(current)
            bundle.events = list(EventRegistry([row for row in current.events if row.instrument_id not in scope] + events).records.values())
            bundle.facts = [row for row in current.facts if row.instrument_id not in scope] + facts
            bundle.data = {**current.data, 'events':[row.model_dump(mode='json') for row in bundle.events],
                'facts':[row.model_dump(mode='json') for row in bundle.facts],
                'coverage':{**current.data['coverage'], **coverage},
                'raw_documents':{**{key: value for key, value in current.data['raw_documents'].items()
                                   if value['instrument_id'] not in scope}, **documents}}
            return self._publish(bundle)

    def _account(self, *, allow_idle=False):
        self.config.require_external("account_read",self.approval)
        account = (self.broker.snapshot(maximum_age_seconds=self.config.app['monitoring']['account_poll_idle_seconds'])
                   if allow_idle and isinstance(self.broker,KisBrokerPort) else self.broker.snapshot())
        store = getattr(self.broker, 'store', None)
        if account.get("complete") is not True or account.get("ownership_complete") is not True:
            diagnostics = account.get('diagnostics') or account.get('errors', [])
            if store is not None:
                with store.transaction():
                    store.set('reconciled', False)
                    store.set('account_cash_reconciled', False)
                    store.set('account_checked_at', self.clock().isoformat())
                    store.set('account_diagnostics', diagnostics)
                    store.event('account', 'ACCOUNT_INCOMPLETE', {'diagnostics': diagnostics})
            error = HumanRequired("External account observations incomplete: "+",".join(account.get("errors",[])))
            error.diagnostics = diagnostics
            raise error
        return account,account.get('refresh_order',time.monotonic_ns())

    def _protection_symbols(self,account):
        symbols = {key for key,value in account.get("strategy_quantities",{}).items() if value}
        symbols.update(row["instrument_id"] for row in account.get("orders",[])
                       if row["state"] not in {"FILLED","CANCELED","REJECTED"})
        store = getattr(self.broker,"store",None)
        if store is not None:
            symbols.update(row["instrument_id"] for row in store.holdings()+store.working())
        return symbols

    def _publish(self,bundle,*,protection=False):
        # Only local publication is serialized; no provider request holds this lock.
        with self.publish_lock:
            latest = self.latest_bundle
            incoming = bundle
            if protection:
                bundle = copy(latest)
                bundle.data = dict(latest.data)
                bundle.data["runtime_diagnostics"] = [row for row in latest.data["runtime_diagnostics"]
                    if row.get('scope') not in {'PROTECTION','ENTRY'} or
                    row.get('instrument_id') not in incoming.data.get('quote_refresh_order',{})]+incoming.data["runtime_diagnostics"]
                bundle.exclusions = [row for row in latest.exclusions
                    if row.get('scope') not in {'PROTECTION','ENTRY'} or
                    row.get('instrument_id') not in incoming.data.get('quote_refresh_order',{})]+incoming.data['runtime_diagnostics']
            observations = [item for item in (latest,incoming) if item is not None]
            account = max(observations,key=lambda item:item.data.get("account_refresh_order",0))
            for key in ("account_snapshot","broker_available_cash","strategy_sellable_quantities","account_refresh_order"):
                bundle.data[key] = account.data[key]
            quotes,depth,orders = {},{},{}
            for item in observations:
                for symbol,order in item.data.get("quote_refresh_order",{}).items():
                    if order >= orders.get(symbol,0):
                        if (symbol in quotes and symbol in item.quotes and
                                item.quotes[symbol].observed_at < quotes[symbol].observed_at):
                            continue
                        orders[symbol] = order
                        quotes.pop(symbol,None)
                        depth.pop(symbol,None)
                        if symbol in item.quotes:
                            quotes[symbol] = item.quotes[symbol]
                            depth[symbol] = dict(item.data["quote_depth"].get(symbol,{}))
            bundle.quotes = quotes
            bundle.now = max(item.now for item in observations)
            bundle.data.update(as_of=bundle.now.isoformat(),quotes=[item.model_dump(mode="json") for item in quotes.values()],
                               quote_depth=depth,quote_refresh_order=orders)
            if isinstance(self.broker,PaperBrokerPort):
                paper = self.broker.snapshot(bundle=bundle)
                bundle.data.update(account_snapshot=paper,broker_available_cash=paper["broker_available_cash"],
                                   strategy_sellable_quantities=paper["strategy_sellable_quantities"],account_refresh_order=time.monotonic_ns())
            self.latest_bundle = bundle
            if isinstance(self.broker,KisBrokerPort):
                self.broker.latest_bundle = bundle
            return bundle

    def refresh_protection(self, *, allow_idle_account=False):
        account,account_order = self._account(allow_idle=allow_idle_account)
        return self.refresh_quotes(account=account,account_order=account_order)

    def refresh_quotes(self, symbols=(), *, account=None, account_order=None):
        """Read current quotes at their point of use; retain frozen evidence/account age."""
        self.config.assert_current()
        self.config.require_external("market_read",self.approval)
        with self.publish_lock:
            previous = self.latest_bundle
        if previous is None:
            raise HumanRequired("MONITOR_DEGRADED: VERIFIED_PROTECTION_REFERENCE_UNAVAILABLE")
        if account is None:
            account,account_order = previous.data['account_snapshot'],previous.data['account_refresh_order']
        excluded = self._subscribe_quotes(account)
        quotes,depth,orders,diagnostics = {},{},{},[]
        protected = self._protection_symbols(account)
        symbols = set(symbols)
        maximum_age = self.profile['orders']['quote_max_age_seconds'] if symbols else monitor_quote_max_age(self.profile)
        for symbol in sorted(symbols or protected):
            try:
                if symbol in excluded:
                    raise ValueError("PROTECTED_STREAM_CAPACITY_EXCEEDED")
                instrument = previous.instruments.get(symbol)
                if instrument is None:
                    raise ValueError("VERIFIED_INSTRUMENT_UNAVAILABLE")
                quotes[symbol] = self._quote(instrument,depth=depth,maximum_age_seconds=maximum_age)
                if not quote_fresh(quotes[symbol],self.clock(),maximum_age):
                    raise ValueError("STALE_QUOTE")
            except (ValueError,KeyError,AdapterError) as error:
                if isinstance(error,HumanRequired) or isinstance(error,AdapterError) and error.code in AUTH_ERRORS:
                    raise
                diagnostics.append({"scope":"PROTECTION" if symbol in protected else "ENTRY","instrument_id":symbol,"reason":"MONITOR_DEGRADED",
                                    "detail":getattr(error,"code",str(error)), **getattr(error, 'diagnostic', {})})
            orders[symbol] = time.monotonic_ns()
        bundle = copy(previous)
        bundle.now = self.clock()
        bundle.quotes = quotes
        bundle.data = {**previous.data,"account_snapshot":account,"broker_available_cash":account["broker_available_cash"],
                       "strategy_sellable_quantities":account.get("strategy_sellable_quantities",{}),
                       "account_refresh_order":account_order,"quote_depth":depth,"quote_refresh_order":orders,
                       "runtime_diagnostics":diagnostics}
        return self._publish(bundle,protection=True)

    def collect_disclosures(self):
        """Refresh official evidence without repeating the account/quote census."""
        with self.collect_lock:
            self.config.assert_current()
            self.config.require_external('disclosure_read', self.approval)
            previous = self.latest_bundle
            if previous is None:
                return self.refresh()
            events, facts, coverage, documents = self._events(list(previous.instruments.values()), self.clock())
            data = {**previous.data, 'as_of': self.clock().isoformat(),
                'events': [row.model_dump(mode='json') for row in events],
                'facts': [row.model_dump(mode='json') for row in facts],
                'coverage': coverage, 'raw_documents': documents,
                'runtime_diagnostics': [row for row in previous.data['runtime_diagnostics']
                                        if row.get('source') not in {'DART', 'OFFICIAL_IR'}]
                    + self.disclosure_diagnostics}
            return self._publish(MarketBundle(data, self.profile, mode=self.config.mode))

    def refresh(self):
        with self.collect_lock:
            self.config.assert_current()
            self.config.require_external("market_read",self.approval)
            now = self.clock()
            from .deployment_sources import completed_sessions, prepare_market_sources
            if self.manifest.get("automatic") and aware_time(self.manifest["calendar"]["observed_at"]).astimezone(SEOUL).date() != now.astimezone(SEOUL).date():
                sources = prepare_market_sources(self.kis, self.dart, now=now)
                self.manifest["calendar"], self.manifest["ticks"] = sources["calendar"], sources["ticks"]
                self.manifest["normalization"].update(sources["normalization"])
                self.manifest["disclosures"]["instrument_by_corp_code"] = sources["disclosures"]["instrument_by_corp_code"]
                from .deployment import estimated_costs
                self.manifest["costs"] = estimated_costs(self.manifest["account_alias"], now)
                self.calendar = SessionCalendar([Session.model_validate_json(canonical(row)) for row in sources["calendar"]["sessions"]],
                    provenance=sources["calendar"]["source"], verified=True, synthetic=False)
                self.daily_cache = None
            completed = completed_sessions(self.calendar, now)
            if len(completed) < self.profile["universe"]["minimum_completed_bars"]:
                raise HumanRequired("VERIFIED_CALENDAR_HISTORY_INSUFFICIENT")
            day = completed[-1].session_id
            account,account_order = self._account()
            new_day = self.daily_cache is None or self.daily_cache[0] != day
            if new_day:
                instruments,diagnostics = self._instruments(now)
                bars,index_bars = {},{}
            else:
                _,instruments,bars,index_bars,diagnostics = self.daily_cache
            diagnostics = list(diagnostics)
            universe_diagnostics = list(diagnostics)
            if self.manifest.get("automatic") and self.latest_bundle is None:
                # Start existing-position protection before collecting a month
                # of disclosure originals. The review worker fills coverage;
                # no entry can pass the uncollected event gate in the meantime.
                events, facts, documents = [], [], {}
                coverage = {item.instrument_id: "FETCH_FAILED" for item in instruments}
            else:
                events,facts,coverage,documents = self._events(instruments,self.clock())
            expected = [session.session_id for session in completed[-self.profile["universe"]["minimum_completed_bars"]:]]
            start = completed[-self.profile["universe"]["minimum_completed_bars"]].opens_at.date()
            end = completed[-1].closes_at.date()
            needed = self._protection_symbols(account)
            current_session = self.calendar.available_session(now)
            for event in events:
                try:
                    age = self.calendar.event_age(event,current_session.session_id)
                except ValueError:
                    continue
                if (event.official and event.primary_source_complete and event.available_at <= now and
                        event.timing_quality != 'UNCERTAIN' and 1 <= age <= self.profile["signal"]["max_event_age_sessions"]):
                    needed.add(event.instrument_id)
            for board in self.profile["universe"]["boards"]:
                if board not in index_bars:
                    index_bars[board] = self._bars(self.kis.read_index_bars(board,start,end),index=True,expected=expected)
            for instrument in instruments:
                if instrument.kind != "common_stock" or instrument.status != "NORMAL" or not instrument.status_verified or not instrument.sector:
                    continue
                # Whole universe comes from the master; technical history is only
                # needed for source-qualified events and held/working positions.
                if self.manifest.get("automatic") and instrument.instrument_id not in needed:
                    continue
                if instrument.instrument_id in bars:
                    continue
                try:
                    values = self._bars(self.kis.read_bars(instrument.instrument_id.removeprefix("KRX:"),start,end),expected=expected)
                    if [bar.session_id for bar in values] != expected:
                        raise ValueError("DAILY_CALENDAR_COVERAGE_INCOMPLETE")
                    bars[instrument.instrument_id] = values
                except (ValueError,KeyError,AdapterError) as error:
                    if isinstance(error,HumanRequired) or isinstance(error,AdapterError) and error.code in AUTH_ERRORS:
                        raise
                    diagnostics.append({"instrument_id":instrument.instrument_id,"reason":getattr(error,"code",str(error))})
            self.daily_cache = (day,instruments,bars,index_bars,universe_diagnostics)
            diagnostics.extend(self.disclosure_diagnostics)
            protected = self._protection_symbols(account)
            current = self.calendar.active(self.clock())
            recent = set()
            if current:
                for event in events:
                    try:
                        age = self.calendar.event_age(event,current.session_id)
                    except ValueError:
                        continue
                    if (event.official and event.primary_source_complete and not event.withdrawn and
                            event.available_at <= self.clock() and event.timing_quality != 'UNCERTAIN' and
                            event.family in self.profile["signal"]["event_families"] and
                            1 <= age <= self.profile["signal"]["max_event_age_sessions"]):
                        recent.add(event.instrument_id)
            quotes,depth,quote_orders = [],{},{}
            quote_instruments = []
            for instrument in instruments:
                try:
                    if instrument.instrument_id not in protected:
                        if instrument.instrument_id not in bars:
                            continue
                        features = calculate_features(bars[instrument.instrument_id],index_bars[instrument.board],
                            instrument_id=instrument.instrument_id,board=instrument.board,as_of=self.clock(),research_profile=self.profile)
                        reasons = [reason for failed,reason in (
                            (instrument.instrument_id not in recent, 'NO_VALID_RECENT_OFFICIAL_EVENT'),
                            (features.adtv20 < Decimal(self.profile['universe']['minimum_adtv_krw']), 'INSUFFICIENT_LIQUIDITY'),
                            (not (features.close > features.sma60 and features.sma20 >= features.sma20_five_sessions_ago), 'TREND_GATE_FAILED'),
                            (features.rs20 <= 0, 'RELATIVE_STRENGTH_GATE_FAILED'),
                            (features.index_close < features.index_sma60, 'BOARD_INDEX_GATE_FAILED')) if failed]
                        if reasons:
                            diagnostics.append({'scope':'SCREENING', 'instrument_id':instrument.instrument_id,
                                                'reason':reasons[0], 'reasons':reasons})
                            continue
                    quote_instruments.append(instrument)
                except (ValueError,KeyError,AdapterError) as error:
                    if isinstance(error,HumanRequired) or isinstance(error,AdapterError) and error.code in AUTH_ERRORS:
                        raise
                    quote_orders[instrument.instrument_id] = time.monotonic_ns()
                    diagnostics.append({"instrument_id":instrument.instrument_id,"reason":getattr(error,"code",str(error))})
            excluded = self._subscribe_quotes(account, {item.instrument_id for item in quote_instruments})
            for instrument_id in sorted(excluded):
                diagnostics.append({'scope':'PROTECTION' if instrument_id in protected else 'ENTRY',"instrument_id":instrument_id,"reason":"STREAM_SUBSCRIPTION_CAPACITY_EXCEEDED"})
                quote_orders[instrument_id] = time.monotonic_ns()
            quote_instruments = [item for item in quote_instruments if item.instrument_id not in excluded]
            self._wait_for_stream_quotes(quote_instruments)
            for instrument in quote_instruments:
                try:
                    maximum_age = (monitor_quote_max_age(self.profile) if instrument.instrument_id in protected
                                   else self.profile['orders']['quote_max_age_seconds'])
                    quotes.append(self._quote(instrument,depth=depth,maximum_age_seconds=maximum_age))
                except (ValueError,KeyError,AdapterError) as error:
                    if isinstance(error,HumanRequired) or isinstance(error,AdapterError) and error.code in AUTH_ERRORS:
                        raise
                    diagnostics.append({'scope':'PROTECTION' if instrument.instrument_id in protected else 'ENTRY',"instrument_id":instrument.instrument_id,
                                        "reason":getattr(error,"code",str(error)), **getattr(error, 'diagnostic', {})})
                quote_orders[instrument.instrument_id] = time.monotonic_ns()
            now = self.clock()
            ticks = self.manifest["ticks"]
            data = {"provenance":"VERIFIED_EXTERNAL_OBSERVATIONS","as_of":now.isoformat(),"account_identity":self.manifest["bootstrap"]["account_identity"],
                "broker_available_cash":account["broker_available_cash"],"sector_classification_verified":True,
                "sessions":[session.model_dump(mode="json") for session in self.calendar.sessions],"calendar_source":self.manifest["calendar"]["source"],
                "calendar_verified":True,"tick_bands":ticks["bands"],"tick_source":ticks["source"],"ticks_verified":True,
                "tick_effective_at":ticks["effective_at"],"tick_expires_at":ticks["expires_at"],"costs":self.manifest["costs"],
                "instruments":[item.model_dump(mode="json") for item in instruments],"instrument_names":dict(self.instrument_names),
                "quotes":[item.model_dump(mode="json") for item in quotes],
                "bars":{key:[bar.model_dump(mode="json") for bar in values] for key,values in bars.items()},
                "index_bars":{key:[bar.model_dump(mode="json") for bar in values] for key,values in index_bars.items()},
                "events":[event.model_dump(mode="json") for event in events],"facts":[fact.model_dump(mode="json") for fact in facts],
                'history_requested':sorted(needed) if self.manifest.get('automatic') else sorted(item.instrument_id for item in instruments),
                "coverage":coverage,"runtime_diagnostics":diagnostics,"raw_documents":documents,"account_snapshot":account,
                "quote_depth":depth,"quote_refresh_order":quote_orders,"account_refresh_order":account_order,
                "strategy_sellable_quantities":account.get("strategy_sellable_quantities",{})}
            bundle = MarketBundle(data,self.profile,mode=self.config.mode)
            requested = {item.instrument_id for item in quote_instruments}
            bundle.candidates = [candidate for candidate in bundle.candidates if candidate.instrument.instrument_id in requested]
            bundle.exclusions = list({(row.get('source'),row.get('instrument_id'),row['reason']):row
                                      for row in [*bundle.exclusions,*diagnostics]}.values())
            return self._publish(bundle)

    def _frozen_documents(self, frozen, instrument_ids):
        if 'document_manifest' not in frozen:
            return {}  # Legacy inputs confer no original-document authority.
        try:
            manifest = freeze_documents(frozen.get('document_manifest', {}), set(instrument_ids),
                                        aware_time(frozen['created_at']))
            if manifest != frozen.get('document_manifest', {}):
                raise AdapterError('FROZEN_DOCUMENT_SCOPE_MISMATCH')
            documents = {}
            for receipt in sorted({document['receipt_id'] for document in manifest.values()}):
                cached = self.state.disclosure_documents(receipt)
                for key, expected in manifest.items():
                    if expected['receipt_id'] != receipt:
                        continue
                    actual = cached.get(key)
                    if not isinstance(actual, dict) or not isinstance(actual.get('content'), str):
                        raise AdapterError('FROZEN_DOCUMENT_MISSING')
                    if ({field: value for field, value in actual.items() if field != 'content'} != expected or
                            hashlib.sha256(actual['content'].encode('utf-8')).hexdigest() != expected['content_sha256']):
                        raise AdapterError('FROZEN_DOCUMENT_CHANGED')
                    if not self._document_allowed(actual, instrument_ids):
                        raise AdapterError('FROZEN_DOCUMENT_SCOPE_MISMATCH')
                    documents[key] = actual
            return documents
        except (ValueError, KeyError, TypeError):
            raise AdapterError('FROZEN_DOCUMENT_INVALID') from None

    def decide(self,frozen,*,on_progress=None):
        self.config.require_external("model_call",self.approval)
        codex = self._model_for_call()
        store = getattr(self.broker,"store",None)
        if store is None:
            raise AdapterError("MODEL_JOURNAL_STORE_UNBOUND")
        enriched = dict(frozen)
        ids = sorted(document_scope(frozen))
        documents = self._frozen_documents(frozen, ids)
        enriched["tool_records"] = {
            "events":{event["event_id"]:event for event in frozen["events"]},
            "facts":{**{fact["fact_id"]:fact for fact in frozen["facts"]},**documents},
            "candidates":{item["instrument"]["instrument_id"]:item for item in frozen["candidates"]},
            "theses":{item["thesis_id"]:item for item in frozen["theses"]},"bars":{},"official_evidence":{}}
        if self.latest_bundle:
            enriched["tool_records"]["bars"] = {key:[dict(bar.model_dump(mode="json"),date=bar.session_id) for bar in values]
                                                 for key,values in self.latest_bundle.bars.items() if key in ids}
        enriched["tool_scope"] = {"instrument_ids":ids,"start":self.calendar.sessions[0].session_id,
                                  "end":self.clock().date().isoformat(),"official_domains":["dart.fss.or.kr",*self.config.app["market"]["official_ir_domains"]]}
        started_at = self.clock()
        call_id = str(uuid4())
        attempt_root = self.config.state_dir/"model-attempts"/call_id
        def validate_at_completion(value):
            completed_at = self.clock()
            return validate_proposal(value,frozen,current_account_version=frozen["portfolio"]["account_state_version"],
                current_facts_hash=digest([frozen["events"],frozen["facts"]]),
                current_documents_hash=digest(frozen.get('document_manifest', {})), completed_at=completed_at,now=completed_at)
        result = codex.run(enriched,frozen["output_contract"],attempt_root=attempt_root,
            prompt=(ROOT/"prompts/portfolio_decision.md").read_text(),validate_schema=lambda value:DecisionProposal.model_validate(value),
            validate_semantic=validate_at_completion,on_progress=on_progress,
            expires_at=started_at+timedelta(seconds=codex.run_budget_seconds))
        self._record_model_result(store, frozen, call_id, attempt_root, started_at, result, purpose="review", codex=codex)
        if result.status != "SUCCESS":
            raise AdapterError("MODEL_"+result.status, diagnostic=result.diagnostic)
        return result.decision.model_dump(mode="json") if hasattr(result.decision,"model_dump") else result.decision

    def chat(self, *, request_id, session_id, messages, account_context=None, attachments=None,
             on_progress=None, cancel=None):
        """A frozen read-only account view, without broker or settings authority."""
        self.config.assert_current()
        self.config.require_external("model_call", self.approval)
        codex = self._model_for_call()
        store = getattr(self.broker, "store", None)
        if store is None:
            raise AdapterError("MODEL_JOURNAL_STORE_UNBOUND")
        frozen = {"schema_version": 1, "run_id": request_id, "session_id": session_id,
                  "created_at": self.clock().isoformat(), "strategy_hash": self.config.strategy_hash,
                  "code_id": code_identity(), "config_hash": self.config.config_hash, "conversation": messages,
                  "tool_scope": {"instrument_ids": []}, "tool_records": {}}
        if account_context is not None:
            frozen["account_context"] = account_context
        if attachments:
            frozen["attachments"] = attachments
        frozen["input_snapshot_id"] = digest(frozen)
        schema = {"type": "object", "properties": {"reply_text": {"type": "string", "minLength": 1, "maxLength": 12000}},
                  "required": ["reply_text"], "additionalProperties": False}
        def validate_reply(value):
            if (not isinstance(value, dict) or set(value) != {"reply_text"} or
                    not isinstance(value["reply_text"], str) or not 1 <= len(value["reply_text"].strip()) <= 12000):
                raise ValueError("INVALID_CHAT_REPLY")
            reject_credentials(value)
            return value
        call_id, started_at = str(uuid4()), self.clock()
        attempt_root = self.config.state_dir / "model-attempts" / call_id
        result = codex.run(frozen, schema, attempt_root=attempt_root,
            prompt=(ROOT / "prompts/general_chat.md").read_text(), validate_schema=validate_reply,
            validate_semantic=lambda _value: True,
            expires_at=started_at + timedelta(seconds=codex.run_budget_seconds),
            on_progress=on_progress, cancel=cancel)
        self._record_model_result(store, frozen, call_id, attempt_root, started_at, result, purpose="chat", codex=codex)
        if result.status != "SUCCESS":
            return {"status": "MODEL_" + result.status, "session_id": session_id,
                    "reply_text": {"CANCELED": "현재 대화 응답을 중단했습니다. 보호 감시는 계속됩니다.",
                        "AUTH_FAILED": "Codex 로그인 검증에 실패했습니다. /status에서 인증 상태를 확인해 주세요.",
                        "QUOTA_CIRCUIT_OPEN": "Codex 사용 한도가 막혀 있습니다. /usage에서 남은 한도와 초기화 시각을 확인해 주세요.",
                        "QUOTA_EXHAUSTED": "Codex 사용 한도를 소진했습니다. /usage에서 초기화 시각을 확인해 주세요.",
                        "TIMEOUT": "모델 응답 시간이 초과되었습니다. 보호 감시는 계속됩니다."
                    }.get(result.status, "Codex 응답을 완료하지 못했습니다. 로그인 여부와 별도로 실행 오류를 기록했습니다. /status에서 확인해 주세요."),
                    "model_called": result.attempts > 0, "orders_created": False}
        value = validate_reply(result.decision)
        return {"status": "CHAT_COMPLETE", "session_id": session_id, "reply_text": value["reply_text"],
                "model_called": result.attempts > 0, "orders_created": False}

    def _record_model_result(self, store, frozen, call_id, attempt_root, started_at, result, *, purpose, codex):
        attempts = []
        for path in sorted(attempt_root.glob("*/result.json"),key=lambda item:(item.stat().st_mtime_ns,str(item))):
            record = _json(path)
            attempts.append({"attempt_id":path.parent.name,"status":record["status"],"usage":record.get("usage"),
                             "diagnostic": record.get("diagnostic"),
                             "input_sha256":record["input_sha256"],"provenance":record.get("provenance")})
        metadata = {"call_id":call_id,"input_snapshot_id":frozen["input_snapshot_id"],"model_id":codex.model_id,
                    "provider":"codex_cli","reasoning_effort":codex.reasoning_effort,"purpose":purpose,
                    **{key: frozen[key] for key in ("created_at", "strategy_hash", "config_hash", "code_id") if key in frozen}}
        with store.transaction():
            health = {"status": result.status, "checked_at": self.clock().isoformat(),
                "purpose": purpose, "diagnostic": result.diagnostic or (attempts[-1].get("diagnostic") if attempts else None)}
            store.set("model_health", health)
            store.set('model_health:' + purpose, health)
            for index,attempt in enumerate(attempts,1):
                store.event(frozen["run_id"],"MODEL_ATTEMPT",{**metadata,**attempt,"record_type":"MODEL_ATTEMPT",
                    "attempt_number":index,"usage_scope":"attempt"})
            store.event(frozen["run_id"],"MODEL_OUTCOME",{**metadata,"record_type":"MODEL_OUTCOME","status":result.status,
                "attempt_count":result.attempts,"usage":result.usage,"usage_scope":"last_attempt",
                "diagnostic": health["diagnostic"],
                "reset_at":result.reset_at.isoformat() if result.reset_at else None,
                "started_at":started_at.isoformat(),"completed_at":self.clock().isoformat()})


class PaperBrokerPort:
    """Persisted quote-constrained simulation, using subsequent observations only."""
    environment = "paper"

    def __init__(self,runtime):
        self.runtime,self.state = runtime,runtime.state
        with self.state.lock:
            self.state.data.setdefault("paper",{"orders":{},"quantities":{},"cash":runtime.profile["capital_krw"]})
            self.state.save(("paper",))

    def bind_store(self,store):
        self.store = store

    def submit(self,intent):
        now = self.runtime.clock()
        identifier = digest([intent["plan_id"],intent["side"],intent.get("plan_revision",1)])
        namespace = "paper:"+self.runtime.manifest["account_alias"]
        with self.state.lock:
            self.state.data["paper"]["orders"].setdefault(identifier,{
                **intent,"broker_id":identifier,"namespace":namespace,"state":"ACKNOWLEDGED",
                "created_at":now.isoformat(),"cumulative_quantity":0,"cumulative_notional":"0","cumulative_fees":"0",
                "observed_at":now.isoformat(),"revision":0,"last_quote_at":None,
                "first_fill_at":None,"fill_time_quality":"UNKNOWN","fill_session_id":None})
            self.state.save(("paper",))
        return {"status":"ACKNOWLEDGED","broker_id":identifier,"namespace":namespace}

    def cancel(self,request):
        with self.state.lock:
            order = self.state.data["paper"]["orders"][request["broker_id"]]
            if order["namespace"] != request["namespace"]:
                raise ValueError("PAPER_ORDER_NAMESPACE_MISMATCH")
            order["state"] = "CANCELED"
            order["revision"] += 1
            self.state.save(("paper",))
        return {"status":"ACKNOWLEDGED"}

    def snapshot(self,*,bundle=None):
        bundle,now = bundle if bundle is not None else self.runtime.latest_bundle,self.runtime.clock()
        costs = CostSchedule.model_validate_json(canonical(self.runtime.manifest["costs"]))
        with self.state.lock:
            ledger = self.state.data["paper"]
            for order in ledger["orders"].values():
                if bundle is None or order["state"] not in {"ACKNOWLEDGED","PARTIALLY_FILLED"}:
                    continue
                quote = bundle.quotes.get(order["instrument_id"])
                if (quote is None or not quote.valid or quote.observed_at <= aware_time(order["created_at"]) or
                        order["last_quote_at"] == quote.observed_at.isoformat() or
                        not 0 <= (now-quote.observed_at).total_seconds() <= self.runtime.profile["orders"]["quote_max_age_seconds"]):
                    continue
                if order["expires_at"] and now >= aware_time(order["expires_at"]):
                    continue
                buy = order["side"] == "BUY"
                raw_price = quote.ask if buy else quote.bid
                if raw_price is None:
                    continue
                price = raw_price+slippage(costs,1,raw_price,"BUY") if buy else raw_price-slippage(costs,1,raw_price,"SELL")
                if price <= 0 or buy and price > Decimal(order["limit_price"]):
                    continue
                available = bundle.data["quote_depth"].get(order["instrument_id"],{}).get("ask" if buy else "bid",0)
                quantity = min(order["quantity"]-order["cumulative_quantity"],available)
                if not buy:
                    quantity = min(quantity,ledger["quantities"].get(order["instrument_id"],0))
                if not quantity:
                    continue
                cumulative = order["cumulative_quantity"]+quantity
                cumulative_notional = Decimal(order["cumulative_notional"])+quantity*price
                average = cumulative_notional/cumulative
                cumulative_fees = buy_commission(costs,cumulative,average) if buy else sell_cost(costs,cumulative,average)
                fees = cumulative_fees-Decimal(order["cumulative_fees"])
                cash = Decimal(ledger["cash"])
                if buy and price*quantity+fees > cash:
                    continue
                order.update(cumulative_quantity=cumulative,cumulative_notional=str(cumulative_notional),
                             cumulative_fees=str(cumulative_fees),revision=order["revision"]+1,
                             observed_at=quote.observed_at.isoformat(),last_quote_at=quote.observed_at.isoformat(),
                             state="FILLED" if cumulative == order["quantity"] else "PARTIALLY_FILLED",
                             first_fill_at=order["first_fill_at"] or quote.observed_at.isoformat(),fill_time_quality="EXACT",
                             fill_session_id=quote.observed_at.astimezone(SEOUL).date().isoformat())
                ledger["quantities"][order["instrument_id"]] = ledger["quantities"].get(order["instrument_id"],0)+(quantity if buy else -quantity)
                ledger["cash"] = str(cash-quantity*price-fees if buy else cash+quantity*price-fees)
            self.state.save(("paper",))
            return {"complete":True,"ownership_complete":True,"orders":list(ledger["orders"].values()),
                    "strategy_quantities":dict(ledger["quantities"]),"broker_available_cash":ledger["cash"],
                    "strategy_sellable_quantities":dict(ledger["quantities"]),"provenance":"PAPER_QUOTE_CONSTRAINED_SIMULATION"}


class ShadowBrokerPort(KisBrokerPort):
    environment = None

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.environment = None

    def submit(self,_intent):
        raise HumanRequired("SHADOW_BROKER_MUTATION_FORBIDDEN")

    def cancel(self,_request):
        raise HumanRequired("SHADOW_BROKER_MUTATION_FORBIDDEN")


def build_external_runtime(config,trusted_approval,*,kis_transport=None,dart_transport=None,
                           model_runner=None,env=None,clock=utcnow,ws_connector=None,
                           manifest=None,kis=None,dart=None,state=None,store=None):
    """Return (MarketBundle, broker port, decision callback, refresh callback).

    The optional transports/runner are injection points for contract tests, not
    authority bypasses. Every injected operation still checks the trusted grant.
    """
    for capability in ("account_read","market_read","disclosure_read","model_call","broker_auth"):
        config.require_external(capability,trusted_approval)
    automatic = manifest is not None and trusted_approval.get("authority") == "deployment_config"
    if manifest is not None and not automatic:
        raise HumanRequired("Runtime observations require operator deployment authority")
    if not automatic:
        path = config.app["broker"]["capability_manifest"]
        if not path:
            raise HumanRequired("Verified runtime capability manifest is not configured")
        path = Path(path)
        path = path if path.is_absolute() else config.directory/path
        manifest = _json(path)
        expected = trusted_approval["operational_evidence"].get("runtime_manifest_sha256")
        if expected != hashlib.sha256(path.read_bytes()).hexdigest():
            raise HumanRequired("Runtime manifest is not bound to the trusted approval")
    required = {"schema_version","source","verified","account_alias","environment","effective_at","expires_at",
                "credentials","calendar","ticks","costs","normalization","bootstrap","model","disclosures","rate_limit"}
    if set(manifest) != required | ({"automatic"} if automatic else set()) or manifest["schema_version"] != 1 or manifest["verified"] is not True or not manifest["source"]:
        raise HumanRequired("Runtime manifest contract/source is incomplete")
    now = clock()
    if (manifest["account_alias"] != config.app["app"]["account_alias"] or
            manifest["environment"] != config.app["broker"]["environment"] or
            not aware_time(manifest["effective_at"]) <= now < aware_time(manifest["expires_at"])):
        raise HumanRequired("Runtime manifest account/environment/validity mismatch")
    if config.mode == "live" and manifest["environment"] != "real" or config.mode == "broker_demo" and manifest["environment"] != "demo":
        raise HumanRequired("Runtime mode/environment mismatch")
    for section in ("calendar","ticks"):
        if manifest[section].get("verified") is not True or not manifest[section].get("source"):
            raise HumanRequired("Unverified "+section+" source")
    if not automatic:
        calendar_reference = config.app["market"]["calendar_manifest"]
        if not calendar_reference:
            raise HumanRequired("Calendar manifest reference is not configured")
        calendar_path = Path(calendar_reference)
        calendar_value = _json(calendar_path if calendar_path.is_absolute() else config.directory/calendar_path)
        if digest(calendar_value.get("calendar",calendar_value)) != digest(manifest["calendar"]):
            raise HumanRequired("Calendar reference differs from approved runtime calendar")
    bars = manifest["normalization"]["bars"]
    if (not automatic and bars.get("consistent_ohlc_verified") is not True) or not bars.get("source") or bars.get("price_returns_only") is not True:
        raise HumanRequired("Point-in-time OHLC/index adjustment basis is unverified")
    if type(manifest["normalization"]["orders"].get("day_order_fill_session_verified")) is not bool:
        raise HumanRequired("Day-order fill-session verification must be an explicit boolean")
    quote_fields = manifest["normalization"]["quote"]
    if quote_fields.get("transport", "rest") not in {"rest", "websocket"}:
        raise HumanRequired("Unsupported quote transport")
    if quote_fields.get("transport") == "websocket":
        expected = {"session_date":"BSOP_DATE", "observed_time":"STCK_CNTG_HOUR",
                    "bid":"BIDP1", "ask":"ASKP1", "bid_quantity":"BIDP_RSQN1", "ask_quantity":"ASKP_RSQN1"}
        if not quote_fields.get("source") or any(quote_fields.get(key) != value for key,value in expected.items()):
            raise HumanRequired("Unverified H0STCNT0 field mapping")
    costs = CostSchedule.model_validate_json(canonical(manifest["costs"]))
    if not costs.verified or costs.synthetic or costs.account_alias != manifest["account_alias"] or not costs.effective_at <= now < costs.expires_at:
        raise HumanRequired("External cost contract is unverified or out of scope")
    bootstrap = manifest["bootstrap"]
    if bootstrap.get("ownership_verified") is not True or not bootstrap.get("source"):
        raise HumanRequired("Strategy/manual ownership bootstrap is unverified")
    policy = config.research if config.mode != "live" else config.data["strategy"]["strategy"]["live_mandate"]["accepted_risk_policy"]
    if not policy or not 0 <= _decimal(bootstrap["strategy_cash"]) <= _decimal(policy["capital_krw"]):
        raise HumanRequired("Approved strategy cash exceeds active capital allocation")
    if _decimal(bootstrap["strategy_cash"]) == 0 and not any(bootstrap["strategy_quantities"].values()):
        raise HumanRequired("ACCOUNT_ALLOCATION_EMPTY")
    model_settings = config.app["model"]
    if not all(model_settings.get(key) for key in ("model_id","reasoning_effort","auth_mode")):
        raise HumanRequired("Approved runtime model/effort/auth mode is unset")
    executable = shutil.which(model_settings["executable"])
    if not executable:
        raise HumanRequired("Approved model executable is missing")
    executable_hash = hashlib.sha256(Path(executable).read_bytes()).hexdigest()
    isolation = manifest["model"]
    if isolation.get("isolation_verified") is not True or isolation.get("executable_sha256") != executable_hash or not isolation.get("source"):
        raise HumanRequired("Model isolation evidence does not match the executable")
    if manifest["credentials"] != {"managed_token": True}:
        raise HumanRequired("Runtime requires the managed KIS token cache contract")
    environment = load_secrets(config.directory) if env is None else env
    def secret(name):
        if not isinstance(name,str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*",name) or not environment.get(name):
            raise HumanRequired("An explicitly named runtime credential is unavailable")
        return environment[name]
    # Private file access occurs only after source, policy and authorization checks.
    account_reference = secret(config.app["broker"]["account_ref_env"])
    if not re.fullmatch(r"\d{8}-\d{2}",account_reference):
        raise HumanRequired("Account reference must contain approved account/product components")
    account,product = account_reference.split("-")
    credentials = KisCredentials(account=account,product=product,app_key=secret(config.app["broker"]["app_key_env"]),
        app_secret=secret(config.app["broker"]["app_secret_env"]),token="")
    def authorize(operation,_environment=None):
        capability = {"broker_auth":"broker_auth","broker_read":"account_read","market_read":"market_read","disclosure_read":"disclosure_read",
                      "broker_write":"live_orders" if config.mode == "live" else "demo_orders"}[operation]
        config.assert_current()
        config.require_external(capability,trusted_approval)
    if kis is None:
        origins = {BASE_URLS[manifest["environment"]],MASTER_ORIGIN}
        kis_transport = kis_transport or http_transport(allowed_origins=origins,network_enabled=True)
        rate = manifest["rate_limit"]
        if rate.get("verified") is not True or not rate.get("source"):
            raise HumanRequired("Approved rate/monitor budget is unavailable")
        kis_transport = PriorityTransport(kis_transport,minimum_interval_seconds=rate["minimum_interval_seconds"],
                                          maximum_queue_seconds=rate["maximum_queue_seconds"])
        tokens = KisTokenCache(environment=manifest["environment"], app_key=credentials.app_key,
            app_secret=credentials.app_secret, path=config.state_dir/"kis-token.json", transport=kis_transport,
            mode=config.mode, authorize=authorize, clock=clock)
        kis = KisAdapter(environment=manifest["environment"],credentials=credentials,transport=kis_transport,
            mode=config.mode,authorize=authorize,token_provider=tokens,clock=clock,ws_connector=ws_connector)
    if dart is None:
        dart = DartAdapter(api_key=secret(config.app["market"]["dart_key_env"]),mode=config.mode,authorize=authorize,
            official_ir_domains=config.app["market"]["official_ir_domains"],
            transport=dart_transport or http_transport(allowed_origins={ORIGIN,*("https://"+host for host in config.app["market"]["official_ir_domains"])},network_enabled=True))
    state = state or RuntimeState(config.state_dir/"state.sqlite")
    def persist_circuit(circuit):
        with state.lock:
            state.data["circuit"] = circuit
            state.save(("circuit",))
    def model_authorize(_operation,model,effort,auth_mode):
        config.require_external("model_call",trusted_approval)
        if (model,effort,auth_mode) != (model_settings["model_id"],model_settings["reasoning_effort"],model_settings["auth_mode"]):
            raise HumanRequired("Runtime model identity changed")
    codex = CodexAdapter(executable=executable,model_id=model_settings["model_id"],reasoning_effort=model_settings["reasoning_effort"],
        auth_mode=model_settings["auth_mode"],auth_home=secret(isolation["auth_home_env"]),mode=config.mode,authorize=model_authorize,
        timeout_seconds=model_settings["timeout_seconds"],transient_retries=model_settings["transient_retries"],
        retry_delay_seconds=model_settings["retry_delay_seconds"],schema_repair_attempts=model_settings["schema_repair_attempts"],
        runner=model_runner,circuit_state=state.data["circuit"],persist_circuit=persist_circuit,
        isolation_probe=lambda current: str(Path(current).resolve()) == str(Path(executable).resolve()) and hashlib.sha256(Path(current).read_bytes()).hexdigest() == executable_hash)
    runtime = ExternalRuntime(config,trusted_approval,manifest,kis,dart,codex,state,clock=clock)
    if config.mode == "paper":
        runtime.broker = PaperBrokerPort(runtime)
    elif config.mode == "shadow":
        runtime.broker = ShadowBrokerPort(kis,manifest,state,clock=clock)
    if store is not None:
        runtime.broker.bind_store(store)
    try:
        bundle = runtime.refresh()
    except BaseException:
        runtime.close()
        raise
    return bundle,runtime.broker,runtime.decide,runtime.refresh
