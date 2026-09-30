"""Injected transport only. All account numbers/credentials/data here are FAKE."""
import hashlib
import importlib.util
import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import yaml

from danta.adapters import AdapterError, FetchResult, HttpResponse
from danta.adapters.kis import KisAdapter, KisCredentials
from danta.application import Application
from danta.config import HumanRequired, load_config, canonical, aware_time
from danta.runtime import KisBrokerPort, RuntimeState, build_external_runtime

ROOT = Path(__file__).resolve().parents[2]
helpers_spec = importlib.util.spec_from_file_location("runtime_synthetic_helpers", ROOT/"tests/unit/test_strategy.py")
helpers = importlib.util.module_from_spec(helpers_spec)
helpers_spec.loader.exec_module(helpers)


class FixtureTransport:
    fixture_only = True

    def __init__(self,now,bars,index_bars):
        self.now,self.bars,self.index_bars = now,bars,index_bars
        self.calls = []
        self.dart_error = self.quote_error = None
        self.quote_observed_at = now
        self.account_positions = []

    def __call__(self,method,url,headers=None,body=None,timeout=15):
        self.calls.append((method,urlsplit(url).path))
        path = urlsplit(url).path
        if path.endswith("/oauth2/tokenP"):
            return HttpResponse(200,json.dumps({"access_token":"FAKE_TOKEN_NOT_A_CREDENTIAL",
                "access_token_token_expired":(self.now + timedelta(hours=12)).astimezone(
                    ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')}).encode())
        if path.endswith("list.json"):
            if self.dart_error:
                raise self.dart_error
            return HttpResponse(200,json.dumps({"status":"013"}).encode())
        if path.endswith("inquire-asking-price-exp-ccn"):
            if self.quote_error:
                raise self.quote_error
            return HttpResponse(200,json.dumps({"rt_cd":"0","output1":{
                "aspr_acpt_hour":self.quote_observed_at.strftime("%H%M%S"),"bidp1":"9999","askp1":"10000",
                "bidp_rsqn1":"1","askp_rsqn1":"1"}}).encode())
        if path.endswith("inquire-price"):
            return HttpResponse(200,json.dumps({"rt_cd":"0","output":{
                "stck_bsop_date":self.quote_observed_at.strftime("%Y%m%d")}}).encode())
        if path.endswith("inquire-daily-itemchartprice"):
            data = [{"stck_bsop_date":b.session_id.replace("-",""),"stck_hgpr":str(b.high),"stck_lwpr":str(b.low),
                     "stck_clpr":str(b.close),"acml_tr_pbmn":str(b.turnover)} for b in self.bars]
            return HttpResponse(200,json.dumps({"rt_cd":"0","output2":data}).encode())
        if path.endswith("inquire-daily-indexchartprice"):
            data = [{"stck_bsop_date":b.session_id.replace("-",""),"bstp_nmix_hgpr":str(b.high),"bstp_nmix_lwpr":str(b.low),
                     "bstp_nmix_prpr":str(b.close),"acml_tr_pbmn":str(b.turnover)} for b in self.index_bars]
            return HttpResponse(200,json.dumps({"rt_cd":"0","output2":data}).encode())
        if path.endswith("inquire-balance"):
            return HttpResponse(200,json.dumps({"rt_cd":"0","output1":self.account_positions,"output2":[{}]}).encode())
        if path.endswith("inquire-psbl-order"):
            return HttpResponse(200,json.dumps({"rt_cd":"0","output":{"ord_psbl_cash":"10000000"}}).encode())
        if path.endswith("inquire-daily-ccld"):
            return HttpResponse(200,json.dumps({"rt_cd":"0","output1":[],"output2":{}}).encode())
        raise AssertionError("Unexpected fixture endpoint: "+path)


class ExternalRuntimeContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config_dir = self.base/"config"
        shutil.copytree(ROOT/"tests/fixtures/config",self.config_dir,ignore=shutil.ignore_patterns("secrets.yaml"))
        self.case = helpers.synthetic_case()
        profile,calendar,ticks,self.now,self.bars,self.index_bars,candidate,quote,event,costs,snapshot = self.case
        self.transport = FixtureTransport(self.now,self.bars,self.index_bars)
        app = yaml.safe_load((self.config_dir/"app.yaml").read_text())
        app["app"].update(mode="paper",account_alias="FAKE_ACCOUNT_ALIAS",state_dir=str(self.base/"state"))
        app["broker"].update(environment="demo",capability_manifest=str(self.base/"manifest.json"))
        app["market"]["calendar_manifest"] = str(self.base/"manifest.json")
        app["model"].update(model_id="FAKE_MODEL_FOR_CONTRACT_TEST",reasoning_effort="medium",auth_mode="api",executable="/usr/bin/true")
        (self.config_dir/"app.yaml").write_text(yaml.safe_dump(app,sort_keys=False))
        self.config = load_config(self.config_dir)
        self.manifest = {
            "schema_version":1,"source":"FAKE_CONTRACT_FIXTURE_NEVER_A_LIVE_APPROVAL","verified":True,
            "account_alias":"FAKE_ACCOUNT_ALIAS","environment":"demo","effective_at":"2025-01-01T00:00:00+00:00","expires_at":"2099-01-01T00:00:00+00:00",
            "credentials":{"managed_token":True},
            "calendar":{"source":"FAKE_WEEKDAY_CALENDAR","verified":True,"sessions":[s.model_dump(mode="json") for s in calendar.sessions]},
            "ticks":{"source":"FAKE_TICKS","verified":True,"bands":[["0","1"]],"effective_at":"2025-01-01T00:00:00+00:00","expires_at":"2099-01-01T00:00:00+00:00"},
            "costs":costs.model_copy(update={"account_alias":"FAKE_ACCOUNT_ALIAS","synthetic":False,"expires_at":calendar.sessions[-1].closes_at}).model_dump(mode="json"),
            "normalization":{
                "instruments":{"source":"FAKE_PROVIDER_CODES","true_codes":["Y"],"false_codes":["N"],"common_groups":["ST"],
                               "issuer_by_symbol":{"000001":"FAKE_ISSUER"},"sector_by_industry":{"1":"FAKE_SECTOR"}},
                "bars":{"source":"FAKE_ADJUSTMENT_EVIDENCE","consistent_ohlc_verified":True,"price_returns_only":True,"stock_basis":"FAKE_PRICE_ONLY","index_basis":"FAKE_PRICE_ONLY"},
                "quote":{"source":"FAKE_QUOTE_FIELD_CONTRACT","session_date":"price.stck_bsop_date","observed_time":"asking.aspr_acpt_hour","bid":"asking.bidp1","ask":"asking.askp1"},
                "account":{"resource_symbol":"000001","resource_price":"10000","available_cash":"ord_psbl_cash","symbol":"pdno","quantity":"hldg_qty","sellable_quantity":"ord_psbl_qty"},
                "orders":{"symbol":"pdno","session_date":"ord_dt","broker_id":"odno","quantity":"ord_qty","cumulative_quantity":"tot_ccld_qty", "cumulative_notional":"tot_ccld_amt",
                          "side":"sll_buy_dvsn_cd","side_codes":{"02":"BUY","01":"SELL"},"canceled_quantity":"cnc_cfrm_qty","day_order_fill_session_verified":True}},
            "bootstrap":{"source":"FAKE_EMPTY_BOOTSTRAP","ownership_verified":True,"account_identity":"FAKE_ACCOUNT_OPAQUE_ID","strategy_quantities":{},
                         "strategy_cash":"10000000","external_quantities":{},"external_order_keys":[],"orders_since":self.now.date().isoformat()},
            "model":{"source":"FAKE_ISOLATION_FIXTURE","isolation_verified":True,"executable_sha256":hashlib.sha256(Path("/usr/bin/true").read_bytes()).hexdigest(),"auth_home_env":"FAKE_AUTH_HOME"},
            "disclosures":{"start_date":self.now.date().isoformat(),"instrument_by_corp_code":{},"verified_events_path":None},
            "rate_limit":{"source":"FAKE_RATE_CONTRACT","verified":True,"minimum_interval_seconds":"0.0001","maximum_queue_seconds":"1"}}
        self._save_manifest()
        self.approval = {"config_hash":self.config.config_hash,"capabilities":["account_read","market_read","disclosure_read","model_call","demo_orders","broker_auth"],
                         "expires_at":"2099-01-01T00:00:00+00:00","operational_evidence":{"runtime_manifest_sha256":hashlib.sha256((self.base/"manifest.json").read_bytes()).hexdigest()}}
        self.env = {"KIS_ACCOUNT_REF":"00000000-00","KIS_APP_KEY":"FAKE_KEY_NOT_A_CREDENTIAL","KIS_APP_SECRET":"FAKE_SECRET_NOT_A_CREDENTIAL",
                    "DART_API_KEY":"FAKE_DART_NOT_A_CREDENTIAL","FAKE_AUTH_HOME":str(self.base/"fake-empty-auth")}

    def tearDown(self):
        self.temp.cleanup()

    def _save_manifest(self):
        (self.base/"manifest.json").write_text(canonical(self.manifest))

    def _factory(self):
        row = {"symbol":"000001","board":"KOSPI","group":"ST","industry":"1","etp":"N","spac":"N","halted":"N","liquidation":"N","managed":"N","preferred":"N"}
        def master(adapter,board):
            adapter._permit("market_read")
            return FetchResult((dict(row,board=board),) if board == "KOSPI" else (),"COMPLETE",self.now)
        with patch("danta.adapters.kis.utcnow",return_value=self.now),patch("danta.adapters.disclosures.utcnow",return_value=self.now),patch.object(KisAdapter,"read_instruments",master):
            return build_external_runtime(self.config,self.approval,kis_transport=self.transport,dart_transport=self.transport,
                                          env=self.env,clock=lambda:self.now)

    def test_default_offline_makes_no_external_calls(self):
        config = load_config(ROOT/"tests/fixtures/config")
        with self.assertRaises(HumanRequired):
            build_external_runtime(config,None,kis_transport=self.transport,dart_transport=self.transport,env={})
        self.assertEqual(self.transport.calls,[])

    def test_file_credentials_and_managed_token_survive_runtime_restart(self):
        path = self.config_dir/"secrets.yaml"
        path.write_text(yaml.safe_dump(self.env))
        path.chmod(0o600)
        self.env = None  # Exercise the production file loader, not injected values.
        first = self._factory()
        first[3].__self__.state.db.close()
        second = self._factory()
        self.assertEqual(sum(path == "/oauth2/tokenP" for _,path in self.transport.calls),1)
        self.assertTrue((self.config.state_dir/"kis-token.json").is_file())
        self.assertEqual(second[0].total_universe,1)
        second[3].__self__.state.db.close()

    def test_broker_auth_required_before_private_file_or_network(self):
        self.approval['capabilities'].remove('broker_auth')
        with patch('danta.runtime.load_secrets', side_effect=AssertionError('private read forbidden')):
            self.env = None
            with self.assertRaises(HumanRequired):
                self._factory()
        self.assertEqual(self.transport.calls,[])

    def test_empty_zero_cash_allocation_is_rejected_before_external_calls(self):
        self.manifest['bootstrap']['strategy_cash'] = '0'
        self._save_manifest()
        self.approval['operational_evidence']['runtime_manifest_sha256'] = hashlib.sha256((self.base/'manifest.json').read_bytes()).hexdigest()
        with self.assertRaisesRegex(HumanRequired, 'ACCOUNT_ALLOCATION_EMPTY'):
            self._factory()
        self.assertEqual(self.transport.calls, [])

    def test_factory_collects_whole_universe_features_and_explicit_no_event(self):
        bundle,broker,decide,refresh = self._factory()
        self.assertEqual(broker.environment,"paper")
        self.assertEqual(bundle.total_universe,1)
        self.assertEqual(bundle.features["KRX:000001"].bars,120)
        self.assertEqual(bundle.data["coverage"]["KRX:000001"],"COMPLETE_NO_EVENT")
        self.assertEqual(bundle.candidates,[])
        self.assertTrue(callable(decide) and callable(refresh))
        self.assertTrue(all(method == "GET" or path == "/oauth2/tokenP" for method,path in self.transport.calls))
        self.assertEqual(sum(path == "/oauth2/tokenP" for _,path in self.transport.calls),1)
        self.assertTrue((self.config.state_dir/"state.sqlite").exists())
        self.assertFalse((self.config.state_dir/"runtime-state.json").exists())
        before = len([path for _method,path in self.transport.calls if "chartprice" in path])
        with patch("danta.adapters.kis.utcnow",return_value=self.now),patch("danta.adapters.disclosures.utcnow",return_value=self.now):
            refresh()
        self.assertEqual(len([path for _method,path in self.transport.calls if "chartprice" in path]),before)
        app = Application(self.config,bundle,broker=broker,decide=decide,refresh=refresh,approval=self.approval)
        try:
            self.assertEqual(app.reconcile()["status"],"RECONCILED")
            self.assertEqual(app.portfolio().nav,Decimal(10000000))
            self.assertIs(broker.store,app.store)
        finally:
            app.close()

    def test_unapproved_manifest_or_model_identity_stops_before_credentials_and_network(self):
        self.manifest["model"]["executable_sha256"] = "changed"
        self._save_manifest()
        with self.assertRaises(HumanRequired):
            self._factory()
        self.assertEqual(self.transport.calls,[])

    def test_existing_positions_bootstrap_into_ledger_without_fictional_buys(self):
        app_config = yaml.safe_load((self.config_dir/'app.yaml').read_text())
        app_config['app']['mode'] = 'shadow'
        (self.config_dir/'app.yaml').write_text(yaml.safe_dump(app_config))
        self.config = load_config(self.config_dir)
        self.approval['config_hash'] = self.config.config_hash
        self.manifest['bootstrap'].update(strategy_quantities={'KRX:000001': 2}, strategy_cash='9000000')
        self._save_manifest()
        self.approval['operational_evidence']['runtime_manifest_sha256'] = hashlib.sha256((self.base/'manifest.json').read_bytes()).hexdigest()
        self.transport.account_positions = [{'pdno':'000001','hldg_qty':'2','ord_psbl_qty':'2'}]
        def start():
            bundle, broker, decide, refresh = self._factory()
            return Application(self.config, bundle, broker=broker, decide=decide, refresh=refresh, approval=self.approval)
        app = start()
        try:
            # A mismatch must leave the empty ledger untouched.
            invalid = dict(self.manifest['bootstrap'], strategy_quantities={'KRX:000001': 3})
            with self.assertRaisesRegex(HumanRequired, 'MATCHING_COMPLETE'):
                app.adopt_account(invalid)
            self.assertEqual(app.store.holdings(), [])
            app.adopt_account(self.manifest['bootstrap'])
            thesis = app.theses()[0]
            self.assertEqual(thesis.origin, 'inherited')
            self.assertIsNone(thesis.first_fill_at)
            self.assertEqual(thesis.adopted_at, self.now)
            self.assertEqual(app.store.quantity('KRX:000001'), 2)
            self.assertEqual(app.store.get('cash_krw'), '9000000')
            self.assertEqual(app.store.db.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 0)
            with patch('danta.adapters.kis.utcnow', return_value=self.now):
                self.assertEqual(app.reconcile()['status'], 'RECONCILED')
            self.assertEqual(app.portfolio().nav, Decimal(9000000) + thesis.average_entry*2)
            with app.store.transaction():
                app.store.set('cash_krw', '8999000')
            original_thesis = thesis.model_dump()
        finally:
            app.close()
        app = start()
        try:
            app.adopt_account(self.manifest['bootstrap'])
            self.assertEqual(app.store.get('cash_krw'), '8999000')
            self.assertEqual(app.theses()[0].model_dump(), original_thesis)
        finally:
            app.close()

    def test_dart_failure_still_bootstraps_partial_bundle_and_price_protection(self):
        from danta.risk import evaluate_exit
        from danta.strategy import assess_entry

        state = RuntimeState(self.config.state_dir/"state.sqlite")
        state.data["paper"] = {"orders":{},"quantities":{"KRX:000001":1},"cash":"9900000"}
        state.save()
        state.db.close()
        self.transport.dart_error = AdapterError("TRANSPORT_FAILED")
        bundle,_broker,decide,_refresh = self._factory()
        self.addCleanup(decide.__self__.state.db.close)
        self.assertEqual(bundle.data["coverage"],{"KRX:000001":"PARTIAL"})
        self.assertIn({"source":"DART","reason":"TRANSPORT_FAILED"},bundle.data["runtime_diagnostics"])
        quote = bundle.quotes["KRX:000001"]
        candidate = bundle.candidates[0]
        self.assertFalse(assess_entry(candidate,quote,bundle.events,bundle.calendar,bundle.ticks,self.now,bundle.profile).allowed)
        thesis = helpers.synthetic_thesis(self.case,quantity=1).model_copy(update={"instrument_id":"KRX:000001"})
        holding = helpers.synthetic_holding(self.case,thesis,quantity=1)
        self.assertEqual(evaluate_exit(thesis,holding,quote,bundle.calendar,self.now,bundle.profile).action,"EXIT_PROTECTION")
        with patch("danta.adapters.kis.utcnow",return_value=self.now):
            protected = decide.__self__.refresh_protection()
        self.assertEqual(protected.candidates,bundle.candidates)
        self.assertEqual(protected.candidates[0].coverage,"PARTIAL")

    def test_targeted_quote_refresh_does_not_repeat_account_or_other_holdings(self):
        bundle,_broker,decide,_refresh = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.state.close)
        with patch.object(runtime,'_account',side_effect=AssertionError('unneeded account read')), \
                patch.object(runtime,'_protection_symbols',return_value={'KRX:000002'}), \
                patch.object(runtime,'_subscribe_quotes',return_value=set()), \
                patch.object(runtime,'_quote',wraps=runtime._quote) as quote, \
                patch('danta.adapters.kis.utcnow',return_value=self.now):
            refreshed = runtime.refresh_quotes(['KRX:000001'])
        self.assertEqual([call.args[0].instrument_id for call in quote.call_args_list],['KRX:000001'])
        self.assertIn('KRX:000001',refreshed.quotes)

    def test_quote_recovery_clears_cached_exclusions_and_keeps_retryable_candidates(self):
        bundle, _broker, decide, refresh = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        account = bundle.data['account_snapshot']
        quote = self.case[7].model_copy(update={'instrument_id':'KRX:000001'})
        with patch.object(runtime, '_account', return_value=(account, 1)), \
                patch.object(runtime, '_protection_symbols', return_value={'KRX:000001'}), \
                patch.object(runtime, '_quote', side_effect=AdapterError('STREAM_NOT_READY')):
            missing = refresh()
        self.assertNotIn('KRX:000001', missing.quotes)
        self.assertEqual([row.instrument.instrument_id for row in missing.candidates], ['KRX:000001'])
        self.assertTrue(any(row['reason']=='STREAM_NOT_READY' for row in missing.exclusions))
        with patch.object(runtime, '_account', return_value=(account, 2)), \
                patch.object(runtime, '_protection_symbols', return_value={'KRX:000001'}), \
                patch.object(runtime, '_quote', return_value=quote):
            recovered = refresh()
        self.assertIn('KRX:000001', recovered.quotes)
        self.assertFalse(any(row['reason']=='STREAM_NOT_READY' for row in recovered.exclusions))
        with patch.object(runtime, '_quote', side_effect=AdapterError('STREAM_NOT_READY')):
            runtime.refresh_quotes(['KRX:000001'])
        with patch.object(runtime, '_quote', return_value=quote):
            recovered = runtime.refresh_quotes(['KRX:000001'])
        for rows in (recovered.exclusions, recovered.data['runtime_diagnostics']):
            self.assertFalse(any(row.get('scope') in {'PROTECTION','ENTRY'} for row in rows))

    def test_monitor_quote_limit_is_separate_from_targeted_order_refresh(self):
        bundle, _broker, decide, _refresh = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.profile['orders']['monitor_quote_max_age_seconds'] = 60
        runtime.state.data['paper']['quantities'] = {'KRX:000001':1}
        self.now += timedelta(seconds=30)
        stale_quote = {'scope':'PROTECTION', 'instrument_id':'KRX:000001',
                       'reason':'MONITOR_DEGRADED', 'detail':'STALE_QUOTE'}
        with patch('danta.adapters.kis.utcnow', side_effect=lambda:self.now):
            protected = runtime.refresh_protection()
            self.assertFalse([row for row in protected.data['runtime_diagnostics']
                              if row.get('scope') in {'PROTECTION', 'ENTRY'}])
            order = runtime.refresh_quotes(['KRX:000001'])
            self.assertIn(stale_quote, order.data['runtime_diagnostics'])
            self.now += timedelta(seconds=31)
            stale = runtime.refresh_protection()
            self.assertIn(stale_quote, stale.data['runtime_diagnostics'])

    def test_protection_refresh_finishes_while_disclosure_collection_is_blocked(self):
        bundle,broker,decide,refresh = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.state.db.close)
        runtime.state.data["paper"]["quantities"] = {"KRX:000001":1}
        previous = self.case[8].model_copy(update={"instrument_id":"KRX:000001","polarity":"NEGATIVE"})
        bundle.events = [previous]
        bundle.data["events"] = [previous.model_dump(mode="json")]
        bundle.candidates = [self.case[6].model_copy(update={"instrument":bundle.instruments["KRX:000001"],
                                                          "features":bundle.features["KRX:000001"]})]
        entered,release = threading.Event(),threading.Event()
        def blocked_events(*_args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Fixture disclosure gate timed out")
            return [previous],[],{"KRX:000001":"PARTIAL"},{}
        with patch.object(runtime,"_events",side_effect=blocked_events), \
                patch("danta.adapters.kis.utcnow",return_value=self.now), ThreadPoolExecutor(max_workers=2) as pool:
            full = pool.submit(refresh)
            try:
                self.assertTrue(entered.wait(2))
                calls = len(self.transport.calls)
                fast = pool.submit(runtime.refresh_protection).result(timeout=2)
                self.assertFalse(full.done())
                self.assertEqual(fast.events,[previous])
                self.assertIs(fast.bars,bundle.bars)
                self.assertIs(fast.features,bundle.features)
                self.assertIs(fast.calendar,bundle.calendar)
                self.assertIn("KRX:000001",fast.quotes)
                self.assertEqual(fast.candidates,bundle.candidates)
                self.assertTrue(all("inquire-asking-price" in path or path.endswith("inquire-price")
                                    for _,path in self.transport.calls[calls:]))
            finally:
                release.set()
            self.assertEqual(full.result(timeout=2).events,[previous])

    def test_protection_stale_and_failed_quotes_keep_published_negative_facts(self):
        from danta.models import MarketFact
        from danta.risk import evaluate_exit

        bundle,_broker,decide,_refresh = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.state.db.close)
        runtime.state.data["paper"]["quantities"] = {"KRX:000001":1}
        event = self.case[8].model_copy(update={"instrument_id":"KRX:000001","polarity":"NEGATIVE"})
        fact = MarketFact(fact_id="negative-fixture",instrument_id="KRX:000001",value="cancelled",unit="status",
                          source=event.source_uri,content_hash=event.source_hash,published_at=None,
                          observed_at=self.now,available_at=self.now,quality="VERIFIED")
        bundle.events,bundle.facts = [event],[fact]
        bundle.data.update(events=[event.model_dump(mode="json")],facts=[fact.model_dump(mode="json")])
        thesis = helpers.synthetic_thesis(self.case,quantity=1).model_copy(update={"instrument_id":"KRX:000001","invalidating_event_ids":[event.event_id]})
        holding = helpers.synthetic_holding(self.case,thesis,quantity=1)
        self.transport.quote_observed_at = self.now-timedelta(minutes=1)
        with patch("danta.adapters.kis.utcnow",return_value=self.now):
            stale = runtime.refresh_protection()
            self.assertEqual(stale.quotes["KRX:000001"].observed_at,self.transport.quote_observed_at)
            self.transport.quote_error = AdapterError("TRANSPORT_FAILED")
            missing = runtime.refresh_protection()
        self.assertNotIn("KRX:000001",missing.quotes)
        for observed in (stale,missing):
            self.assertEqual(observed.events,[event])
            self.assertEqual(observed.facts,[fact])
            self.assertTrue(any(row["reason"] == "MONITOR_DEGRADED" for row in observed.data["runtime_diagnostics"]))
            exit_plan = evaluate_exit(thesis,holding,observed.quotes.get("KRX:000001"),observed.calendar,self.now,
                                      observed.profile,invalidating_events=observed.events)
            self.assertEqual(exit_plan.action,"MONITOR_DEGRADED")
            self.assertIn("EXIT_THESIS_INVALID",exit_plan.reasons)

    def test_protection_publication_retains_new_full_facts_and_matches_paper_quote_depth(self):
        from copy import copy

        bundle,broker,decide,_refresh = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.state.db.close)
        runtime.manifest["normalization"]["quote"].update(bid_quantity="asking.bidp_rsqn1",ask_quantity="asking.askp_rsqn1")
        broker.submit({"plan_id":"PENDING_PAPER","side":"BUY","quantity":2,"instrument_id":"KRX:000001",
                       "limit_price":"10000","expires_at":(self.now+timedelta(minutes=2)).isoformat()})
        self.now += timedelta(seconds=1)
        self.transport.quote_observed_at = self.now
        newer = copy(bundle)
        event = self.case[8].model_copy(update={"instrument_id":"KRX:000001","polarity":"NEGATIVE"})
        newer.events = [event]
        newer.data = {**bundle.data,"events":[event.model_dump(mode="json")]}
        entered,release = threading.Event(),threading.Event()
        original = runtime._quote
        def blocked_quote(*args,**kwargs):
            quote = original(*args,**kwargs)
            entered.set()
            if not release.wait(3):
                raise AssertionError("Fixture quote gate timed out")
            return quote
        with patch.object(runtime,"_quote",side_effect=blocked_quote), \
                patch("danta.adapters.kis.utcnow",side_effect=lambda:self.now), ThreadPoolExecutor(max_workers=1) as pool:
            fast = pool.submit(runtime.refresh_protection)
            try:
                self.assertTrue(entered.wait(2))
                runtime._publish(newer)
            finally:
                release.set()
            published = fast.result(timeout=2)
        self.assertEqual(published.events,[event])
        self.assertEqual(published.data["quote_depth"]["KRX:000001"],{"bid":1,"ask":1})
        self.assertEqual(published.data["account_snapshot"]["orders"][0]["cumulative_quantity"],1)
        self.assertEqual(published.data["strategy_sellable_quantities"],{"KRX:000001":1})
        delayed = copy(published)
        delayed.quotes = {"KRX:000001":published.quotes["KRX:000001"].model_copy(update={"observed_at":self.now-timedelta(seconds=1)})}
        delayed.data = {**published.data,"quote_depth":{"KRX:000001":{"bid":9,"ask":9}},
                        "quote_refresh_order":{"KRX:000001":published.data["quote_refresh_order"]["KRX:000001"]+1}}
        retained = runtime._publish(delayed)
        self.assertEqual(retained.quotes,published.quotes)
        self.assertEqual(retained.data["quote_depth"],published.data["quote_depth"])

    def test_disclosure_and_protection_authorization_failures_are_not_degraded_data(self):
        _bundle,_broker,decide,refresh = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.state.db.close)
        runtime.state.data["disclosure_last_poll"] = None
        for failure in (HumanRequired("AUTHORIZATION_REVOKED"),AdapterError("AUTH_FAILED")):
            with self.subTest(failure=type(failure).__name__),patch.object(runtime.dart,"list_disclosures",side_effect=failure):
                with self.assertRaises(type(failure)):
                    refresh()
        with patch.object(runtime.dart,"list_disclosures",return_value=FetchResult((),"FETCH_FAILED",self.now,metadata={"error":"AUTH_FAILED"})):
            with self.assertRaisesRegex(AdapterError,"AUTH_FAILED"):
                refresh()
        with patch.object(type(runtime.config),"require_external",side_effect=HumanRequired("AUTHORIZATION_REVOKED")):
            with self.assertRaisesRegex(HumanRequired,"AUTHORIZATION_REVOKED"):
                runtime.refresh_protection()
        runtime.state.data["paper"]["quantities"] = {"KRX:000001":1}
        with patch.object(runtime.kis,"quote",side_effect=HumanRequired("AUTHORIZATION_REVOKED")):
            with self.assertRaisesRegex(HumanRequired,"AUTHORIZATION_REVOKED"):
                runtime.refresh_protection()

    def test_known_fill_with_unknown_fees_is_preserved_for_protection(self):
        state = RuntimeState(self.base/"broker.sqlite")
        self.addCleanup(state.db.close)
        state.db.execute("CREATE TABLE intents(broker_namespace TEXT,broker_id TEXT,instrument_id TEXT,side TEXT,broker_metadata TEXT)")
        namespace = "demo:FAKE_ACCOUNT_ALIAS:"+self.now.date().isoformat()+":KRX"
        state.db.execute("INSERT INTO intents VALUES (?,?,?,?,?)",(namespace,"123","KRX:000001","BUY",'{"organization":"999"}'))
        class FakeAdapter:
            environment = "demo"
            def read_account(inner,**kwargs):
                return FetchResult(({"pdno":"000001","hldg_qty":"3","ord_psbl_qty":"3"},),"COMPLETE",self.now,
                                   metadata={"orderable_resources":{"ord_psbl_cash":"9000000"}})
            def read_orders(inner,*args):
                return FetchResult(({"pdno":"000001","ord_dt":self.now.strftime("%Y%m%d"),"odno":"123","ord_qty":"10","tot_ccld_qty":"3",
                                     "tot_ccld_amt":"30000","sll_buy_dvsn_cd":"02","cnc_cfrm_qty":"0"},),"COMPLETE",self.now)
        broker = KisBrokerPort(FakeAdapter(),self.manifest,state,clock=lambda:self.now)
        snapshot = broker.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["strategy_quantities"],{"KRX:000001":3})
        self.assertEqual(snapshot["orders"][0]["cumulative_quantity"],3)
        self.assertIsNone(snapshot["orders"][0]["cumulative_fees"])
        self.assertEqual(snapshot["orders"][0]["fill_time_quality"],"FIRST_OBSERVED")
        self.assertIsNone(snapshot["orders"][0]["first_fill_at"])
        supplement = {"verified":True,"source":"FAKE_SETTLEMENT","source_sha256":"FAKE_SOURCE_HASH",
                      "cumulative_quantity":3,"cumulative_notional":"30000","observed_at":self.now.isoformat()}
        for fees,first_fill in (("5",None),(None,self.now-timedelta(minutes=1)),("5",self.now-timedelta(minutes=1))):
            with self.subTest(fees=fees,first_fill=first_fill),patch.object(broker,"_supplements",return_value={
                    namespace+":123":{**supplement,"actual_cumulative_fees":fees,
                                     "first_fill_at":first_fill.isoformat() if first_fill else None}}):
                settled = broker.snapshot()
                self.assertTrue(settled["complete"])
                item = settled["orders"][0]
                self.assertEqual(item["cumulative_fees"],fees)
                self.assertEqual(item["fill_session_id"],self.now.date().isoformat())
                self.assertEqual(item["observed_at"],aware_time(self.now.isoformat()).isoformat())
                self.assertEqual(item["first_fill_at"],aware_time(first_fill.isoformat()).isoformat() if first_fill else None)
                self.assertEqual(item["fill_time_quality"],"EXACT" if first_fill else "FIRST_OBSERVED")
        for invalid in (self.now+timedelta(minutes=1),self.now-timedelta(days=1)):
            with self.subTest(invalid=invalid),patch.object(broker,"_supplements",return_value={
                    namespace+":123":{**supplement,"first_fill_at":invalid.isoformat()}}):
                self.assertEqual(broker.snapshot()["errors"],["INVALID_FILL_TIMESTAMPS"])

    def test_parallel_broker_snapshots_do_not_mix_cash_or_sellable_quantities(self):
        state = RuntimeState(self.base/"parallel-broker.sqlite")
        self.addCleanup(state.db.close)
        self.manifest["bootstrap"]["strategy_quantities"] = {"KRX:000001":3}
        quantities,local = iter((3,4)),threading.local()
        entered,release = threading.Event(),threading.Event()
        class FakeAdapter:
            environment = "demo"
            def read_account(inner,**kwargs):
                local.quantity = next(quantities)
                return FetchResult(({"pdno":"000001","hldg_qty":str(local.quantity),"ord_psbl_qty":str(local.quantity-2)},),
                                   "COMPLETE",self.now,metadata={"orderable_resources":{"ord_psbl_cash":str(local.quantity*1000)}})
            def read_orders(inner,*args):
                return FetchResult((),"COMPLETE",self.now)
        broker = KisBrokerPort(FakeAdapter(),self.manifest,state,clock=lambda:self.now)
        def supplements():
            if local.quantity == 3:
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Fixture account gate timed out")
            return {}
        with patch.object(broker,"_supplements",side_effect=supplements),ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(broker.snapshot)
            try:
                self.assertTrue(entered.wait(2))
                second = broker.snapshot()
            finally:
                release.set()
            first = pending.result(timeout=2)
        for snapshot,quantity in ((first,3),(second,4)):
            self.assertTrue(snapshot["complete"] and snapshot["ownership_complete"])
            self.assertEqual(snapshot["broker_available_cash"],str(quantity*1000))
            self.assertEqual(snapshot["strategy_quantities"],{"KRX:000001":quantity})
            self.assertEqual(snapshot["strategy_sellable_quantities"],{"KRX:000001":quantity-2})

    def test_unknown_ownership_or_partial_pagination_never_becomes_complete(self):
        state = RuntimeState(self.base/"incomplete.sqlite")
        class FakeAdapter:
            environment = "demo"
            def read_account(inner,**kwargs):
                return FetchResult((),"PARTIAL",self.now)
            def read_orders(inner,*args):
                return FetchResult((),"COMPLETE",self.now)
        broker = KisBrokerPort(FakeAdapter(),self.manifest,state,clock=lambda:self.now)
        self.assertFalse(broker.snapshot()["complete"])

    def test_disclosure_page_limit_continues_and_document_retry_does_not_rewind_listing(self):
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.manifest['automatic'] = True
        runtime.manifest['disclosures']['instrument_by_corp_code'] = {'00000001':'KRX:000001'}
        runtime.manifest['disclosures']['start_date'] = (self.now.date() - timedelta(days=30)).isoformat()
        runtime.state.data['disclosure_last_poll'] = None
        rows = [{'rcept_no':self.now.strftime('%Y%m%d') + f'{index:06}', 'rcept_dt':self.now.strftime('%Y%m%d'),
                 'corp_code':'00000001','report_nm':'영업실적 공시'} for index in (1,2)]
        content = b'<p>synthetic unclassified disclosure</p>'
        calls, document_calls = [], []
        class Dart:
            first = True
            def list_disclosures(inner, start, end, **kwargs):
                calls.append((start,end,kwargs))
                if not inner.first:
                    return FetchResult((), 'COMPLETE_NO_EVENT', self.now)
                if not kwargs:
                    return FetchResult((rows[0],), 'PARTIAL', self.now, 101, {'error':'PAGE_LIMIT'})
                return FetchResult((rows[1],), 'COMPLETE', self.now)
            def read_disclosure(inner, receipt):
                document_calls.append(receipt)
                if inner.first and receipt == rows[0]['rcept_no']:
                    raise AdapterError('DOCUMENT_NOT_AVAILABLE')
                return FetchResult(({'content':content,'sha256':hashlib.sha256(content).hexdigest()},), 'COMPLETE', self.now)
        runtime.dart = Dart()
        runtime._events(list(bundle.instruments.values()), self.now)
        self.assertEqual(calls[1][2], {'cursor':101})
        self.assertEqual(runtime.state.data['disclosure_cursor_date'], self.now.date().isoformat())
        self.assertEqual(list(runtime.state.data['disclosure_pending_documents']), [rows[0]['rcept_no']])
        runtime.dart.first = False
        later = self.now + timedelta(days=1)
        runtime._events(list(bundle.instruments.values()), later)
        self.assertEqual(calls[-1][0], self.now.date())
        self.assertEqual(document_calls.count(rows[0]['rcept_no']), 2)
        self.assertEqual(document_calls.count(rows[1]['rcept_no']), 1)
        self.assertFalse(runtime.state.data['disclosure_pending_documents'])

    def test_decision_rechecks_only_its_issuers_without_rebuilding_universe(self):
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.manifest['disclosures']['instrument_by_corp_code'] = {'00000001':'KRX:000001'}
        frozen = {'created_at':self.now.isoformat(), 'events':[], 'facts':[], 'candidates':[],
                  'reviewed_positions':['KRX:000001']}
        last_poll = runtime.state.data['disclosure_last_poll']
        with patch.object(runtime.dart, 'list_disclosures', return_value=FetchResult((), 'COMPLETE_NO_EVENT', self.now)) as listing, \
                patch.object(runtime, '_instruments', side_effect=AssertionError('full universe reread')):
            result = runtime.refresh_decision(frozen)
        listing.assert_called_once_with(self.now.date(), self.now.date(), corp_code='00000001')
        self.assertEqual(runtime.state.data['disclosure_last_poll'], last_poll)
        self.assertEqual(result.instruments, bundle.instruments)
        with patch.object(runtime.dart, 'list_disclosures', return_value=FetchResult((), 'FETCH_FAILED', self.now, metadata={'error':'TRANSPORT_FAILED'})):
            with self.assertRaisesRegex(AdapterError, 'DECISION_EVIDENCE_REFRESH_INCOMPLETE'):
                runtime.refresh_decision(frozen)

    def test_supported_official_parser_and_next_day_cursor_preserve_first_availability(self):
        bundle,broker,decide,refresh = self._factory()
        runtime = decide.__self__
        runtime.manifest["disclosures"]["instrument_by_corp_code"] = {"00000001":"KRX:000001"}
        runtime.state.data["disclosure_last_poll"] = None
        content = ('<p>연결 기준 (단위: 백만원)</p><table><tr><th>구분</th><th>당기실적 2026.01.01~2026.03.31</th>'
                   '<th>전년동기실적 2025.01.01~2025.03.31</th></tr><tr><td>매출액</td><td>1,200</td><td>1,000</td></tr>'
                   '<tr><td>영업이익</td><td>150</td><td>100</td></tr></table>').encode()
        receipt = self.now.strftime("%Y%m%d")+"000001"
        class FakeDart:
            has_event = True
            def list_disclosures(inner,start,end):
                rows = ({"rcept_no":receipt,"rcept_dt":self.now.strftime("%Y%m%d"),"corp_code":"00000001","report_nm":"연결 영업실적 공시"},) if inner.has_event else ()
                return FetchResult(rows,"COMPLETE" if rows else "COMPLETE_NO_EVENT",self.now)
            def read_disclosure(inner,identifier):
                return FetchResult(({"content":content,"sha256":hashlib.sha256(content).hexdigest()},),"COMPLETE",self.now)
        runtime.dart = FakeDart()
        events,facts,coverage,documents = runtime._events(list(bundle.instruments.values()),self.now)
        self.assertEqual(events[0].family,"earnings_quality")
        self.assertTrue(events[0].primary_source_complete)
        first_available = events[0].available_at
        self.assertTrue(facts and documents)
        runtime.dart.has_event = False
        later = self.case[1].sessions[121].opens_at+timedelta(minutes=30)
        events,_,coverage,_ = runtime._events(list(bundle.instruments.values()),later)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].available_at,first_available)
        self.assertEqual(coverage["KRX:000001"],"COMPLETE")
        restored = RuntimeState(self.config.state_dir/"state.sqlite")
        self.assertEqual(aware_time(restored.data["events"][receipt]),first_available)
        with patch.object(runtime.dart,"list_disclosures",side_effect=AdapterError("TRANSPORT_FAILED")):
            preserved,_,coverage,_ = runtime._events(list(bundle.instruments.values()),later+timedelta(seconds=180))
        self.assertEqual(preserved[0].available_at,first_available)
        self.assertEqual(coverage["KRX:000001"],"PARTIAL")
        self.assertIn({"source":"DART","reason":"TRANSPORT_FAILED"},runtime.disclosure_diagnostics)

    def test_cached_disclosures_reparse_without_redownload_or_rejuvenating_old_evidence(self):
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.manifest['automatic'] = True
        runtime.manifest['disclosures']['instrument_by_corp_code'] = {'00000001':'KRX:000001'}
        runtime.state.data['disclosure_last_poll'] = None
        content = ('<p>연결 기준 (단위: 백만원)</p><table><tr><th>구분</th><th>당기실적 2026.01.01~2026.03.31</th>'
                   '<th>전년동기실적 2025.01.01~2025.03.31</th></tr><tr><td>매출액</td><td>1,200</td><td>1,000</td></tr>'
                   '<tr><td>영업이익</td><td>150</td><td>100</td></tr></table>').encode()
        recent_day = self.case[1].sessions[118].opens_at.date()
        old_day = self.case[1].sessions[114].opens_at.date()
        rows = [
            {'rcept_no':recent_day.strftime('%Y%m%d')+'000001','rcept_dt':recent_day.isoformat(),'corp_code':'00000001','report_nm':'연결 영업실적 공시'},
            {'rcept_no':self.now.strftime('%Y%m%d')+'000002','rcept_dt':self.now.date().isoformat(),'corp_code':'00000001','report_nm':'임상시험계획승인'},
            {'rcept_no':old_day.strftime('%Y%m%d')+'000003','rcept_dt':old_day.isoformat(),'corp_code':'00000001','report_nm':'분기보고서'},
            {'rcept_no':'20200101000004','rcept_dt':'20200101','corp_code':'00000001','report_nm':'분기보고서'},
        ]
        def document(receipt):
            body = content if receipt == rows[0]['rcept_no'] else b'<p>unsupported official table</p>'
            return FetchResult(({'content':body,'sha256':hashlib.sha256(body).hexdigest()},), 'COMPLETE', self.now)
        with patch.object(runtime.dart, 'list_disclosures', return_value=FetchResult(tuple(rows), 'COMPLETE', self.now)), \
                patch.object(runtime.dart, 'read_disclosure', side_effect=document):
            events, _, coverage, _ = runtime._events(list(bundle.instruments.values()), self.now)
        self.assertEqual({event.official_id for event in events}, {row['rcept_no'] for row in rows[:2]})
        self.assertEqual(coverage['KRX:000001'], 'COMPLETE')
        record = runtime.state.data['disclosure_records'][rows[0]['rcept_no']]
        record.pop('parser_version')
        record.pop('receipt')
        record['event'].update(timing_quality='UNCERTAIN', published_date=None, primary_source_complete=False)
        first_seen = runtime.state.data['events'][rows[0]['rcept_no']]
        with patch.object(runtime.dart, 'list_disclosures', side_effect=AssertionError('unexpected listing')), \
                patch.object(runtime.dart, 'read_disclosure', side_effect=AssertionError('unexpected redownload')):
            events, _, coverage, _ = runtime._events(list(bundle.instruments.values()), self.now+timedelta(seconds=1))
        repaired = next(event for event in events if event.official_id == rows[0]['rcept_no'])
        self.assertTrue(repaired.primary_source_complete)
        self.assertEqual(repaired.timing_quality, 'DATE_ONLY')
        self.assertIsNone(repaired.published_at)
        self.assertEqual(repaired.published_date, recent_day)
        self.assertEqual(repaired.available_at, aware_time(first_seen))
        self.assertEqual(runtime.calendar.event_age(repaired, runtime.calendar.active(self.now).session_id), 3)
        self.assertEqual(coverage['KRX:000001'], 'COMPLETE')
        runtime.state.db.execute('CREATE TABLE holdings(instrument_id TEXT, owner TEXT, quantity INTEGER)')
        runtime.state.db.execute("INSERT INTO holdings VALUES ('KRX:000001','strategy',1)")
        retained, _, coverage, documents = runtime._events(list(bundle.instruments.values()), self.now+timedelta(seconds=2))
        self.assertEqual({event.official_id for event in retained}, {row['rcept_no'] for row in rows})
        self.assertEqual(coverage['KRX:000001'], 'COMPLETE')
        self.assertTrue(any(doc['receipt_id'] == rows[-1]['rcept_no'] for doc in documents.values()))

    def test_version_two_decline_cache_is_reparsed_without_new_availability_or_download(self):
        from danta.disclosure_parser import PARSER_VERSION
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.manifest['automatic'] = True
        runtime.manifest['disclosures']['instrument_by_corp_code'] = {'00000001': 'KRX:000001'}
        runtime.state.data['disclosure_last_poll'] = None
        content = ('<p>연결 기준 (단위: 백만원)</p><table><tr><th>구분</th><th>당기실적 2026.01.01~2026.03.31</th>'
                   '<th>전년동기실적 2025.01.01~2025.03.31</th></tr><tr><td>매출액</td><td>900</td><td>1,000</td></tr>'
                   '<tr><td>영업이익</td><td>90</td><td>100</td></tr></table>').encode()
        receipt = self.now.strftime('%Y%m%d') + '000001'
        row = {'rcept_no': receipt, 'rcept_dt': self.now.date().isoformat(),
               'corp_code': '00000001', 'report_nm': '연결 영업실적 공시'}
        document = FetchResult(({'content': content, 'sha256': hashlib.sha256(content).hexdigest()},), 'COMPLETE', self.now)
        with patch('danta.disclosure_parser.PARSER_VERSION', 2), \
                patch.object(runtime.dart, 'list_disclosures', return_value=FetchResult((row,), 'COMPLETE', self.now)), \
                patch.object(runtime.dart, 'read_disclosure', return_value=document):
            runtime._events(list(bundle.instruments.values()), self.now)
        record = runtime.state.data['disclosure_records'][receipt]
        record['event']['polarity'] = 'UNKNOWN'  # Actual version-2 interpretation of this table.
        first_seen = record['event']['available_at']
        with patch.object(runtime.dart, 'list_disclosures', side_effect=AssertionError('unexpected listing')), \
                patch.object(runtime.dart, 'read_disclosure', side_effect=AssertionError('unexpected download')):
            events, _, _, _ = runtime._events(list(bundle.instruments.values()), self.now + timedelta(seconds=1))
        self.assertEqual(events[0].polarity, 'NEGATIVE')
        self.assertEqual(events[0].available_at, aware_time(first_seen))
        self.assertEqual(runtime.state.data['disclosure_records'][receipt]['parser_version'], PARSER_VERSION)

    def test_future_or_unknown_publication_date_remains_uncertain(self):
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.manifest['disclosures']['instrument_by_corp_code'] = {'00000001':'KRX:000001'}
        content = b'<p>unsupported financial table</p>'
        document = FetchResult(({'content':content,'sha256':hashlib.sha256(content).hexdigest()},), 'COMPLETE', self.now)
        for index, day in enumerate(((self.now.date()+timedelta(days=1)).isoformat(), '', 'bad-date')):
            row = {'rcept_no':self.now.strftime('%Y%m%d')+f'{index:06}','rcept_dt':day,
                   'corp_code':'00000001','report_nm':'영업실적 공시'}
            runtime.state.data['disclosure_last_poll'] = None
            with patch.object(runtime.dart, 'list_disclosures', return_value=FetchResult((row,), 'COMPLETE', self.now)), \
                    patch.object(runtime.dart, 'read_disclosure', return_value=document):
                events, _, coverage, _ = runtime._events(list(bundle.instruments.values()), self.now)
            self.assertEqual(next(event for event in events if event.official_id == row['rcept_no']).timing_quality, 'UNCERTAIN')
            self.assertEqual(coverage['KRX:000001'], 'PARTIAL')

    def test_old_pending_source_failure_is_visible_but_does_not_block_current_decision(self):
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.manifest['disclosures']['instrument_by_corp_code'] = {'00000001':'KRX:000001'}
        old_day = self.case[1].sessions[110].opens_at.date()
        row = {'rcept_no':old_day.strftime('%Y%m%d')+'000001','rcept_dt':old_day.isoformat(),
               'corp_code':'00000001','report_nm':'분기보고서'}
        runtime.state.data['disclosure_pending_documents'] = {row['rcept_no']:row}
        frozen = {'created_at':self.now.isoformat(), 'events':[], 'facts':[], 'candidates':[], 'reviewed_positions':['KRX:000001']}
        with patch.object(runtime.dart, 'list_disclosures', return_value=FetchResult((), 'COMPLETE_NO_EVENT', self.now)), \
                patch.object(runtime.dart, 'read_disclosure', side_effect=AdapterError('DOCUMENT_NOT_AVAILABLE')):
            runtime.refresh_decision(frozen)
            self.assertEqual(len(runtime.disclosure_diagnostics), 1)
            self.assertFalse(runtime.disclosure_diagnostics[0]['affects_current_evidence'])
            row['rcept_dt'] = ''
            with self.assertRaisesRegex(AdapterError, 'DECISION_EVIDENCE_REFRESH_INCOMPLETE'):
                runtime.refresh_decision(frozen)

    def test_refresh_preserves_held_evidence_older_than_calendar_without_selecting_it_as_recent(self):
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        ancient = self.case[8].model_copy(update={'instrument_id':'KRX:000001','timing_quality':'DATE_ONLY',
                                                'published_date':datetime(2020,1,1).date()})
        with patch.object(runtime, '_events', return_value=([ancient], [], {'KRX:000001':'COMPLETE'}, {})):
            refreshed = runtime.refresh()
        self.assertEqual(refreshed.events, [ancient])
        self.assertTrue(any(row.get('reason') == 'NO_VALID_RECENT_OFFICIAL_EVENT' for row in refreshed.data['runtime_diagnostics']))

    def test_correction_parent_lookup_is_issuer_scoped_and_retries_from_cached_original(self):
        bundle, _, decide, _ = self._factory()
        runtime = decide.__self__
        self.addCleanup(runtime.close)
        runtime.manifest['disclosures']['instrument_by_corp_code'] = {'00000001':'KRX:000001'}
        runtime.state.data['disclosure_last_poll'] = None
        parent_day = self.now.date()-timedelta(days=30)
        parent = {'rcept_no':parent_day.strftime('%Y%m%d')+'000001','rcept_dt':parent_day.isoformat(),
                  'corp_code':'00000001','report_nm':'단일판매ㆍ공급계약체결'}
        receipt = {'rcept_no':self.now.strftime('%Y%m%d')+'000002','rcept_dt':self.now.date().isoformat(),
                   'corp_code':'00000001','report_nm':'[기재정정]단일판매ㆍ공급계약체결'}
        pairs = [('정정관련 공시서류','단일판매ㆍ공급계약체결'),('정정관련 공시서류제출일',parent_day.isoformat())]
        contract = [('계약금액(원)','2000000000'),('최근매출액(원)','8000000000'),('계약상대','합성회사'),
                    ('계약조건','검수 후 지급'),('계약기간 시작일','2026-01-01'),('계약기간 종료일','2026-12-31')]
        content = ''.join('<table>'+''.join(f'<tr><td>{key}</td><td>{value}</td></tr>' for key,value in rows)+'</table>'
                          for rows in (pairs, contract)).encode()
        document = FetchResult(({'content':content,'sha256':hashlib.sha256(content).hexdigest()},), 'COMPLETE', self.now)
        parent_result = FetchResult((), 'FETCH_FAILED', self.now, metadata={'error':'TRANSPORT_FAILED'})
        def listing(start, end, **kwargs):
            if kwargs:
                self.assertEqual((start,end,kwargs), (parent_day,parent_day,{'corp_code':'00000001'}))
                return parent_result
            return FetchResult((receipt,), 'COMPLETE', self.now)
        with patch.object(runtime.dart, 'list_disclosures', side_effect=listing), \
                patch.object(runtime.dart, 'read_disclosure', return_value=document) as read:
            _, _, coverage, _ = runtime._events(list(bundle.instruments.values()), self.now)
            self.assertEqual(coverage['KRX:000001'], 'PARTIAL')
            parent_result = FetchResult((parent,), 'COMPLETE', self.now)
            events, _, coverage, _ = runtime._events(list(bundle.instruments.values()), self.now+timedelta(seconds=181))
        read.assert_called_once_with(receipt['rcept_no'])
        self.assertEqual(events[0].correction_of, 'dart:'+parent['rcept_no'])
        self.assertTrue(events[0].primary_source_complete)
        self.assertEqual(coverage['KRX:000001'], 'COMPLETE')
        self.assertFalse(runtime.state.data['disclosure_pending_documents'])

    def test_quote_actual_observation_time_is_not_http_reception_time(self):
        bundle,_broker,decide,_refresh = self._factory()
        runtime = decide.__self__
        stale = self.now-timedelta(minutes=1)
        def quote(_symbol):
            return FetchResult(({"asking":{"aspr_acpt_hour":stale.strftime("%H%M%S"),"bidp1":"10000","askp1":"10001"},
                                  "price":{"stck_bsop_date":self.now.strftime("%Y%m%d")}},),"COMPLETE",self.now)
        runtime.kis.quote = quote
        normalized = runtime._quote(bundle.instruments["KRX:000001"])
        self.assertEqual(normalized.observed_at,stale)
        self.assertEqual(normalized.received_at,self.now)

    def test_decision_callback_uses_injected_runner_and_excludes_broker_credentials(self):
        bundle,broker,decide,refresh = self._factory()
        app = Application(self.config,bundle,broker=broker,decide=decide,refresh=refresh,approval=self.approval)
        self.addCleanup(app.close)
        runtime = decide.__self__
        runtime.clock = lambda:datetime.now(timezone.utc)
        calls = []
        def fake_runner(command,*,env,cwd,input,timeout):
            calls.append(command)
            self.assertNotIn("KIS_APP_SECRET",env)
            self.assertNotIn("FAKE_SECRET_NOT_A_CREDENTIAL",input)
            frozen = json.loads((Path(cwd)/"input.json").read_text())
            value = {"schema_version":1,"run_id":frozen["run_id"],"input_snapshot_id":frozen["input_snapshot_id"],
                     "account_state_version":1,"review_scope":"FULL","candidate_reviews":[],"position_reviews":[],"human_question":None}
            Path(command[command.index("--output-last-message")+1]).write_text(json.dumps(value))
            return 0,json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}),""
        runtime.codex.runner = fake_runner
        frozen = {"run_id":"FAKE_RUN","input_snapshot_id":"FAKE_INPUT","portfolio":{"account_state_version":1},
                  "review_scope":"FULL","reviewed_positions":[],"events":[],"facts":[],"candidates":[],"theses":[]}
        result = decide(frozen)
        self.assertEqual(result["run_id"],"FAKE_RUN")
        self.assertEqual(len(calls),1)
        rows = [(row["kind"],json.loads(row["payload"])) for row in app.store.db.execute("SELECT kind,payload FROM journal WHERE kind LIKE 'MODEL_%'")]
        self.assertEqual([kind for kind,_ in rows],["MODEL_ATTEMPT","MODEL_OUTCOME"])
        self.assertEqual(rows[0][1]["usage"],{"input_tokens":1,"output_tokens":1})
        self.assertEqual(rows[1][1]["status"],"SUCCESS")

    def test_decide_requires_bound_journal_before_model_call(self):
        _bundle,_broker,decide,_refresh = self._factory()
        with patch.object(decide.__self__.codex,"run",side_effect=AssertionError("unlogged model forbidden")):
            with self.assertRaisesRegex(AdapterError,"MODEL_JOURNAL_STORE_UNBOUND"):
                decide({})

    def test_decide_records_every_retry_and_failure_for_usage(self):
        from danta.service import Service

        bundle,broker,decide,refresh = self._factory()
        app = Application(self.config,bundle,broker=broker,decide=decide,refresh=refresh,approval=self.approval)
        self.addCleanup(app.close)
        runtime = decide.__self__
        runtime.clock = lambda:datetime.now(timezone.utc)
        runtime.codex.sleep = lambda _seconds:None
        frozen = {"run_id":"RETRY_RUN","input_snapshot_id":"RETRY_INPUT","portfolio":{"account_state_version":1},
                  "review_scope":"FULL","reviewed_positions":[],"events":[],"facts":[],"candidates":[],"theses":[]}
        calls = []
        def runner(command,*,env,cwd,input,timeout):
            calls.append(command)
            if len(calls) == 1:
                return 1,'{"type":"turn.failed","error":{"message":"503 server_error"}}',""
            return 0,'{"type":"error","message":"usage_limit_reached"}\n{"type":"turn.failed","error":{"message":"quota exhausted"}}',""
        runtime.codex.runner = runner
        with self.assertRaisesRegex(AdapterError,"MODEL_QUOTA_EXHAUSTED"):
            decide(frozen)
        rows = [(row["kind"],json.loads(row["payload"])) for row in app.store.db.execute("SELECT kind,payload FROM journal WHERE kind LIKE 'MODEL_%' ORDER BY sequence")]
        self.assertEqual([kind for kind,_ in rows],["MODEL_ATTEMPT","MODEL_ATTEMPT","MODEL_OUTCOME"])
        self.assertEqual([record["status"] for _,record in rows],["TRANSIENT_FAILURE","QUOTA_EXHAUSTED","QUOTA_EXHAUSTED"])
        self.assertTrue(all(record["usage"] is None for _,record in rows))
        self.assertEqual(rows[-1][1]["attempt_count"],2)
        self.assertEqual(len({record["call_id"] for _,record in rows}),1)
        with self.assertRaisesRegex(AdapterError,"MODEL_QUOTA_CIRCUIT_OPEN"):
            decide(frozen)
        self.assertEqual(len(calls),2)
        outcome = json.loads(app.store.db.execute("SELECT payload FROM journal WHERE kind='MODEL_OUTCOME' ORDER BY sequence DESC LIMIT 1").fetchone()[0])
        self.assertEqual(outcome["attempt_count"],0)
        self.assertIsNone(outcome["usage"])
        service = Service(app)
        usage = service._dispatch({"kind":"telegram","command":"usage","text":"/usage","route":"1","chat_id":"2","user_id":"3"},"usage-request")
        self.assertEqual(usage["status"],"USAGE")
        self.assertEqual(len(usage["attempts"]),4)
        self.assertTrue(all(record["record_type"] in {"MODEL_ATTEMPT","MODEL_OUTCOME"} for record in usage["attempts"]))

    def test_semantic_callback_uses_actual_completion_after_long_model_call(self):
        bundle,broker,decide,refresh = self._factory()
        app = Application(self.config,bundle,broker=broker,decide=decide,refresh=refresh,approval=self.approval)
        self.addCleanup(app.close)
        runtime = decide.__self__
        now = [datetime.now(timezone.utc)]
        runtime.clock = lambda:now[0]
        frozen = {"run_id":"LONG_RUN","input_snapshot_id":"LONG_INPUT","portfolio":{"account_state_version":1},
                  "review_scope":"FULL","reviewed_positions":[],"events":[],"facts":[],"candidates":[],"theses":[]}
        def runner(command,*,env,cwd,input,timeout):
            now[0] += timedelta(seconds=150)
            value = {"schema_version":1,"run_id":"LONG_RUN","input_snapshot_id":"LONG_INPUT","account_state_version":1,
                     "review_scope":"FULL","candidate_reviews":[],"position_reviews":[],"human_question":None}
            Path(cwd,"final.json").write_text(json.dumps(value))
            return 0,'{"type":"turn.completed"}',""
        runtime.codex.runner = runner
        from danta.decision import validate_proposal
        with patch("danta.runtime.validate_proposal",wraps=validate_proposal) as validator:
            self.assertEqual(decide(frozen)["run_id"],"LONG_RUN")
            self.assertEqual(validator.call_args.kwargs["completed_at"],now[0])
            self.assertEqual(validator.call_args.kwargs["now"],now[0])
        result = json.loads(app.store.db.execute("SELECT payload FROM journal WHERE kind='MODEL_OUTCOME'").fetchone()[0])
        self.assertEqual((aware_time(result["completed_at"])-aware_time(result["started_at"])).total_seconds(),150)

    def test_process_start_failure_records_attempt_and_outcome_without_error_text(self):
        bundle,broker,decide,refresh = self._factory()
        app = Application(self.config,bundle,broker=broker,decide=decide,refresh=refresh,approval=self.approval)
        self.addCleanup(app.close)
        runtime = decide.__self__
        runtime.clock = lambda:datetime.now(timezone.utc)
        calls = []
        canary = "synthetic-canary"
        def runner(*args,**kwargs):
            calls.append(args)
            raise OSError(canary)
        runtime.codex.runner = runner
        frozen = {"run_id":"START_FAILURE","input_snapshot_id":"START_FAILURE_INPUT","portfolio":{"account_state_version":1},
                  "review_scope":"FULL","reviewed_positions":[],"events":[],"facts":[],"candidates":[],"theses":[]}
        with self.assertRaisesRegex(AdapterError,"MODEL_PROCESS_FAILED") as caught:
            decide(frozen)
        self.assertNotIn(canary,str(caught.exception))
        self.assertEqual(len(calls),1)
        rows = list(app.store.db.execute("SELECT kind,payload FROM journal WHERE kind LIKE 'MODEL_%' ORDER BY sequence"))
        self.assertEqual([row["kind"] for row in rows],["MODEL_ATTEMPT","MODEL_OUTCOME"])
        for row in rows:
            self.assertNotIn(canary,row["payload"])
            record = json.loads(row["payload"])
            self.assertEqual(record["status"],"PROCESS_FAILED")
            self.assertIsNone(record["usage"])
        results = list((self.config.state_dir/"model-attempts").glob("*/*/result.json"))
        self.assertEqual(len(results),1)
        self.assertNotIn(canary,results[0].read_text())
        self.assertEqual(json.loads(rows[-1]["payload"])["attempt_count"],1)

    def test_paper_partial_fills_use_later_quotes_and_one_order_minimum_fee(self):
        _bundle,broker,decide,_refresh = self._factory()
        runtime = decide.__self__
        runtime.manifest["costs"]["minimum_buy_commission"] = "100"
        instant = [self.now]
        runtime.clock = lambda:instant[0]
        intent = {"plan_id":"FAKE_PAPER_PLAN","side":"BUY","quantity":2,"instrument_id":"KRX:000001",
                  "limit_price":"10000","expires_at":(self.now+timedelta(seconds=120)).isoformat()}
        ack = broker.submit(intent)
        base_quote = self.case[7].model_copy(update={"instrument_id":"KRX:000001","bid":Decimal(9999),"ask":Decimal(10000)})
        runtime.latest_bundle = SimpleNamespace(quotes={"KRX:000001":base_quote},data={"quote_depth":{"KRX:000001":{"ask":1,"bid":1}}})
        self.assertEqual(broker.snapshot()["orders"][0]["cumulative_quantity"],0)
        for seconds in (1,2):
            instant[0] = self.now+timedelta(seconds=seconds)
            runtime.latest_bundle.quotes["KRX:000001"] = base_quote.model_copy(update={"observed_at":instant[0],"received_at":instant[0]})
            result = broker.snapshot()
        order = next(item for item in result["orders"] if item["broker_id"] == ack["broker_id"])
        self.assertEqual(order["cumulative_quantity"],2)
        self.assertEqual(order["cumulative_fees"],"100")
        self.assertEqual(result["broker_available_cash"],"9979900")


if __name__ == "__main__":
    unittest.main()
