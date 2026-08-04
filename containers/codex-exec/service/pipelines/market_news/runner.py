"""Structured scheduler runner for market_news jobs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .collector import collect_market_news, resolve_db_path


class MarketNewsDirectRunner:
    def __init__(self, config: Any) -> None:
        self.config = config

    def run(self, raw_config: Any) -> dict[str, Any]:
        payload = raw_config if isinstance(raw_config, dict) else {}
        db_path = resolve_db_path(Path(self.config.workspace_dir), payload.get("db_path"))
        result = collect_market_news(db_path=db_path)
        logging.info(
            "market-news collection status=%s fetched=%s inserted=%s duplicates=%s",
            result.get("status"),
            result.get("fetched_count", 0),
            result.get("inserted_count", 0),
            result.get("duplicate_count", 0),
        )
        if result.get("status") == "failed":
            errors = "; ".join(str(item) for item in result.get("errors") or [])
            raise RuntimeError(f"market-news collection failed: {errors or 'unknown source failure'}")
        return result
