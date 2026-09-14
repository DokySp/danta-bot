"""Injected transport only. All account numbers/credentials/data here are FAKE."""
import hashlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from datetime import timedelta
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

    def __call__(self,method,url,headers=None,body=None,timeout=15):
        self.calls.append((method,urlsplit(url).path))
        path = urlsplit(url).path
        if path.endswith("/oauth2/tokenP"):
            return HttpResponse(200,json.dumps({"access_token":"FAKE_TOKEN_NOT_A_CREDENTIAL",
                "access_token_token_expired":(self.now + timedelta(hours=12)).astimezone(
                    ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')}).encode())
        if path.endswith("list.json"):
            return HttpResponse(200,json.dumps({"status":"013"}).encode())
        if path.endswith("inquire-daily-itemchartprice"):
            data = [{"stck_bsop_date":b.session_id.replace("-",""),"stck_hgpr":str(b.high),"stck_lwpr":str(b.low),
                     "stck_clpr":str(b.close),"acml_tr_pbmn":str(b.turnover)} for b in self.bars]
            return HttpResponse(200,json.dumps({"rt_cd":"0","output2":data}).encode())
        if path.endswith("inquire-daily-indexchartprice"):
            data = [{"stck_bsop_date":b.session_id.replace("-",""),"bstp_nmix_hgpr":str(b.high),"bstp_nmix_lwpr":str(b.low),
                     "bstp_nmix_prpr":str(b.close),"acml_tr_pbmn":str(b.turnover)} for b in self.index_bars]
            return HttpResponse(200,json.dumps({"rt_cd":"0","output2":data}).encode())
        if path.endswith("inquire-balance"):
            return HttpResponse(200,json.dumps({"rt_cd":"0","output1":[],"output2":[{}]}).encode())
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
        shutil.copytree(ROOT/"config",self.config_dir,ignore=shutil.ignore_patterns("secrets.yaml"))
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
                          "side":"sll_buy_dvsn_cd","side_codes":{"02":"BUY","01":"SELL"},"canceled_quantity":"cncl_cfrm_qty","day_order_fill_session_verified":True}},
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
        config = load_config(ROOT/"config")
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

    def test_known_fill_with_unknown_fees_is_preserved_for_protection(self):
        state = RuntimeState(self.base/"broker.sqlite")
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
                                     "tot_ccld_amt":"30000","sll_buy_dvsn_cd":"02","cncl_cfrm_qty":"0"},),"COMPLETE",self.now)
        broker = KisBrokerPort(FakeAdapter(),self.manifest,state,clock=lambda:self.now)
        snapshot = broker.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["strategy_quantities"],{"KRX:000001":3})
        self.assertEqual(snapshot["orders"][0]["cumulative_quantity"],3)
        self.assertIsNone(snapshot["orders"][0]["cumulative_fees"])
        self.assertEqual(snapshot["orders"][0]["fill_time_quality"],"FIRST_OBSERVED")
        self.assertIsNone(snapshot["orders"][0]["first_fill_at"])

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
        self.assertEqual(usage["status"],"RECORDED_ATTEMPTS")
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
