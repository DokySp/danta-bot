"""Resolve an operator-owned deployment into an observed runtime at startup.

The mounted YAML policy is the authority. Account snapshots and protocol checks
are evidence produced by the application, never operator-authored success flags.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile

from .config import Config, HumanRequired, ROOT, canonical, digest, load_secrets, utcnow

READ_CAPABILITIES = ["account_read", "market_read", "disclosure_read", "broker_auth", "model_call"]


def require_operator_config(config):
    config.assert_current()
    for path in (config.directory, *(config.directory / (name + ".yaml")
                                    for name in ("app", "strategy", "schedules"))):
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or path != config.directory and not stat.S_ISREG(info.st_mode)):
            raise HumanRequired("DEPLOYMENT_CONFIG_MUST_BE_ROOT_OWNED_READ_ONLY")
    app = config.app
    mandate = config.data["strategy"]["strategy"]["live_mandate"]
    if (config.mode != "live" or app["broker"]["environment"] != "real"
            or app["broker"]["capability_manifest"] != "automatic"
            or not app["execution"]["enabled"]
            or config.data["strategy"]["strategy"]["active_profile"] != "live"
            or mandate["status"] != "approved"
            or mandate["capital_krw"] != "entire_account"
            or mandate["accepted_risk_policy"] != "configured_profile"
            or mandate["account_ownership_policy"] != "entire_account"
            or mandate["inherited_positions_policy"] != "adopt_and_protect"
            or mandate["inherited_orders_policy"] != "require_no_active_orders"):
        raise HumanRequired("EXPLICIT_DEPLOYMENT_POLICY_REQUIRED")


def resolved_config(source, capital, secrets):
    data = source.data
    strategy = data["strategy"]["strategy"]
    policy = dict(strategy["research_profile"], capital_krw=str(capital))
    strategy_hash = digest({"id": strategy["id"], "policy": policy})
    strategy["live_mandate"].update(capital_krw=str(capital), accepted_risk_policy=policy,
        accepted_strategy_hash=strategy_hash, trusted_approval_id="deployment-config")
    tg = data["app"]["telegram"]
    for key, env, pattern in (("allowed_sender_ids", "TELEGRAM_ALLOWED_SENDER_IDS", r"[1-9]\d*"),
                              ("allowed_chat_ids", "TELEGRAM_ALLOWED_CHAT_IDS", r"-?[1-9]\d*")):
        if not tg[key]:
            tg[key] = [value.strip() for value in secrets.get(env, "").split(",") if value.strip()]
        if tg["enabled"] and (not tg[key] or any(not re.fullmatch(pattern, value) for value in tg[key])):
            raise HumanRequired("TELEGRAM_ALLOWLIST_REQUIRED_IN_SECRETS")
    from .config import model_reload_hash
    return Config(canonical(data), digest(data), strategy_hash, source.directory, source.config_hash,
                  model_reload_hash(source.data))


def model_evidence(config, secrets):
    from .adapters.isolation_probe import probe
    from .adapters.codex_cli import auth_preflight
    settings = config.app["model"]
    executable = shutil.which(settings["executable"])
    if not executable:
        raise HumanRequired("CODEX_EXECUTABLE_MISSING")
    auth = secrets.get("DANTA_CODEX_AUTH_HOME")
    if not auth:
        raise HumanRequired("CODEX_AUTH_HOME_MISSING")
    authentication = auth_preflight(auth, settings["auth_mode"])
    if authentication != "AUTHENTICATED":
        raise HumanRequired("CODEX_" + authentication)
    # Never copy auth into the image, model inputs or a probe transcript.
    result = subprocess.run([executable, "-c", 'cli_auth_credentials_store="file"', "login", "status"], capture_output=True, timeout=15,
        env={"PATH": os.environ.get("PATH", ""), "HOME": auth, "CODEX_HOME": auth}, shell=False)
    if result.returncode:
        raise HumanRequired("CODEX_LOGIN_REQUIRED")
    try:
        evidence = probe(settings["model_id"])
    except AssertionError:
        from .adapters import AdapterError
        raise AdapterError('MODEL_ISOLATION_PROBE_FAILED') from None
    return {"source": "runtime local-loopback isolation probe", "isolation_verified": True,
            "executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
            "auth_home_env": "DANTA_CODEX_AUTH_HOME", "probe": evidence}


def estimated_costs(alias, now):
    # Explicit sizing estimate, not the customer's fee agreement. Actual money
    # comes from the broker's account cash and daily fee/tax reconciliation.
    return {"source": "https://securities.koreainvestment.com/main/customer/guide/_static/TF04ae010000.jsp",
        "basis": "CONSERVATIVE_ESTIMATE", "account_alias": alias, "venue": "KRX",
        "effective_at": now.isoformat(), "expires_at": (now + timedelta(days=2)).isoformat(),
        "verified": True, "synthetic": False, "buy_commission_rate": "0.005",
        "sell_commission_rate": "0.005", "sell_tax_rate": "0.002",
        "minimum_buy_commission": "0", "minimum_sell_commission": "0",
        "buy_slippage_bps": "10", "sell_slippage_bps": "10"}


def prepare_application(source, *, kis_transport=None, dart_transport=None, model_runner=None,
                        clock=utcnow, ws_connector=None):
    from .adapters import http_transport
    from .adapters.disclosures import DartAdapter, ORIGIN
    from .adapters.kis import BASE_URLS, MASTER_ORIGIN, KisAdapter, KisCredentials, KisTokenCache
    from .application import Application, code_identity
    from .deployment_sources import prepare_market_sources
    from .runtime import KisBrokerPort, PriorityTransport, RuntimeState, build_external_runtime
    from .service import log_event
    from .store import Store

    require_operator_config(source)
    secrets = load_secrets(source.directory)
    reference = secrets.get(source.app["broker"]["account_ref_env"], "")
    if not re.fullmatch(r"\d{8}-\d{2}", reference):
        raise HumanRequired("KIS_ACCOUNT_REFERENCE_REQUIRED")
    account, product = reference.split("-")
    identity = "kis:" + hashlib.sha256(("real:" + reference).encode()).hexdigest()
    credentials = KisCredentials(account=account, product=product,
        app_key=secrets.get(source.app["broker"]["app_key_env"], ""),
        app_secret=secrets.get(source.app["broker"]["app_secret_env"], ""), token="")
    # The writer lock is acquired before any account bootstrap can be persisted.
    store = Store(source.state_dir / "state.sqlite", mode="live", account_identity=identity, initial_cash=Decimal(0))
    state = kis = app = runtime = None
    current = source
    grant = {"schema_version": 1, "id": "deployment-config", "authority": "deployment_config",
        "config_hash": source.config_hash, "strategy_hash": source.strategy_hash,
        "account_alias": source.app["app"]["account_alias"], "environment": "real",
        "code_id": code_identity(), "model_id": source.app["model"]["model_id"],
        "prompt_hash": hashlib.sha256((ROOT / "prompts/portfolio_decision.md").read_bytes()).hexdigest(),
        "capabilities": list(READ_CAPABILITIES), "issued_at": clock().isoformat(),
        # Operator YAML authorization persists until changed. Market data and
        # order deadlines retain their own short validity checks.
        "expires_at": "9999-01-01T00:00:00+00:00", "operational_evidence": {}}
    def authorize(operation, _environment=None):
        capability = {"broker_auth": "broker_auth", "broker_read": "account_read", "market_read": "market_read",
            "disclosure_read": "disclosure_read", "broker_write": "live_orders"}[operation]
        current.assert_current()
        current.require_external(capability, grant)
    try:
        log_event("INITIALIZING", stage="codex_login_and_isolation")
        isolation = model_evidence(source, secrets)
        transport = PriorityTransport(kis_transport or http_transport(
            allowed_origins={BASE_URLS["real"], MASTER_ORIGIN}, network_enabled=True),
            minimum_interval_seconds="0.25", maximum_queue_seconds="2")
        token = KisTokenCache(environment="real", app_key=credentials.app_key, app_secret=credentials.app_secret,
            path=source.state_dir / "kis-token.json", transport=transport, mode="live", authorize=authorize, clock=clock)
        kis = KisAdapter(environment="real", credentials=credentials, transport=transport, mode="live",
            authorize=authorize, token_provider=token, clock=clock, ws_connector=ws_connector)
        dart = DartAdapter(api_key=secrets.get(source.app["market"]["dart_key_env"], ""), mode="live",
            authorize=authorize, official_ir_domains=source.app["market"]["official_ir_domains"],
            transport=dart_transport or http_transport(allowed_origins={ORIGIN,
                *("https://" + host for host in source.app["market"]["official_ir_domains"])}, network_enabled=True))
        state = RuntimeState(source.state_dir / "state.sqlite")
        now = clock()
        template = {"account_alias": grant["account_alias"], "bootstrap": {"whole_account": True, "account_identity": identity,
            "orders_since": (now.date() - timedelta(days=89)).isoformat(), "strategy_quantities": {},
            "external_quantities": {}, "external_order_keys": [], "strategy_cash": "0"},
            "normalization": {"account": {"resource_symbol": "005930", "resource_price": "1",
                "available_cash": "nrcvb_buy_amt", "symbol": "pdno", "quantity": "hldg_qty", "sellable_quantity": "ord_psbl_qty"},
                "orders": {"symbol": "pdno", "session_date": "ord_dt", "broker_id": "odno", "quantity": "ord_qty",
                    "cumulative_quantity": "tot_ccld_qty", "cumulative_notional": "tot_ccld_amt", "side": "sll_buy_dvsn_cd",
                    "side_codes": {"02": "BUY", "01": "SELL"}, "canceled_quantity": "cncl_cfrm_qty",
                    "day_order_fill_session_verified": True}}}
        log_event("INITIALIZING", stage="account_and_order_reconciliation")
        census = KisBrokerPort(kis, template, state, clock=clock).census()
        if not census["complete"] or census.get("errors"):
            raise HumanRequired("ACCOUNT_CENSUS_INCOMPLETE")
        saved = store.get("deployment_bootstrap") if store.get("account_adoption") else None
        if store.get("account_adoption") and not saved:
            raise HumanRequired("DEPLOYMENT_BASELINE_MISSING")
        if saved:
            if saved["account_identity"] != identity:
                raise HumanRequired("DEPLOYMENT_ACCOUNT_IDENTITY_CHANGED")
            bootstrap, capital = saved["bootstrap"], Decimal(saved["capital"])
        else:
            if any(row["state"] not in {"FILLED", "CANCELED", "PARTIAL_CANCELED", "REJECTED", "EXPIRED"} for row in census["orders"]) or census["reservations"]:
                raise HumanRequired("EXISTING_ACTIVE_OR_RESERVED_ORDERS_REQUIRE_RESOLUTION")
            capital = Decimal(census["account_nav"])
            if capital <= 0:
                raise HumanRequired("ACCOUNT_ALLOCATION_EMPTY")
            bootstrap = dict(template["bootstrap"], source="KIS whole-account census at adoption",
                ownership_verified=True, strategy_cash=str(census["economic_cash"]),
                strategy_quantities=census["quantities"],
                baseline_orders={row["key"]: row["fingerprint"] for row in census["orders"]})
        current = resolved_config(source, capital, secrets)
        grant.update(config_hash=current.config_hash, strategy_hash=current.strategy_hash)
        log_event("INITIALIZING", stage="market_calendar_and_issuer_sources")
        sources = prepare_market_sources(kis, dart, now=now)
        # Initial event coverage includes recent sessions. Durable collection
        # cursors are never truncated on restart.
        sources["disclosures"]["start_date"] = (now.date() - timedelta(days=31)).isoformat()
        template["normalization"].update(sources["normalization"])
        manifest = dict(template, schema_version=1, source="automatic provider observations", verified=True,
            automatic=True, account_alias=grant["account_alias"], environment="real", effective_at=now.isoformat(),
            expires_at=(now + timedelta(days=2)).isoformat(), credentials={"managed_token": True},
            calendar=sources["calendar"], ticks=sources["ticks"], costs=estimated_costs(grant["account_alias"], now),
            bootstrap=bootstrap, model=isolation, disclosures=sources["disclosures"],
            rate_limit={"source": "local 4 requests/sec budget with shared provider cooldown; reserved broker priority", "verified": True,
                "minimum_interval_seconds": "0.25", "maximum_queue_seconds": "2"})
        bundle, broker, decide, refresh = build_external_runtime(current, grant, manifest=manifest,
            kis=kis, dart=dart, state=state, store=store, env=secrets, clock=clock, model_runner=model_runner)
        runtime = refresh.__self__
        broker.bind_store(store)
        app = Application(current, bundle, broker=broker, decide=decide, refresh=refresh,
            protection_refresh=runtime.refresh_protection, quote_refresh=runtime.refresh_quotes,
            decision_refresh=runtime.refresh_decision, approval=grant, chat=runtime.chat)
        app.adopt_account(bootstrap, deployment_bootstrap={"source_config_hash": source.config_hash,
            "account_identity": identity, "capital": str(capital), "bootstrap": bootstrap})
        if not saved:
            store.reconcile_account_cash({"cash_krw": census["economic_cash"],
                "observed_at": census["account_observed_at"],
                "source": "KIS:inquire-balance:prvs_rcdl_excc_amt",
                "daily_costs": census["daily_costs"], "cost_quality": census["cost_quality"]})
        reconciled = app.reconcile()
        if reconciled["status"] != "RECONCILED":
            raise HumanRequired("ACCOUNT_RECONCILIATION_REQUIRED")
        with tempfile.TemporaryDirectory(dir=current.state_dir, prefix="backup-check-") as directory:
            backup, restored = Path(directory) / "backup.sqlite", Path(directory) / "restored.sqlite"
            store.backup(backup)
            Store.restore(backup, restored)
            with sqlite3.connect(restored) as check:
                if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise HumanRequired("BACKUP_RESTORE_FAILED")
        grant["operational_evidence"] = {"account_reconciled": True, "ownership_reconciled": True,
            "single_writer": True, "local_storage": True, "backup_restore": True, "model_isolation": True,
            "cost_schedule": True, "calendar": True}
        grant["capabilities"] += ["live_orders", "telegram_ingress", "telegram_send", "telegram_control",
                                  "candidate_control", "resume", "resume_drawdown"]
        with state.lock:
            state.data["deployment_evidence"] = {"observed_at": clock().isoformat(), "manifest": manifest,
                "approval": grant, "source_config_hash": source.config_hash}
            state.save()
        log_event("INITIALIZATION_COMPLETE", mode=current.mode, holdings=len(bootstrap["strategy_quantities"]),
                  cost_basis="CONSERVATIVE_ESTIMATE_WITH_BROKER_CASH_RECONCILIATION")
        return app
    except BaseException:
        if app:
            app.close()
        else:
            if kis:
                kis.close()
            if state:
                state.db.close()
            store.close()
        raise
