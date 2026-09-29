"""Synthetic streaming-to-domain contracts; no network or real credentials."""
import hashlib
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from danta.adapters import AdapterError, FetchResult
from danta.config import HumanRequired
from danta.runtime import ExternalRuntime, RuntimeState
from tests.contract import test_runtime as contracts


class QuoteCache:
    environment = "demo"

    def __init__(self, now):
        self.now = now
        self.subscriptions = []
        self.closed = False
        self.raw = {"BSOP_DATE": now.strftime("%Y%m%d"), "STCK_CNTG_HOUR": now.strftime("%H%M%S"),
                    "BIDP1": "9999", "ASKP1": "10000", "BIDP_RSQN1": "5", "ASKP_RSQN1": "6",
                    "MARKET_CLS_CODE": "2"}

    def stream_quote(self, ticker):
        return FetchResult((dict(self.raw),), "COMPLETE", self.now)

    def subscribe_quotes(self, tickers):
        self.subscriptions.append(tickers)

    def close(self):
        self.closed = True


class RuntimeStreamContracts(unittest.TestCase):
    def test_stream_book_uses_its_own_time_and_source(self):
        raw = {**self.kis.raw,'BSOP_HOUR':self.now.strftime('%H%M%S')}
        raw['STCK_CNTG_HOUR'] = (self.now-timedelta(seconds=30)).strftime('%H%M%S')
        with patch.object(self.kis,'stream_quote',return_value=FetchResult((raw,),'COMPLETE',self.now,metadata={'tr_id':'H0STASP0'})):
            quote = self.runtime._quote(self.instrument)
        self.assertEqual(quote.observed_at,self.now)
        self.assertEqual(quote.source,'KIS:H0STASP0')

    def setUp(self):
        self.fixture = contracts.ExternalRuntimeContracts(methodName="runTest")
        self.fixture.setUp()
        self.now = self.fixture.now
        self.kis = QuoteCache(self.now)
        self.fixture.manifest["normalization"]["quote"] = {
            "transport": "websocket", "source": "SYNTHETIC_CURRENT_KIS_LAYOUT",
            "session_date": "BSOP_DATE", "observed_time": "STCK_CNTG_HOUR",
            "bid": "BIDP1", "ask": "ASKP1", "bid_quantity": "BIDP_RSQN1", "ask_quantity": "ASKP_RSQN1"}
        self.runtime = ExternalRuntime(self.fixture.config, self.fixture.approval, self.fixture.manifest,
            self.kis, None, None, RuntimeState(self.fixture.base / "stream-state.sqlite"), clock=lambda: self.now)
        self.instrument = SimpleNamespace(instrument_id="KRX:000001")

    def tearDown(self):
        self.runtime.close()
        self.fixture.tearDown()

    def test_provider_date_and_original_reception_time_reach_quote(self):
        observed = self.now - timedelta(seconds=2)
        received = self.now - timedelta(seconds=1)
        self.kis.raw["STCK_CNTG_HOUR"] = observed.strftime("%H%M%S")
        self.kis.now = received
        quote = self.runtime._quote(self.instrument)
        self.assertEqual(quote.observed_at, observed)
        self.assertEqual(quote.received_at, received)
        self.assertEqual(self.runtime.quote_depth[self.instrument.instrument_id], {"bid": 5, "ask": 6})

    def test_non_regular_market_rejected_even_with_current_timestamp(self):
        for market in ("1", "3", "5", None):
            with self.subTest(market=market):
                self.kis.raw["MARKET_CLS_CODE"] = market
                with self.assertRaisesRegex(ValueError, "QUOTE_MARKET_IS_NOT_REGULAR"):
                    self.runtime._quote(self.instrument)

    def test_stale_trade_uses_fresh_book_without_relabeling_its_timestamp(self):
        book = {'session_date': self.now.date().isoformat(), 'asking': {
            'aspr_acpt_hour': self.now.strftime('%H%M%S'), 'bidp1': '9998', 'askp1': '10001',
            'bidp_rsqn1': '7', 'askp_rsqn1': '8'}}
        result = FetchResult((book,), 'COMPLETE', self.now, metadata={'source': 'SYNTHETIC_REST_BOOK'})
        with patch.object(self.kis, 'stream_quote', side_effect=AdapterError('STREAM_QUOTE_STALE')), \
                patch.object(self.kis, 'poll_quote', return_value=result, create=True) as poll:
            quote = self.runtime._quote(self.instrument)
            self.assertEqual(str(quote.bid), '9998')
            self.assertEqual(quote.source, 'SYNTHETIC_REST_BOOK')
            self.assertEqual(quote.observed_at, self.now)
            book['asking']['aspr_acpt_hour'] = (self.now - timedelta(seconds=6)).strftime('%H%M%S')
            with self.assertRaisesRegex(ValueError, 'STALE_QUOTE'):
                self.runtime._quote(self.instrument)
            # The same fallback keeps the provider timestamp when protection permits 60 seconds.
            self.assertEqual(self.runtime._quote(self.instrument, maximum_age_seconds=60).observed_at,
                             self.now - timedelta(seconds=6))
            book['asking']['aspr_acpt_hour'] = (self.now - timedelta(seconds=61)).strftime('%H%M%S')
            with self.assertRaisesRegex(ValueError, 'STALE_QUOTE'):
                self.runtime._quote(self.instrument, maximum_age_seconds=60)
            book['asking']['aspr_acpt_hour'] = (self.now + timedelta(seconds=1)).strftime('%H%M%S')
            with self.assertRaisesRegex(ValueError, 'quote observation follows reception'):
                self.runtime._quote(self.instrument)
            self.assertEqual(poll.call_count, 5)
        with patch.object(self.kis, 'stream_quote', side_effect=AdapterError('STREAM_SESSION_INVALID')), \
                patch.object(self.kis, 'poll_quote', create=True) as poll:
            with self.assertRaisesRegex(AdapterError, 'STREAM_SESSION_INVALID'):
                self.runtime._quote(self.instrument)
            poll.assert_not_called()

    def test_aftermarket_time_cannot_extend_regular_calendar(self):
        self.kis.raw["STCK_CNTG_HOUR"] = "160001"
        self.now = self.now.replace(hour=16, minute=0, second=2)
        self.kis.now = self.now
        with self.assertRaisesRegex(ValueError, "QUOTE_OBSERVATION_OUTSIDE_SESSION"):
            self.runtime._quote(self.instrument)

    def test_stale_stream_moves_directly_to_book_refresh_without_initial_tick_wait(self):
        with patch.object(self.kis, 'stream_quote', side_effect=AdapterError('STREAM_QUOTE_STALE')), \
                patch('danta.runtime.time.sleep', side_effect=AssertionError('unnecessary wait')):
            self.runtime._wait_for_stream_quotes([self.instrument])

    def test_fresh_stream_tick_wins_over_old_rest_book_returned_later(self):
        stale_book = FetchResult(({'session_date':self.now.date().isoformat(), 'asking':{
            'aspr_acpt_hour':(self.now-timedelta(seconds=20)).strftime('%H%M%S'),
            'bidp1':'1','askp1':'2','bidp_rsqn1':'1','askp_rsqn1':'1'}},),
            'COMPLETE', self.now, metadata={'source':'SYNTHETIC_REST_BOOK'})
        fresh = FetchResult((dict(self.kis.raw),), 'COMPLETE', self.now)
        with patch.object(self.kis, 'stream_quote', side_effect=[AdapterError('STREAM_QUOTE_STALE'),fresh]), \
                patch.object(self.kis, 'poll_quote', return_value=stale_book, create=True):
            quote = self.runtime._quote(self.instrument)
        self.assertEqual(quote.observed_at, self.now)
        self.assertEqual(str(quote.bid), '9999')

    def test_subscription_limit_preserves_protection_and_reports_all_excess_candidates(self):
        protected = {"KRX:000001": 3}
        account = {"strategy_quantities": protected, "orders": []}
        candidates = {f"KRX:{number:06}" for number in range(2, 43)}
        excluded = self.runtime._subscribe_quotes(account, candidates)
        self.assertEqual(excluded, candidates)
        self.assertEqual(self.kis.subscriptions[-1], ["000001"])
        self.assertEqual(self.runtime.entry_quote_symbols, candidates)
        excluded = self.runtime._subscribe_quotes(account, {"KRX:000002"})
        self.assertEqual(excluded, set())
        self.assertEqual(self.kis.subscriptions[-1], ["000001", "000002"])

    def test_more_than_41_protected_symbols_is_explicitly_unavailable(self):
        holdings = {f"KRX:{number:06}": 1 for number in range(1, 43)}
        excluded = self.runtime._subscribe_quotes({"strategy_quantities": holdings, "orders": []})
        self.assertEqual(excluded, set(holdings))
        self.assertEqual(self.kis.subscriptions[-1], [])

    def test_closed_market_releases_subscriptions(self):
        self.now = self.now.replace(hour=16, minute=0)
        self.runtime._subscribe_quotes({"strategy_quantities": {"KRX:000001": 1}, "orders": []})
        self.assertEqual(self.kis.subscriptions[-1], [])

    def test_factory_rejects_unverified_mapping_before_network_setup(self):
        self.fixture.manifest["normalization"]["quote"]["session_date"] = None
        self.fixture._save_manifest()
        self.fixture.approval["operational_evidence"]["runtime_manifest_sha256"] = hashlib.sha256(
            (self.fixture.base / "manifest.json").read_bytes()).hexdigest()
        with self.assertRaisesRegex(HumanRequired, "H0STCNT0 field mapping"):
            self.fixture._factory()
