import os
from dataclasses import dataclass
from pathlib import Path


MCP_TRADING_ENV_VALUES = {"paper", "acct"}


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def required_env_bool(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        raise ValueError(f"{name} is required")
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else raw.strip().lower()
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {allowed_text}")
    return value


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    codex_bin: str
    codex_home: Path
    state_dir: Path
    workspace_dir: Path
    schedule_file: Path
    telegram_gateway_url: str
    telegram_route: str | None
    mcp_trading_env: str
    model: str
    reasoning_effort: str
    codex_timeout_seconds: int
    scheduler_poll_seconds: int
    telegram_typing_interval_seconds: float
    bypass_sandbox: bool
    new_session_prompt: str
    usage_script: Path
    usage_timeout_seconds: int
    bundled_skills_dir: Path
    sync_skills_overwrite: bool
    portfolio_file: Path

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            host=os.getenv("CODEX_EXEC_HOST", "0.0.0.0"),
            port=env_int("CODEX_EXEC_PORT", 8080),
            codex_bin=os.getenv("CODEX_BIN", "codex"),
            codex_home=Path(os.getenv("CODEX_HOME", "/codex-home")),
            state_dir=Path(os.getenv("STATE_DIR", "/state")),
            workspace_dir=Path(os.getenv("WORKSPACE_DIR", "/workspace")),
            schedule_file=Path(os.getenv("SCHEDULE_FILE", "/app/config/schedules.yaml")),
            telegram_gateway_url=os.getenv(
                "TELEGRAM_GATEWAY_URL",
                "http://telegram-gateway:8080/sendMessage",
            ),
            telegram_route=os.getenv("TELEGRAM_ROUTE", "").strip() or None,
            mcp_trading_env=env_choice(
                "CODEX_MCP_TRADING_ENV",
                "paper",
                MCP_TRADING_ENV_VALUES,
            ),
            model=os.getenv("CODEX_MODEL", "gpt-5.5"),
            reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "medium"),
            codex_timeout_seconds=env_int("CODEX_TIMEOUT_SECONDS", 1800),
            scheduler_poll_seconds=env_int("SCHEDULER_POLL_SECONDS", 15),
            telegram_typing_interval_seconds=env_float("TELEGRAM_TYPING_INTERVAL_SECONDS", 4.0),
            bypass_sandbox=env_bool("CODEX_BYPASS_APPROVALS_AND_SANDBOX", True),
            new_session_prompt=os.getenv("NEW_SESSION_PROMPT", "새 대화 시작"),
            usage_script=Path(os.getenv("CODEX_USAGE_SCRIPT", "/app/codex_usage.py")),
            usage_timeout_seconds=env_int("CODEX_USAGE_TIMEOUT_SECONDS", 20),
            bundled_skills_dir=Path(os.getenv("BUNDLED_SKILLS_DIR", "/app/skills")),
            sync_skills_overwrite=required_env_bool("CODEX_SYNC_SKILLS_OVERWRITE"),
            portfolio_file=Path(os.getenv("PORTFOLIO_FILE", "/app/config/portfolio.txt")),
        )
