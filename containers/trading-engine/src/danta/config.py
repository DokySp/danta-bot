"""Strict configuration snapshots and an explicit operator trust boundary."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
MODES = {"offline", "shadow", "paper", "broker_demo", "live"}


class ConfigurationError(ValueError):
    pass


class HumanRequired(ConfigurationError):
    def __init__(self, reason: str, state: str = "WAITING_FOR_HUMAN"):
        self.state = state
        super().__init__(reason)


class AccountObservationIncomplete(HumanRequired):
    """An account read failed; a later read may recover without operator changes."""


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key == "<<" or key in result:
            raise ConfigurationError("Duplicate, merge, or non-string YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def canonical(value: Any) -> str:
    def encode(item: Any) -> Any:
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("Non-finite Decimal")
            return str(item)
        if isinstance(item, datetime):
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("Timezone required")
            return item.astimezone(timezone.utc).isoformat()
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, Path):
            return str(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, default=encode, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(timezone.utc)


def _validate_shape(value: Any, contract: Any, path: str) -> None:
    if isinstance(contract, dict):
        if not isinstance(value, dict) or set(value) != set(contract):
            raise ConfigurationError(f"{path}: missing or unknown key")
        for key, template in contract.items():
            _validate_shape(value[key], template, f"{path}.{key}")
    elif isinstance(contract, list):
        if not isinstance(value, list):
            raise ConfigurationError(f"{path}: list required")
        if path.endswith(".jobs"):
            templates = {item["kind"]: item for item in contract}
            ids = set()
            for item in value:
                if not isinstance(item, dict) or item.get("kind") not in templates:
                    raise ConfigurationError("Unknown scheduler job kind")
                _validate_shape(item, templates[item["kind"]], path + ".job")
                if item["id"] in ids:
                    raise ConfigurationError("Duplicate scheduler id")
                ids.add(item["id"])
        else:
            exemplar = contract[0] if contract else ""
            for item in value:
                _validate_shape(item, exemplar, path + "[]")
    elif contract is None:
        # Null references stay unresolved; policies are fully checked at activation.
        allowed = (str, dict, list) if path.endswith(("accepted_risk_policy", "account_ownership_policy",
                   "inherited_positions_policy", "inherited_orders_policy", "permitted_order_types",
                   "approved_cost_allocation")) else (str,)
        if path.endswith("overnight_permission"):
            allowed = (bool,)
        if path.endswith("raw_log_retention_days"):
            allowed = (int,)
        if value is not None and (type(value) not in allowed):
            raise ConfigurationError(f"{path}: invalid optional value")
    elif type(value) is not type(contract):
        raise ConfigurationError(f"{path}: expected {type(contract).__name__}")
    elif isinstance(contract, str):
        try:
            Decimal(contract)
        except InvalidOperation:
            return
        try:
            number = Decimal(value)
        except InvalidOperation as error:
            raise ConfigurationError(f"{path}: decimal string required") from error
        if not number.is_finite() or number < 0:
            raise ConfigurationError(f"{path}: finite nonnegative value required")
        if any(part in path for part in ("fraction", "weight", "trigger", "confidence_level")) and number > 1:
            raise ConfigurationError(f"{path}: ratio outside [0,1]")


def validate_evaluation_profile(profile: dict) -> None:
    expected = json.loads((ROOT / "schemas/config-contract.json").read_text())["strategy"]["strategy"]["research_profile"]["evaluation"]
    if profile.get("evaluation") != expected:
        raise ConfigurationError("Unsupported preregistered evaluation protocol")


def _validate_semantics(data: dict) -> None:
    app = data["app"]
    strategy = data["strategy"]["strategy"]
    profile = strategy["research_profile"]
    schedules = data["schedules"]["scheduler"]
    if any(item["schema_version"] != 1 for item in data.values()):
        raise ConfigurationError("Unsupported schema version")
    if app["app"]["mode"] not in MODES or app["app"]["timezone"] != "Asia/Seoul":
        raise ConfigurationError("Invalid mode/timezone")
    if not 1 <= app["app"]["listen_port"] <= 65535:
        raise ConfigurationError("Invalid listen port")
    if app['model']['timeout_seconds'] <= 0:
        raise ConfigurationError('app.model.timeout_seconds: positive integer required')
    if strategy["active_profile"] not in {"research", "live"}:
        raise ConfigurationError("Unknown strategy profile")
    if schedules["calendar_ref"] != "app.market.calendar_manifest" or schedules["timezone"] != "Asia/Seoul":
        raise ConfigurationError("Invalid calendar reference/timezone")
    if Decimal(profile["capital_krw"]) <= 0:
        raise ConfigurationError("Positive research capital required")
    for section, values in profile.items():
        if isinstance(values, dict):
            for key, value in values.items():
                if type(value) is int and value <= 0:
                    raise ConfigurationError(f"{section}.{key}: positive integer required")
    if profile["universe"]["boards"] not in (["KOSPI"], ["KOSDAQ"], ["KOSPI", "KOSDAQ"]):
        raise ConfigurationError("Unsupported boards")
    fixed = {
        ("universe", "instrument_kind"): "common_stock", ("universe", "venue"): "KRX",
        ("universe", "session"): "regular_continuous", ("universe", "leverage"): False,
        ("universe", "short_selling"): False, ("universe", "adtv_statistic"): "median",
        ("signal", "atr_method"): "simple_mean_true_range",
        ("portfolio", "pyramiding"): False, ("portfolio", "averaging_down"): False,
        ("portfolio", "price_drift_rebalance"): False, ("exits", "stop_can_move_down"): False,
        ("exits", "auto_resume_after_drawdown"): False, ("orders", "entry_auto_reprice"): False,
        ("orders", "entry_type"): "limit_at_verified_ask", ("orders", "exit_type"): "market_in_valid_session",
        ("costs", "unknown_cost_is_zero"): False, ("evaluation", "automatic_live_promotion"): False,
        ("universe", "overnight"): True, ("universe", "weekend_holding"): True,
        ("universe", "watchlist"): [], ("signal", "primary_source_required"): True,
        ("signal", "index_entry_gate"): "close_at_or_above_sma60",
        ("exits", "trend_exit_consecutive_closes"): 2,
        ("exits", "reentry_requires_close_above_previous_entry"): True,
        ("exits", "drawdown_action"): "cancel_entries_pause_new_risk_keep_protection",
        ("orders", "respect_pending_orders"): True,
        ("costs", "commission_schedule"): None, ("costs", "tax_schedule"): None,
        ("costs", "operating_cost_allocation"): None,
        ("costs", "simulation_slippage_bps_per_side"): "10",
    }
    for (section, key), expected in fixed.items():
        if profile[section][key] != expected:
            raise ConfigurationError(f"Unsupported strategy semantics: {section}.{key}")
    validate_evaluation_profile(profile)
    if schedules["discretionary_entry_window"] != {
            "start_minutes_after_continuous_open": 20, "end_minutes_before_continuous_close": 30}:
        raise ConfigurationError("Unsupported discretionary entry window: supported offsets are 20/30 minutes")
    if app["model"]["auto_fallback"] or not app["execution"]["single_writer"] or not app["execution"]["live_requires_trusted_approval"]:
        raise ConfigurationError("Required authority boundary disabled")
    if app["model"]["provider"] != "codex_cli" or app["broker"]["provider"] != "kis":
        raise ConfigurationError("Unsupported provider")
    if app["model"]["isolation_profile"] != "market_tools_only":
        raise ConfigurationError("Unsupported model isolation profile")
    if app["telegram"]["route"] != "trading-engine":
        raise ConfigurationError("Telegram route must be trading-engine")
    telegram = app["telegram"]
    default_chat = telegram["default_chat_id"]
    if default_chat is not None and default_chat not in telegram["allowed_chat_ids"]:
        raise ConfigurationError("telegram.default_chat_id must be an allowed chat")
    if telegram["enabled"] and len(telegram["allowed_chat_ids"]) > 1 and default_chat is None:
        raise ConfigurationError("Multiple Telegram chats require telegram.default_chat_id for automatic reports")


def model_reload_hash(data):
    """Only model selection, reasoning effort and timeout may reload between calls."""
    data = json.loads(canonical(data))
    for key in ('model_id', 'reasoning_effort', 'timeout_seconds'):
        data['app']['model'].pop(key)
    return digest(data)


@dataclass(frozen=True)
class Config:
    snapshot_json: str
    config_hash: str
    strategy_hash: str
    directory: Path
    source_config_hash: str | None = None
    model_reload_baseline: str | None = None

    @property
    def data(self) -> dict:
        return json.loads(self.snapshot_json)

    @property
    def app(self) -> dict:
        return self.data["app"]

    @property
    def research(self) -> dict:
        return self.data["strategy"]["strategy"]["research_profile"]

    @property
    def mode(self) -> str:
        return self.app["app"]["mode"]

    @property
    def state_dir(self) -> Path:
        path = Path(self.app["app"]["state_dir"])
        return path if path.is_absolute() else (self.directory.parent / path).resolve()

    def assert_current(self) -> None:
        self._current_source()

    def _current_source(self):
        current = load_config(self.directory)
        if current.config_hash == (self.source_config_hash or self.config_hash):
            return current
        if self.model_reload_baseline and model_reload_hash(current.data) == self.model_reload_baseline:
            return current
        raise HumanRequired("POLICY_CHANGED: frozen run cannot acquire new authority")

    def model_settings(self):
        current = self._current_source()
        return current.app['model'] if self.model_reload_baseline else self.app['model']

    def require_external(self, capability: str, approval: dict | None = None) -> None:
        if self.mode == "offline":
            raise HumanRequired("OFFLINE_NETWORK_FORBIDDEN")
        if not approval or capability not in approval.get("capabilities", []):
            raise HumanRequired(f"Missing trusted approval: {capability}")
        if approval.get("config_hash") != self.config_hash or aware_time(approval["expires_at"]) <= utcnow():
            raise HumanRequired("Expired or mismatched approval")


def load_config(directory: str | Path | None = None) -> Config:
    directory = Path(directory or ROOT / "config").resolve()
    try:
        contract = (ROOT / "schemas/config-contract.json").read_text()
        sources = tuple((directory / f"{name}.yaml").read_text() for name in ("app", "strategy", "schedules"))
    except OSError as error:
        raise ConfigurationError(f"Configuration could not be loaded: {type(error).__name__}") from error
    return _parse_config(directory, contract, sources)


@lru_cache(maxsize=8)
def _parse_config(directory: Path, contract_text: str, sources: tuple[str, ...]) -> Config:
    # Read the actual bytes on every authority check; reuse only identical validated content.
    contract = json.loads(contract_text)
    data = {}
    try:
        for name, source in zip(("app", "strategy", "schedules"), sources):
            data[name] = yaml.load(source, Loader=StrictLoader)
            if name == 'app' and isinstance(data[name], dict) and isinstance(data[name].get('telegram'), dict):
                data[name]['telegram'].setdefault('default_chat_id', None)
            if name == 'strategy':
                try:
                    orders = data[name]['strategy']['research_profile']['orders']
                    orders.setdefault('monitor_quote_max_age_seconds', orders['quote_max_age_seconds'])
                except (KeyError, TypeError, AttributeError):
                    pass  # The shape validator reports malformed legacy configurations.
            _validate_shape(data[name], contract[name], name)
    except (yaml.YAMLError, OSError) as error:
        raise ConfigurationError(f"Configuration could not be loaded: {type(error).__name__}") from error
    _validate_semantics(data)
    strategy = data["strategy"]["strategy"]
    policy = strategy["research_profile"] if strategy["active_profile"] == "research" else strategy["live_mandate"]["accepted_risk_policy"]
    return Config(canonical(data), digest(data), digest({"id": strategy["id"], "policy": policy}), directory)


def load_secrets(directory: str | Path) -> dict[str, str]:
    """Read private runtime values separately from policy snapshots and hashes."""
    try:
        descriptor = os.open(Path(directory) / "secrets.yaml", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
                raise ValueError
            content = stream.read(65537)
            if len(content) > 65536:
                raise ValueError
            values = yaml.load(content, Loader=StrictLoader)
        if not isinstance(values, dict) or any(
            not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or not isinstance(value, str) for key, value in values.items()
        ):
            raise ValueError
        return values
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        # YAML exceptions can contain source lines; never propagate their text.
        raise HumanRequired("Private config/secrets.yaml is missing, invalid, or requires mode 0400/0600") from None


def trusted_approval(path: Path, config: Config, expected_id: str, now: datetime | None = None) -> dict:
    """A root-managed, read-only approval file is the local operator authority.

    It is never created by the model, Telegram, or the trading application.
    The deployment must mount the directory read-only into the application.
    """
    now = now or utcnow()
    path = path.absolute()
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise HumanRequired("Approval path must be root-owned and not group/world writable")
    approval = json.loads(path.read_text())
    required = {"schema_version", "id", "config_hash", "strategy_hash", "account_alias", "environment",
                "code_id", "model_id", "prompt_hash", "capabilities", "issued_at", "expires_at", "operational_evidence"}
    if set(approval) != required or approval["schema_version"] != 1 or approval["id"] != expected_id:
        raise HumanRequired("Invalid trusted approval envelope")
    if not aware_time(approval["issued_at"]) <= now < aware_time(approval["expires_at"]):
        raise HumanRequired("Approval outside validity window")
    if approval["config_hash"] != config.config_hash or approval["strategy_hash"] != config.strategy_hash:
        raise HumanRequired("Approval hashes do not match")
    if approval["account_alias"] != config.app["app"]["account_alias"] or approval["environment"] != config.app["broker"]["environment"]:
        raise HumanRequired("Approval account/environment mismatch")
    return approval


def validate_activation(config: Config, approval: dict, expected_hash: str, code_id: str) -> None:
    config.assert_current()
    if config.mode not in {"live", "broker_demo"} or expected_hash != config.config_hash:
        raise HumanRequired("Live/demo mode and expected configuration hash required")
    if not config.app["execution"]["enabled"]:
        raise HumanRequired("Execution is not enabled")
    if config.mode == "broker_demo":
        if config.app["broker"]["environment"] != "demo" or approval["environment"] != "demo":
            raise HumanRequired("Demo activation requires matching demo environment")
        if approval["code_id"] != code_id or approval["model_id"] != config.app["model"]["model_id"]:
            raise HumanRequired("Code/model approval mismatch")
        config.require_external("demo_orders", approval)
        return
    mandate = config.data["strategy"]["strategy"]["live_mandate"]
    if any(value is None for value in mandate.values()) or mandate["status"] != "approved":
        raise HumanRequired("Complete approved live mandate required; research is not inherited")
    if mandate["accepted_strategy_hash"] != config.strategy_hash or mandate["trusted_approval_id"] != approval["id"]:
        raise HumanRequired("Mandate does not name this strategy/approval")
    if config.data["strategy"]["strategy"]["active_profile"] != "live" or not config.app["execution"]["enabled"]:
        raise HumanRequired("Live policy/execution not enabled")
    if Decimal(mandate["capital_krw"]) <= 0 or mandate["overnight_permission"] is not True:
        raise HumanRequired("Capital and overnight policy conflict with the requested swing strategy")
    risk = mandate["accepted_risk_policy"]
    if not isinstance(risk, dict) or set(risk) != set(config.research):
        raise HumanRequired("Complete live policy required; partial research inheritance forbidden")
    _validate_shape(risk, config.research, "live_mandate.accepted_risk_policy")
    policy_data = config.data
    policy_data["strategy"]["strategy"]["research_profile"] = risk
    _validate_semantics(policy_data)
    if Decimal(risk["capital_krw"]) != Decimal(mandate["capital_krw"]):
        raise HumanRequired("Live policy capital and mandate allocation differ")
    if set(mandate["permitted_order_types"]) != {"limit", "market"}:
        raise HumanRequired("Mandate must allow the specified limit entry and market exit")
    if approval["code_id"] != code_id or approval["model_id"] != config.app["model"]["model_id"]:
        raise HumanRequired("Code/model approval mismatch")
    if approval["prompt_hash"] != hashlib.sha256((ROOT / "prompts/portfolio_decision.md").read_bytes()).hexdigest():
        raise HumanRequired("Prompt approval mismatch")
    evidence = {"account_reconciled", "ownership_reconciled", "single_writer", "local_storage", "backup_restore",
                "model_isolation", "cost_schedule", "calendar", "quote_timestamp", "protection_performance"}
    if approval.get("authority") == "deployment_config":
        # Fresh quotes, market phase, account version and deadlines are checked on
        # each order, including protective sells. They cannot be proven at an
        # off-hours container startup with no quote or pending order.
        from .deployment import require_operator_config
        require_operator_config(load_config(config.directory))
        evidence -= {"quote_timestamp", "protection_performance"}
    if not evidence.issubset(approval["operational_evidence"]) or any(approval["operational_evidence"][key] is not True for key in evidence):
        raise HumanRequired("Operational validation evidence is incomplete")
    config.require_external("live_orders", approval)
