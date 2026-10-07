"""Fake broker data only: whole-account cash and live preflight contracts."""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from danta.adapters import AdapterError, FetchResult, HttpResponse
from danta.adapters.kis import BrokerResult, KisAdapter, KisCredentials
from danta.execution import Executor, FixtureBroker, OrderIntent
from danta.runtime import ExternalRuntime, KisBrokerPort
from danta.config import AccountObservationIncomplete, HumanRequired
from danta.reporting import render_notification
from danta.store import Store


NOW = datetime(2026, 9, 21, 2, tzinfo=timezone.utc)


class AccountFixture:
    environment = "real"

    def __init__(self):
        self.now = NOW
        self.summary = {"prvs_rcdl_excc_amt":"1000", "tot_evlu_amt":"1200", "evlu_amt_smtl_amt":"200",
                        "nass_amt":"1200", "tot_loan_amt":"0", "cma_evlu_amt":"0"}
        self.positions = ({"pdno":"000001", "hldg_qty":"2", "ord_psbl_qty":"2", "prpr":"100", "evlu_amt":"200"},)
        self.orders, self.reservations, self.cancelable = [], [], []
        self.power = {"nrcvb_buy_amt":"100", "nrcvb_buy_qty":"1", "max_buy_amt":"100000"}
        self.writes, self.power_symbol = [], None

    def result(self, rows):
        return FetchResult(tuple(rows), "COMPLETE", NOW)

    def read_account(self):
        return FetchResult(self.positions,
                           "COMPLETE", self.now, metadata={"summaries":[[dict(self.summary)], [dict(self.summary)]]})

    def read_orders(self, *_): return self.result(self.orders)
    def read_cancelable_orders(self): return self.result(self.cancelable)
    def read_reservations(self, *_): return self.result(self.reservations)
    def read_daily_costs(self, *_): return self.result([])

    def read_buying_power(self, ticker, price):
        self.power_symbol = ticker
        return self.power

    def submit(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        return BrokerResult("ACKNOWLEDGED", "123", "001")

    def cancel(self, *args, **kwargs):
        self.writes.append((args, kwargs))
        return BrokerResult("ACKNOWLEDGED", "124", "001")


def order(**changes):
    return {"pdno":"000001", "ord_dt":"20260918", "odno":"123", "orgn_odno":"0", "ord_gno_brno":"001",
            "excg_id_dvsn_cd":"KRX", "ord_dvsn_cd":"00", "sll_buy_dvsn_cd":"02", "ord_qty":"2",
            "tot_ccld_qty":"0", "tot_ccld_amt":"0", "cncl_cfrm_qty":"0", "rmn_qty":"2", "rjct_qty":"0", **changes}


class KisAccountContracts(unittest.TestCase):
    def test_midnight_rollover_observations_do_not_become_permanent_cash_adjustments(self):
        port = self.port()
        port.clock = lambda: port.adapter.now
        port.adapter.positions = ()
        port.adapter.summary.update(tot_evlu_amt='1000', nass_amt='1000', evlu_amt_smtl_amt='0')
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory)/'state.sqlite', mode='offline',
                account_identity=directory, initial_cash=D(1000)) as store:
            port.bind_store(store)
            executor = Executor(store, port, mode='live', authorize=lambda *_: None, preflight=lambda *_: None)
            runtime = SimpleNamespace(broker=port, config=SimpleNamespace(require_external=lambda *_: None),
                                      approval={}, clock=lambda: port.adapter.now)
            executor.reconcile(ExternalRuntime._account(runtime)[0])
            prior = [{'observed_at': NOW.isoformat(), 'amount': '-200', 'classification': 'UNCLASSIFIED_CASH_FLOW'}]
            store.set('unclassified_cash_adjustments', prior)
            store.set('performance_uncertain', True)
            for minute, cash in ((4, '537'), (61, '1000'), (69, '1000')):
                port.adapter.now = NOW.replace(hour=15, minute=0) + timedelta(minutes=minute)
                port.adapter.summary.update(prvs_rcdl_excc_amt=cash, tot_evlu_amt=cash,
                                            nass_amt=cash, dnca_tot_amt='500', nxdy_excc_amt='750')
                with self.assertRaises(AccountObservationIncomplete):
                    ExternalRuntime._account(runtime)
                self.assertEqual(store.get('cash_krw'), '1000')
                self.assertEqual(store.get('unclassified_cash_adjustments'), prior)
                self.assertFalse(store.get('account_cash_reconciled'))
            self.assertEqual(store.get('account_diagnostics')[0]['cash_fields']['nxdy_excc_amt'], '750')
            port.adapter.now = NOW.replace(hour=16, minute=10)
            executor.reconcile(ExternalRuntime._account(runtime)[0])
            self.assertTrue(store.get('account_cash_reconciled'))
            self.assertEqual(store.get('unclassified_cash_adjustments'), prior)
            self.assertTrue(store.get('performance_uncertain'))
            self.assertEqual(store.get('account_cash_observation')['cash_fields']['dnca_tot_amt'], '500')
            # A persistent difference outside the rollover window still blocks new risk.
            port.adapter.now += timedelta(minutes=1)
            port.adapter.summary.update(prvs_rcdl_excc_amt='900', tot_evlu_amt='900', nass_amt='900')
            executor.reconcile(ExternalRuntime._account(runtime)[0])
            self.assertEqual(store.get('unclassified_cash_adjustments')[-1]['amount'], '-100')
            self.assertTrue(store.get('performance_uncertain'))
            self.assertEqual(port.adapter.writes, [])

    def test_identical_history_rows_are_counted_once_but_conflicts_remain_incomplete(self):
        port = self.port()
        original = order()
        port.adapter.orders = [original, dict(original)]
        census = port.census()
        self.assertTrue(census['complete'])
        self.assertEqual(len(census['orders']), 1)
        for changed in ({'ord_gno_brno': '002'}, {'tot_ccld_qty': '1', 'tot_ccld_amt': '100', 'rmn_qty': '1'},
                        {'orgn_odno': '999'}, {'provider_extra': 'different'}):
            with self.subTest(changed=changed):
                port.adapter.orders = [original, {**original, **changed}]
                census = port.census()
                self.assertFalse(census['complete'])
                self.assertEqual(census['errors'], ['DUPLICATE_BROKER_ORDER'])
                self.assertEqual(census['orders'], [])
        for changed in ({'ord_dt': '20260917'}, {'excg_id_dvsn_cd': 'NXT'}):
            port.adapter.orders = [original, {**original, **changed}]
            census = port.census()
            self.assertTrue(census['complete'])
            self.assertEqual(len(census['orders']), 2)

    def test_buying_power_failure_reaches_account_order_and_operator_diagnostics(self):
        port = self.port()
        port.latest_bundle = SimpleNamespace(quotes={'KRX:000002':SimpleNamespace(observed_at=NOW)},
            calendar=SimpleNamespace(active=lambda _:SimpleNamespace(closes_at=NOW+timedelta(hours=1))))
        error = AdapterError('TRANSIENT_FAILURE', diagnostic={'http_status':500, 'provider_code':'EGW00215',
            'provider_message':'요청 처리 중 오류', 'requested_at':NOW.isoformat(), 'elapsed_seconds':1.2})
        with patch.object(port.adapter, 'read_buying_power', side_effect=error):
            account = port.snapshot()
            result = port.submit({'instrument_id':'KRX:000002', 'side':'BUY', 'quantity':1, 'limit_price':'100'})
        self.assertFalse(account['complete'])
        self.assertEqual(result['status'], 'NOT_SENT')
        self.assertEqual(port.adapter.writes, [])
        port.state.known_order = lambda *_: {'side':'BUY'}
        with patch.object(port.adapter, 'read_cancelable_orders', return_value=FetchResult((), 'FETCH_FAILED', NOW,
            metadata={'error':error.code, **error.diagnostic})):
            canceled = port.cancel({'broker_id':'123', 'namespace':'fake', 'remaining_quantity':1, 'metadata':{'organization':'001'}})
        self.assertEqual(canceled['status'], 'NOT_SENT')
        self.assertEqual(canceled['diagnostics'][0]['provider_code'], 'EGW00215')
        self.assertEqual(canceled['diagnostics'][0]['endpoint'], 'inquire-psbl-rvsecncl')
        for value in (account, result):
            detail = value['diagnostics'][0]
            self.assertEqual(detail['reason'], 'NO_MARGIN_BUYING_POWER_UNVERIFIED')
            self.assertEqual(detail['detail'], 'TRANSIENT_FAILURE')
            text = render_notification({'kind':'MONITOR_DEGRADED', 'diagnostics':value['diagnostics']})
            for item in ('미수 없는 매수 가능 금액 조회', 'HTTP 500', 'EGW00215', '요청 처리 중 오류'):
                self.assertIn(item, text)
        for power, code, kind in (({}, 'PROVIDER_FIELD_MISSING', 'KeyError'),
                                  ({'nrcvb_buy_amt':'not-a-number'}, 'PROVIDER_FIELD_UNVERIFIED', 'InvalidOperation')):
            port.adapter.power = power
            detail = port.snapshot()['diagnostics'][0]
            self.assertEqual((detail['field'], detail['detail'], detail['error_type']), ('nrcvb_buy_amt', code, kind))
        port.adapter.power = {'nrcvb_buy_amt':'0', 'nrcvb_buy_qty':'0'}
        self.assertTrue(port.snapshot()['complete'], 'A verified zero balance is not a lookup failure')

    def test_failed_account_refresh_records_details_before_monitor_runs(self):
        port = self.port()
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory)/'state.sqlite', mode='offline',
                account_identity=directory, initial_cash=D(1000)) as store:
            port.bind_store(store)
            runtime = SimpleNamespace(broker=port, config=SimpleNamespace(require_external=lambda *_:None),
                                      approval={}, clock=lambda:NOW)
            error = AdapterError('TRANSIENT_FAILURE', diagnostic={'http_status':500, 'provider_code':'EGW00215'})
            with patch.object(port.adapter, 'read_buying_power', side_effect=error), self.assertRaises(AccountObservationIncomplete) as raised:
                ExternalRuntime._account(runtime)
            self.assertEqual(raised.exception.diagnostics[0]['provider_code'], 'EGW00215')
            journal = store.read("SELECT payload FROM journal WHERE kind='ACCOUNT_INCOMPLETE'")
            self.assertEqual(len(journal), 1)
            self.assertEqual(json.loads(journal[0][0])['diagnostics'], raised.exception.diagnostics)
            self.assertEqual(store.read('SELECT COUNT(*) FROM outbox')[0][0], 0)
            with patch.object(port.adapter, 'read_buying_power', side_effect=AdapterError('AUTH_FAILED')), \
                    self.assertRaises(HumanRequired) as authorization:
                ExternalRuntime._account(runtime)
            self.assertNotIsInstance(authorization.exception, AccountObservationIncomplete)


    def port(self):
        adapter = AccountFixture()
        manifest = {"account_alias":"FAKE_ACCOUNT", "bootstrap":{"whole_account":True, "orders_since":"2026-09-01",
                    "external_quantities":{}, "external_order_keys":[], "baseline_orders":{}}}
        state = SimpleNamespace(known_order=lambda *_:None)
        return KisBrokerPort(adapter, manifest, state, clock=lambda:NOW)

    def test_no_margin_power_and_cancelable_quantity_are_checked_before_mutations(self):
        port = self.port()
        port.latest_bundle = SimpleNamespace(quotes={"KRX:000002":SimpleNamespace(observed_at=NOW)},
                                calendar=SimpleNamespace(active=lambda _:SimpleNamespace(closes_at=NOW+timedelta(hours=1))))
        intent = {"instrument_id":"KRX:000002", "side":"BUY", "quantity":2, "limit_price":"100"}
        self.assertEqual(port.submit(intent)["reason"], "NO_MARGIN_BUYING_POWER_EXCEEDED")
        self.assertEqual(port.adapter.power_symbol, "000002")
        self.assertEqual(port.adapter.writes, [])
        intent["quantity"] = 1
        self.assertEqual(port.submit(intent)["status"], "ACKNOWLEDGED")
        port.state.known_order = lambda *_:{"side":"BUY"}
        request = {"namespace":"fake", "broker_id":"123", "remaining_quantity":2, "metadata":{"organization":"001"}}
        port.adapter.cancelable = [{"odno":"123", "ord_gno_brno":"001", "psbl_qty":"1"}]
        self.assertEqual(port.cancel(request)["reason"], "CANCELABLE_QUANTITY_CHANGED")
        self.assertEqual(len(port.adapter.writes), 1)
        request["remaining_quantity"] = 1
        self.assertEqual(port.cancel(request)["status"], "ACKNOWLEDGED")

    def test_broker_net_buying_power_is_read_after_its_order_snapshot(self):
        port = self.port()
        calls = []
        def orders(*_):
            calls.append("orders")
            return port.adapter.result([])
        def power(*_):
            calls.append("power")
            return port.adapter.power
        with patch.object(port.adapter, "read_orders", side_effect=orders), \
                patch.object(port.adapter, "read_buying_power", side_effect=power):
            account = port.snapshot()
        self.assertTrue(account["complete"] and account["broker_cash_reserves_orders"])
        self.assertLess(calls.index("orders"), calls.index("power"))
        self.assertEqual(account["broker_available_cash"], port.adapter.power["nrcvb_buy_amt"])

    def test_census_does_not_sum_repeated_summaries_or_replay_closed_baselines(self):
        port = self.port()
        port.adapter.orders = [order()]
        census = port.census()
        self.assertTrue(census["complete"], census)
        self.assertEqual(census["economic_cash"], "1000")
        self.assertEqual(census["orders"][0]["state"], "EXPIRED")
        row = census["orders"][0]
        port.manifest["bootstrap"]["baseline_orders"] = {row["key"]:row["fingerprint"]}
        self.assertTrue(port.snapshot()["complete"])
        port.adapter.orders[0]["tot_ccld_qty"] = "1"
        port.adapter.orders[0]["tot_ccld_amt"] = "100"
        port.adapter.orders[0]["rmn_qty"] = "1"
        self.assertIn("UNALLOCATED_BROKER_ORDER", port.snapshot()["errors"])
        port.adapter.orders = []
        port.adapter.reservations = [{"rsvn_ord_seq":"1", "rsvn_ord_ord_dt":"20260922", "rsvn_end_dt":"",
                                      "ord_rsvn_qty":"2", "tot_ccld_qty":"0"}]
        self.assertIn("ACTIVE_RESERVATION_ORDER", port.snapshot()["errors"])

    def test_expired_day_orders_and_old_reservations_do_not_block_adoption(self):
        port = self.port()
        port.adapter.orders = [order(rmn_qty="0")]
        port.adapter.reservations = [{"rsvn_ord_ord_dt":"20260818", "rsvn_ord_rcit_dt":"20260816",
            "rsvn_end_dt":"", "prcs_rslt":"미처리", "cncl_ord_dt":""}]
        census = port.census()
        self.assertTrue(census["complete"], census)
        self.assertEqual(census["orders"][0]["state"], "EXPIRED")
        self.assertEqual(census["reservations"], [])
        for changes in ({"ord_dt":"20260921"}, {"tot_ccld_qty":"3", "tot_ccld_amt":"300"},
                        {"rsvn_ord_end_dt":"20260922"}):
            port.adapter.orders = [order(rmn_qty="0", **changes)]
            self.assertFalse(port.census()["complete"], changes)
        period = {"rsvn_ord_ord_dt":"20260918", "rsvn_end_dt":"20260922", "ord_rsvn_qty":"2", "tot_ccld_qty":"0"}
        self.assertTrue(port._active_reservation(period, NOW.date()))
        self.assertFalse(port._active_reservation(dict(period, cncl_ord_dt="20260921"), NOW.date()))

    def test_conflicting_cancel_aliases_remaining_rejection_and_venue_are_rejected(self):
        port = self.port()
        for changes in ({"cnc_cfrm_qty":"1"}, {"rmn_qty":"3"}, {"rjct_qty":"3"}, {"excg_id_dvsn_Cd":"NXT"}):
            port.adapter.orders = [order(**changes)]
            self.assertFalse(port.census()["complete"], changes)
        port.adapter.orders = [order(rjct_qty="2", rmn_qty="0")]
        self.assertEqual(port.census()["orders"][0]["state"], "REJECTED")
        port.adapter.orders = [order(tot_ccld_qty="1", tot_ccld_amt="100", cncl_cfrm_qty="1", rmn_qty="0")]
        self.assertEqual(port.census()["orders"][0]["state"], "PARTIAL_CANCELED")

    def test_wire_all_exchange_and_reservation_200_cursor_and_market_power(self):
        calls = []
        def transport(method, url, headers, body, timeout):
            params = parse_qs(urlsplit(url).query, keep_blank_values=True)
            calls.append((urlsplit(url).path, params))
            data, response_headers = {"rt_cd":"0", "output1":[], "output":{}}, {}
            if "order-resv-ccnl" in url:
                data["output"] = []
                if not params["CTX_AREA_FK200"][0]:
                    data.update(ctx_area_fk200="next", ctx_area_nk200="key")
                    response_headers["tr_cont"] = "M"
            return HttpResponse(200, json.dumps(data).encode(), response_headers)
        transport.fixture_only = True
        adapter = KisAdapter(environment="real", credentials=KisCredentials("00000000", "00", "FAKE", "FAKE", "FAKE"), transport=transport)
        adapter.read_orders(date(2026,9,1), date(2026,9,21))
        adapter.read_reservations(date(2026,9,1), date(2026,9,21))
        adapter.read_buying_power("000002", "100")
        self.assertEqual(calls[0][1]["EXCG_ID_DVSN_CD"], ["ALL"])
        self.assertEqual(calls[1][1]["PRCS_DVSN_CD"], ["0"])
        self.assertEqual(calls[2][1]["CTX_AREA_FK200"], ["next"])
        self.assertEqual(calls[-1][1]["ORD_DVSN"], ["01"])
        self.assertEqual(calls[-1][1]["PDNO"], ["000002"])

    def test_account_cash_reconciles_unknown_fees_without_allocation_or_double_debit(self):
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory)/"ledger.sqlite", mode="offline",
                account_identity=directory, initial_cash=D(1000)) as store:
            broker = FixtureBroker()
            executor = Executor(store, broker, mode="offline", authorize=lambda *_:None, preflight=lambda *_:None)
            def cash(value, fee="0"):
                return {"cash_krw":str(value), "observed_at":NOW.isoformat(), "source":"KIS:inquire-balance:prvs_rcdl_excc_amt",
                        "cost_quality":"BROKER_REPORTED", "daily_costs":{"2026-09-21":{"fee":fee, "tl_tax":"0", "loan_int":"0"}}}
            store.reconcile_account_cash(cash(1000))
            intent = OrderIntent("run", "plan", "thesis", "KRX:000001", "BUY", 1, D(100), NOW+timedelta(minutes=1),
                                 "FAKE", store.get("account_version"), "FAKE_POLICY", D(100))
            submitted = executor.submit(intent, NOW)
            broker.fill(submitted["broker_id"], 1, D(100), D(0), NOW)
            broker.orders[submitted["broker_id"]]["cumulative_fees"] = None
            snapshot = {**broker.snapshot(), "whole_account":True, "account_cash":cash(895, "5")}
            executor.reconcile(snapshot)
            self.assertEqual(store.get("cash_krw"), "895")
            self.assertFalse(store.get("costs_complete"))
            self.assertTrue(store.get("account_cash_reconciled"))
            self.assertFalse(store.get("performance_uncertain"))
            self.assertEqual(store.order(intent.id)["cumulative_fees"], "0")
            executor.reconcile(snapshot)
            self.assertEqual(store.get("cash_krw"), "895")
            self.assertEqual(store.quantity("KRX:000001"), 1)
            next_intent = OrderIntent("next", "next", "next", "KRX:000002", "BUY", 1, D(100), NOW+timedelta(minutes=1),
                                      "FAKE", store.get("account_version"), "FAKE_POLICY", D(100))
            executor._validate_state(next_intent, NOW)
            executor.reconcile(broker.snapshot())
            with self.assertRaisesRegex(ValueError, "ACTUAL_COST_RECONCILIATION_REQUIRED"):
                executor._validate_state(next_intent, NOW)
            store.reconcile_account_cash(cash(995, "5"))
            self.assertTrue(store.get("performance_uncertain"))
            store.reconcile_account_cash(cash(895, "5"))
            self.assertTrue(store.get("performance_uncertain"))

    def test_equal_late_daily_fee_does_not_classify_an_unrelated_cash_debit(self):
        with tempfile.TemporaryDirectory() as directory, Store(Path(directory)/"ledger.sqlite", mode="offline",
                account_identity=directory, initial_cash=D(1000)) as store:
            evidence = {"cash_krw":"1000", "observed_at":NOW.isoformat(), "source":"KIS:inquire-balance:prvs_rcdl_excc_amt",
                        "cost_quality":"BROKER_REPORTED", "daily_costs":{"2026-09-21":{"fee":"0", "tl_tax":"0", "loan_int":"0"}}}
            store.reconcile_account_cash(evidence)
            evidence["cash_krw"] = "995"
            store.reconcile_account_cash(evidence)
            self.assertTrue(store.get("performance_uncertain"))
            evidence["daily_costs"]["2026-09-21"]["fee"] = "5"
            store.reconcile_account_cash(evidence)
            self.assertTrue(store.get("performance_uncertain"))
            self.assertEqual(store.get("cash_krw"), "995")
            self.assertEqual(len(store.get("unclassified_cash_adjustments")), 2)
            # The reverse reporting order also lacks a transaction identity.
            evidence["daily_costs"]["2026-09-21"]["fee"] = "10"
            store.reconcile_account_cash(evidence)
            self.assertTrue(store.get("performance_uncertain"))
            evidence["cash_krw"] = "990"
            store.reconcile_account_cash(evidence)
            self.assertTrue(store.get("performance_uncertain"))
            self.assertEqual(store.get("cash_krw"), "990")


if __name__ == "__main__":
    unittest.main()
