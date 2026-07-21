#!/usr/bin/env python3
"""Tests for deterministic daily-trading artifact construction."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path

from ..scripts.build_run_artifacts import (
    ANALYST_REVIEW_SPEC_ROLES,
    CHART_RECENT_ROW_LIMITS,
    COMBINED_ANALYST_REVIEW_ROLES,
    STRATEGY_POLICY_CONFIG_ENV,
    build_analyst_review,
    build_decision_brief,
    build_execution_plan,
    build_first_specs,
    build_second_spec,
    build_strategy_context,
    build_symbol_strategy_context,
    build_token_summary,
    compact_summary_is_usable,
    default_strategy_policy_config_path,
    etf_summary_for,
    expected_news_calendar_date,
    financial_summary_for,
    load_json,
    load_strategy_policy_config,
    mark_quality_value_excluded_without_financial,
    news_summary_for,
    pipeline_dir,
    today_trade_collection_context,
    today_trade_context,
    write_json,
)


def run_self_test() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        run_dir = tmp / "reports" / "runs" / "daily-trading-test"
        portfolio = {
            "recommanded": [],
            "specified": ["005930", "000660"],
            "holding": ["005930"],
            "universe": ["005930", "000660"],
        }
        write_json(tmp / "portfolio.json", portfolio)
        daily_rows = [
            {
                "date": f"202606{18 - index:02d}",
                "open": 69000 - (index * 1000),
                "high": 71000 - (index * 1000),
                "low": 68000 - (index * 1000),
                "close": 70000 - (index * 1000),
                "volume": 1000 + (index * 100),
                "trading_value": (70000 - (index * 1000)) * (1000 + (index * 100)),
            }
            for index in range(21)
        ]
        weekly_rows = [
            {
                "date": f"2026W{24 - index:02d}",
                "open": 68000 - (index * 500),
                "high": 70000 - (index * 500),
                "low": 67000 - (index * 500),
                "close": 69500 - (index * 500),
                "volume": 5000 + (index * 200),
            }
            for index in range(8)
        ]
        monthly_rows = [
            {
                "date": f"2026{6 - index:02d}",
                "open": 66000 - (index * 700),
                "high": 70500 - (index * 700),
                "low": 65000 - (index * 700),
                "close": 69000 - (index * 700),
                "volume": 12000 + (index * 300),
            }
            for index in range(6)
        ]
        intraday_rows = [
            {"time": f"09{30 + index:02d}00", "price": 70000 + (index * 100), "volume": 100 + index}
            for index in range(7)
        ]
        write_json(
            run_dir / "price-chart.json",
            {
                "run_id": "daily-trading-test",
                "started_at": "2026-06-18 09:00:00 KST",
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "product_type": "stock",
                        "eligible_for_review": True,
                        "price": {"current_or_last": 70000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                        "local_signals": [
                            {"name": "day_change_pct", "value": 1.2},
                            {"name": "daily_pct_vs_ma20", "value": 3.4},
                        ],
                        "charts": {
                            "daily": daily_rows,
                            "weekly": weekly_rows,
                            "monthly": monthly_rows,
                        },
                        "intraday": intraday_rows,
                        "orderbook_summary": {"best_bid": 69900, "best_ask": 70000, "spread_pct": 0.143},
                        "trade_flow_summary": {"tick_count": 3, "recent_price_change_pct": 0.2},
                        "investor_flow_summary": {"foreign_net_buy_quantity": 1000},
                        "required_missing": [],
                        "errors": [],
                    },
                    {
                        "symbol_id": "000660",
                        "symbol_name": "SK하이닉스",
                        "product_type": "stock",
                        "eligible_for_review": True,
                        "price": {"current_or_last": 200000, "observed_at": "2026-06-18T09:00:00+09:00", "snapshot_mode": "live"},
                        "local_signals": [],
                        "required_missing": [],
                        "errors": [],
                    },
                ],
            },
        )
        write_json(
            run_dir / "account-before-order.json",
            {
                "run_id": "daily-trading-test",
                "started_at": "2026-06-18 09:00:00 KST",
                "active_order_lookup_performed": False,
                "order_available_lookup_performed": False,
                "account_summary": {"cash_amount": 1000000, "total_evaluation_amount": 1500000},
                "active_orders": [],
                "symbols": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "current_live_holding_quantity": 1,
                        "current_price": 70000,
                        "valuation_amount": 70000,
                        "pnl_amount": -2500,
                        "pnl_rate": -3.5,
                        "average_purchase_price": 72500.0,
                        "purchase_amount": 72500,
                    },
                    {
                        "symbol_id": "000660",
                        "symbol_name": "SK하이닉스",
                        "current_live_holding_quantity": 0,
                        "current_price": 200000,
                        "valuation_amount": 0,
                    },
                ],
            },
        )
        write_json(
            run_dir / "today-fills.json",
            {
                "schema_version": "1",
                "stage": "today-fills",
                "status": "success",
                "skipped": False,
                "symbols": [{"symbol_id": "005930"}, {"symbol_id": "000660"}],
                "fills": [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "direction": "buy",
                        "filled_at": "2026-06-18T09:31:00+09:00",
                        "filled_quantity": 1,
                        "filled_price": 70100,
                        "filled_amount": 70100,
                        "order_id": "fill-1",
                        "source_actor": "bot_opnapi",
                    },
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "direction": "buy",
                        "filled_at": "2026-06-18T10:20:00+09:00",
                        "filled_quantity": 1,
                        "filled_price": 70300,
                        "filled_amount": 70300,
                        "order_id": "fill-3",
                        "source_actor": "non_bot_user",
                    },
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "direction": "sell",
                        "filled_at": "2026-06-18T10:10:00+09:00",
                        "filled_quantity": 1,
                        "filled_price": 70200,
                        "filled_amount": 70200,
                        "order_id": "fill-2",
                        "source_actor": "bot_opnapi",
                    },
                ],
                "errors": [],
            },
        )
        financial_cache_path = tmp / "memory" / "collect-financial-information" / "financial-2026-06-18.yaml"
        financial_cache_path.parent.mkdir(parents=True, exist_ok=True)
        financial_cache_path.write_text(
            '''date: "2026-06-18"
source: kis_open_api
symbols:
  "005930":
    삼성전자:
      주식현재가 시세:
        응답:
          - 현재가: "70000"
            전일 대비율: "1.2"
            주가수익비율(PER): "12.3"
            업종명: "전기전자"
      국내주식 종목투자의견:
        응답:
          - 주식 영업일자: "20260706"
            증권사명: "메리츠"
            투자의견: "BUY"
            목표가: "500000"
            전일 종가: "309500"
            목표가 괴리율: "-38.10"
          - 주식 영업일자: "20260708"
            증권사명: "키움"
            투자의견: "BUY"
            목표가: "390000"
          - 주식 영업일자: "20260701"
            증권사명: "한투"
            투자의견: "BUY"
            목표가: "460000"
''',
            encoding="utf-8",
        )
        news_cache_path = tmp / "memory" / "collect-news-information" / "news-2026-06-18.yaml"
        news_cache_path.parent.mkdir(parents=True, exist_ok=True)
        news_cache_path.write_text(
            '''date: "2026-06-18"
source: kis_open_api
symbols:
  "005930":
    articles:
      - article_date: ""
        sentiment: neutral
        content: "2026-06-18 기준 수집된 뉴스가 없습니다."
''',
            encoding="utf-8",
        )
        market_index_snapshot_path = run_dir / "market-index-snapshot.json"
        write_json(
            market_index_snapshot_path,
            {
                "schema_version": "1",
                "run_id": "self-test",
                "started_at": "2026-06-18T09:00:00+09:00",
                "generated_at": "2026-06-18T09:00:01+09:00",
                "status": "success",
                "indexes": [
                    {
                        "symbol": "KOSPI",
                        "name": "KOSPI",
                        "source": "kis_domestic_index",
                        "status": "success",
                        "value": 3000.0,
                        "change_percent": 0.2,
                        "observed_at": "2026-06-18T09:00:00+09:00",
                        "market_status": "장중",
                    }
                ],
                "warnings": [],
                "errors": [],
            },
        )
        try:
            brief = build_decision_brief(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "decision-brief.json",
                    portfolio_json=str(tmp / "portfolio.json"),
                    price_chart=None,
                    account_before_order=None,
                    today_fills=None,
                    run_id=None,
                    started_at=None,
                    financial_cache_path=str(financial_cache_path),
                    news_cache_path=str(news_cache_path),
                    market_index_snapshot_json=str(market_index_snapshot_path),
                )
            )
            if brief["status"] != "success" or len(brief["symbols"]) != 2:
                failures.append(f"unexpected decision brief: {brief}")
            if any(item.get("code") == "order_gate_fields_missing" for item in brief.get("errors", [])):
                failures.append(f"decision brief should not include stale order gate error: {brief}")
            decision_brief_text = (run_dir / "decision-brief.json").read_text(encoding="utf-8")
            if "\n  " in decision_brief_text:
                failures.append("decision-brief.json should be stored as compact JSON")
            by_symbol = {item.get("symbol_id"): item for item in brief["symbols"]}
            if by_symbol["005930"].get("evidence_mode") != "full":
                failures.append(f"financial-covered symbol should be full: {by_symbol['005930']}")
            account_exposure_005930 = by_symbol["005930"].get("account_exposure", {})
            if "average_purchase_price" in account_exposure_005930 or "purchase_amount" in account_exposure_005930:
                failures.append(
                    f"decision-brief account_exposure must never carry broker cost-basis fields: {account_exposure_005930}"
                )
            if "position_cost_context" in by_symbol["005930"]:
                failures.append(
                    f"decision-brief symbols must not carry position_cost_context; it is judge-review-only: {by_symbol['005930']}"
                )
            if by_symbol["000660"].get("evidence_mode") != "price_only":
                failures.append(f"financial-missing symbol should remain price_only: {by_symbol['000660']}")
            if by_symbol["000660"].get("financial_summary", {}).get("cache_status") != "missing_symbol":
                failures.append(f"financial-missing symbol should be marked missing_symbol: {by_symbol['000660']}")
            price_only_financial = financial_summary_for(
                {
                    "symbols": {
                        "035420": {
                            "주식현재가 시세": {
                                "응답": [{"현재가": "200000", "전일 대비율": "1.0", "업종명": "서비스업"}]
                            }
                        }
                    }
                },
                "035420",
                "financial-cache.yaml",
            )
            if compact_summary_is_usable(price_only_financial):
                failures.append(f"price/change/sector-only financial summary should not be quality-value usable: {price_only_financial}")
            price_only_quality = mark_quality_value_excluded_without_financial(
                {"agent_role": "analyst-quality-value", "score": 5, "missing_data": []},
                {"product_type": "stock", "financial_summary": price_only_financial},
            )
            if price_only_quality.get("excluded_from_aggregation") is not True:
                failures.append(f"price-only quality-value score should be excluded from aggregation: {price_only_quality}")
            opinion_only_financial = financial_summary_for(
                {
                    "symbols": {
                        "035420": {
                            "국내주식 종목투자의견": {
                                "응답": [
                                    {
                                        "주식 영업일자": "20260618",
                                        "증권사명": "테스트증권",
                                        "투자의견": "BUY",
                                        "목표가": "",
                                    }
                                ]
                            }
                        }
                    }
                },
                "035420",
                "financial-cache.yaml",
            )
            if not compact_summary_is_usable(opinion_only_financial) or not any(
                str(item).startswith("최신 투자의견 BUY") for item in opinion_only_financial.get("items", [])
            ):
                failures.append(f"target-less broker opinion should remain usable partial quality evidence: {opinion_only_financial}")
            financial_items = by_symbol["005930"].get("financial_summary", {}).get("items", [])
            target_price_items = [item for item in financial_items if item.startswith("목표가 컨센서스")]
            expected_target_item = "목표가 컨센서스 3건(발표 20260701~20260708) 중앙값 460000(현재가대비 괴리율 -84.8%), 범위 390000~500000, 최신 390000 (키움, BUY, 20260708)"
            if target_price_items != [expected_target_item]:
                failures.append(f"invest opinion target consensus should be summarized: {financial_items}")
            if by_symbol["005930"].get("news_summary"):
                failures.append(f"no-news placeholder should not be included: {by_symbol['005930']}")
            freshness_filtered_news = news_summary_for(
                {
                    "date": "2026-06-18",
                    "symbols": {
                        "005930": {
                            "articles": [
                                {"article_date": "2020-01-01", "content": "old-1"},
                                {"article_date": "2020-01-02", "content": "old-2"},
                                {"article_date": "2020-01-03", "content": "old-3"},
                                {"article_date": "2026-06-18T09:30:00+09:00", "content": "fresh"},
                            ]
                        }
                    },
                },
                "005930",
                "news-cache.yaml",
                "2026-06-18",
            )
            if len(freshness_filtered_news) != 1 or freshness_filtered_news[0].get("content") != "fresh":
                failures.append(f"news summary should keep matching-date articles after filtering stale leading rows: {freshness_filtered_news}")
            if news_summary_for(
                {"symbols": {"005930": [{"article_date": "2026-06-18", "content": "undated cache"}]}},
                "005930",
                "news-cache.yaml",
                "2026-06-18",
            ):
                failures.append("news summary without a verifiable cache date should not be usable")
            if news_summary_for(
                {
                    "date": "2020-01-01",
                    "symbols": {
                        "005930": {
                            "articles": [
                                {"article_date": "2020-01-01T09:30:00+09:00", "content": "old cache article"}
                            ]
                        }
                    },
                },
                "005930",
                "old-news-cache.yaml",
                "2026-06-18",
            ):
                failures.append("news cache and article dates matching each other should still be rejected when they differ from the run date")
            if expected_news_calendar_date("", "2026-06-17T15:30:00+00:00") != "2026-06-18":
                failures.append("decision-brief started_at fallback should derive the expected news date in KST")
            if (brief.get("market_index_snapshot") or {}).get("indexes", [{}])[0].get("symbol") != "KOSPI":
                failures.append(f"decision brief should include compact market index snapshot: {brief.get('market_index_snapshot')}")
            strategy = brief.get("strategy_context") if isinstance(brief.get("strategy_context"), dict) else {}
            if strategy.get("regime") != "neutral" or strategy.get("missing_tracked_indexes") != ["KOSDAQ"]:
                failures.append(f"strategy context should use partial missing-index policy: {strategy}")
            if strategy.get("new_exposure_review_bias") != "neutral":
                failures.append(f"neutral strategy context should use neutral new-exposure bias: {strategy}")
            if not str((strategy.get("policy_source") or {}).get("sha256") or ""):
                failures.append(f"strategy context should include policy source hash: {strategy}")
            strategy_policy, strategy_policy_path = load_strategy_policy_config("")
            panic_context = build_strategy_context(
                strategy_policy,
                strategy_policy_path,
                {
                    "indexes": [
                        {"symbol": "KOSPI", "change_percent": -4.5, "status": "success"},
                        {"symbol": "KOSDAQ", "change_percent": -1.0, "status": "success"},
                    ]
                },
            )
            if panic_context.get("regime") != "panic_downside" or panic_context.get("new_exposure_review_bias") != "observe_first":
                failures.append(f"panic strategy context should take priority over weak downside: {panic_context}")
            weak_context = build_strategy_context(
                strategy_policy,
                strategy_policy_path,
                {"indexes": [{"symbol": "KOSPI", "change_percent": -2.5, "status": "success"}]},
            )
            if weak_context.get("regime") != "weak_downside" or weak_context.get("missing_tracked_indexes") != ["KOSDAQ"]:
                failures.append(f"weak strategy context should use available tracked indexes: {weak_context}")
            missing_context = build_strategy_context(strategy_policy, strategy_policy_path, {"indexes": []})
            if missing_context.get("regime") != "insufficient_market_data":
                failures.append(f"missing market data should produce insufficient_market_data: {missing_context}")
            symbol_strategy = by_symbol["005930"].get("symbol_strategy_context", {})
            if symbol_strategy.get("current_holding") is not True or symbol_strategy.get("loss_position") is not True:
                failures.append(f"holding loss symbol should include strategy context: {symbol_strategy}")
            if symbol_strategy.get("concentration_level") != "low":
                failures.append(f"symbol concentration should be calculated: {symbol_strategy}")
            panic_symbol_strategy = build_symbol_strategy_context(
                strategy_policy,
                panic_context,
                by_symbol["005930"].get("account_exposure", {}),
                brief.get("account_exposure_summary", {}),
            )
            if panic_symbol_strategy.get("downside_add_review_target") is not True:
                failures.append(f"panic current holding should be downside add review target: {panic_symbol_strategy}")
            invalid_policy = tmp / "invalid-strategy-policy.yaml"
            invalid_policy.write_text("tracked_indexes: []\n", encoding="utf-8")
            try:
                load_strategy_policy_config(invalid_policy)
                failures.append("invalid strategy policy config should fail validation")
            except ValueError:
                pass
            override_policy = tmp / "override-strategy-policy.yaml"
            override_policy.write_text(
                default_strategy_policy_config_path().read_text(encoding="utf-8").replace(
                    "risk_on_all_gte_pct: 1.5",
                    "risk_on_all_gte_pct: 0.1",
                ),
                encoding="utf-8",
            )
            override_config, override_path = load_strategy_policy_config(override_policy)
            if override_path != override_policy.resolve() or override_config.get("regime_thresholds", {}).get("risk_on_all_gte_pct") != 0.1:
                failures.append(f"explicit strategy policy override was not loaded: {override_path} {override_config}")
            old_strategy_env = os.environ.get(STRATEGY_POLICY_CONFIG_ENV)
            os.environ[STRATEGY_POLICY_CONFIG_ENV] = str(override_policy)
            try:
                _, env_override_path = load_strategy_policy_config("")
                if env_override_path != override_policy.resolve():
                    failures.append(f"env strategy policy override was not loaded: {env_override_path}")
            finally:
                if old_strategy_env is None:
                    os.environ.pop(STRATEGY_POLICY_CONFIG_ENV, None)
                else:
                    os.environ[STRATEGY_POLICY_CONFIG_ENV] = old_strategy_env
            chart_context = by_symbol["005930"].get("chart_context", {})
            if chart_context.get("daily_summary", {}).get("latest_close") != 70000:
                failures.append(f"chart summary should include latest close: {by_symbol['005930']}")
            if chart_context.get("daily_summary", {}).get("change_1_period_pct") != 1.4493:
                failures.append(f"chart summary should include one-period change: {by_symbol['005930']}")
            if chart_context.get("daily_summary", {}).get("ma5") != 68000.0:
                failures.append(f"chart summary should include ma5: {by_symbol['005930']}")
            if chart_context.get("daily_summary", {}).get("ma20") != 60500.0:
                failures.append(f"chart summary should include ma20: {by_symbol['005930']}")
            if chart_context.get("daily_summary", {}).get("distance_ma20_pct") != 15.7025:
                failures.append(f"chart summary should include ma20 distance: {by_symbol['005930']}")
            if chart_context.get("recent_daily", [{}])[0].get("close") != 70000:
                failures.append(f"recent chart rows should be preserved: {by_symbol['005930']}")
            if len(chart_context.get("recent_daily", [])) != CHART_RECENT_ROW_LIMITS["daily"]:
                failures.append(f"recent daily rows should be capped: {by_symbol['005930']}")
            if len(chart_context.get("recent_weekly", [])) != CHART_RECENT_ROW_LIMITS["weekly"]:
                failures.append(f"recent weekly rows should be capped: {by_symbol['005930']}")
            if len(chart_context.get("recent_monthly", [])) != CHART_RECENT_ROW_LIMITS["monthly"]:
                failures.append(f"recent monthly rows should be capped: {by_symbol['005930']}")
            if len(chart_context.get("recent_intraday", [])) != CHART_RECENT_ROW_LIMITS["intraday"]:
                failures.append(f"recent intraday rows should be capped: {by_symbol['005930']}")
            if chart_context.get("intraday_summary", {}).get("latest_price") != 70000:
                failures.append(f"intraday summary should include latest price: {by_symbol['005930']}")
            if chart_context.get("intraday_summary", {}).get("change_observed_pct") != -0.8499:
                failures.append(f"intraday summary should include observed change: {by_symbol['005930']}")
            if chart_context.get("intraday_summary", {}).get("observed_high") != 70600.0:
                failures.append(f"intraday summary should include observed high: {by_symbol['005930']}")
            if chart_context.get("intraday_summary", {}).get("observed_low") != 70000.0:
                failures.append(f"intraday summary should include observed low: {by_symbol['005930']}")
            if chart_context.get("intraday_summary", {}).get("total_observed_volume") != 721.0:
                failures.append(f"intraday summary should include total volume: {by_symbol['005930']}")
            if by_symbol["005930"].get("orderbook_summary", {}).get("best_bid") != 69900:
                failures.append(f"orderbook summary should be preserved: {by_symbol['005930']}")
            if by_symbol["005930"].get("trade_flow_summary", {}).get("tick_count") != 3:
                failures.append(f"trade flow summary should be preserved: {by_symbol['005930']}")
            if by_symbol["005930"].get("investor_flow_summary", {}).get("foreign_net_buy_quantity") != 1000:
                failures.append(f"investor flow summary should be preserved: {by_symbol['005930']}")
            trade_context = by_symbol["005930"].get("today_trade_timeline_context", {})
            if trade_context.get("last_direction") != "buy" or trade_context.get("has_intraday_reversal") is not True:
                failures.append(f"same-day trade timeline should be summarized: {by_symbol['005930']}")
            if trade_context.get("collection_status") != "complete" or trade_context.get("has_same_day_buy") is not True:
                failures.append(f"same-day trade collection should be complete with a confirmed buy: {trade_context}")
            no_trade_context = by_symbol["000660"].get("today_trade_timeline_context", {})
            if no_trade_context.get("collection_status") != "complete" or no_trade_context.get("has_same_day_trade") is not False:
                failures.append(f"complete same-day collection with zero fills should confirm absence: {no_trade_context}")
            if by_symbol["005930"].get("today_trade_price_context", {}).get("move_since_last_fill_pct") != -0.43:
                failures.append(f"same-day trade price context should include current-vs-fill move: {by_symbol['005930']}")
            actor_context = by_symbol["005930"].get("today_trade_price_context", {})
            if actor_context.get("bot_net_quantity") != 0 or actor_context.get("manual_net_quantity") != 1 or actor_context.get("manual_fill_count") != 1:
                failures.append(f"same-day trade context should split bot/manual net quantities: {actor_context}")
            missing_collection = today_trade_collection_context({}, artifact_exists=False, symbol_id="005930")
            missing_trade_context = today_trade_context([], 70000, missing_collection)
            if missing_trade_context.get("collection_status") != "unavailable" or missing_trade_context.get("has_same_day_trade") is not None:
                failures.append(f"missing today-fills artifact should keep history unknown: {missing_trade_context}")
            skipped_collection = today_trade_collection_context(
                {
                    "stage": "today-fills",
                    "status": "success",
                    "skipped": True,
                    "symbols": [{"symbol_id": "005930"}],
                    "fills": [],
                    "errors": [],
                },
                artifact_exists=True,
                symbol_id="005930",
            )
            skipped_trade_context = today_trade_context([], 70000, skipped_collection)
            if skipped_trade_context.get("collection_status") != "unavailable" or skipped_trade_context.get("has_same_day_trade") is not None:
                failures.append(f"skipped today-fills collection should keep history unavailable: {skipped_trade_context}")
            partial_collection = today_trade_collection_context(
                {
                    "stage": "today-fills",
                    "status": "success",
                    "skipped": False,
                    "symbols": [{"symbol_id": "005930"}],
                    "fills": [],
                    "errors": [{"code": "today_fills_query_variant_failed"}],
                },
                artifact_exists=True,
                symbol_id="005930",
            )
            partial_trade_context = today_trade_context([], 70000, partial_collection)
            if partial_trade_context.get("collection_status") != "partial" or partial_trade_context.get("has_same_day_trade") is not None:
                failures.append(f"partial today-fills collection with zero fills should keep history unknown: {partial_trade_context}")
            failed_collection = today_trade_collection_context(
                {
                    "stage": "today-fills",
                    "status": "partial",
                    "skipped": False,
                    "symbols": [{"symbol_id": "005930"}],
                    "fills": [],
                    "errors": [{"code": "today_fills_collection_failed"}],
                },
                artifact_exists=True,
                symbol_id="005930",
            )
            failed_trade_context = today_trade_context([], 70000, failed_collection)
            if failed_trade_context.get("collection_status") != "unavailable" or failed_trade_context.get("has_same_day_trade") is not None:
                failures.append(f"failed today-fills collection should keep history unavailable: {failed_trade_context}")
            partial_with_fill = today_trade_context(
                [{"filled_at": "2026-06-18T09:31:00+09:00", "direction": "buy", "quantity": 1, "price": 70100}],
                70000,
                partial_collection,
            )
            if partial_with_fill.get("has_same_day_trade") is not True or partial_with_fill.get("has_same_day_buy") is not True:
                failures.append(f"partial collection should preserve confirmed fill presence: {partial_with_fill}")
            first_specs = build_first_specs(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "analyst-review-specs.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    workspace_dir=tmp,
                    pipeline_dir=pipeline_dir(),
                    relative_paths=False,
                    symbol_ids="",
                )
            )
            if len(first_specs["specs"]) != 2 or not Path(first_specs["specs"][0]["artifact_paths"]["persona"]).is_absolute():
                failures.append(f"unexpected first specs: {first_specs}")
            subagent_dir = run_dir / "subagents"
            for role in ANALYST_REVIEW_SPEC_ROLES:
                parsed_symbols = [
                    {
                        "symbol_id": "005930",
                        "symbol_name": "삼성전자",
                        "score": 7,
                        "reason_code": "buy_candidate",
                        "one_line_reason": "test",
                    },
                    {
                        "symbol_id": "000660",
                        "symbol_name": "SK하이닉스",
                        "score": 5,
                        "reason_code": "hold_neutral",
                        "one_line_reason": "test",
                    },
                ]
                if role in COMBINED_ANALYST_REVIEW_ROLES:
                    view_roles = COMBINED_ANALYST_REVIEW_ROLES[role]
                    parsed_symbols = [
                        {
                            "symbol_id": item["symbol_id"],
                            "symbol_name": item["symbol_name"],
                            "views": {
                                view_role: {
                                    "score": item["score"],
                                    "reason_code": item["reason_code"],
                                    "one_line_reason": f"{view_role} {item['one_line_reason']}",
                                    "missing_data": [],
                                }
                                for view_role in view_roles
                            },
                        }
                        for item in parsed_symbols
                    ]
                write_json(
                    subagent_dir / f"first-{role}.wrapper.json",
                    {
                        "stage": "analyst-review",
                        "agent_role": role,
                        "task_name": f"first-{role}",
                        "status": "success",
                        "ended_at": "2026-06-18T00:00:00+00:00",
                        "parsed_json": {
                            "stage": "analyst-review",
                            "symbols": parsed_symbols,
                        },
                    },
                )
            analyst_review = build_analyst_review(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "analyst-review.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    symbol_ids="",
                )
            )
            if analyst_review["symbols"][0]["final_first_score"] != 7.0:
                failures.append(f"unexpected first review score: {analyst_review}")
            if analyst_review["symbols"][0].get("mean_score") != 7.0:
                failures.append(f"final_first_score must equal the simple mean of included scores: {analyst_review}")
            if any("confidence" in score or "confidence_adjusted_score" in score for score in analyst_review["symbols"][0].get("agent_scores", [])):
                failures.append(f"agent_scores must not carry confidence fields: {analyst_review}")
            news_scores = [
                item
                for item in analyst_review["symbols"][0].get("agent_scores", [])
                if item.get("agent_role") == "analyst-news-flow"
            ]
            if (
                not news_scores
                or news_scores[0].get("score") != 5
                or news_scores[0].get("reason_code") != "no_news_excluded"
                or news_scores[0].get("excluded_from_aggregation") is not True
            ):
                failures.append(f"news-flow without news should be marked excluded from aggregation: {analyst_review}")
            if analyst_review["symbols"][0].get("aggregation_score_count") != 3:
                failures.append(f"no-news news-flow should be excluded from aggregation count: {analyst_review}")
            review_by_symbol = {item.get("symbol_id"): item for item in analyst_review.get("symbols", [])}
            missing_financial_review = review_by_symbol.get("000660", {})
            missing_financial_scores = [
                item
                for item in missing_financial_review.get("agent_scores", [])
                if item.get("agent_role") == "analyst-quality-value"
            ]
            if (
                not missing_financial_scores
                or missing_financial_scores[0].get("score") != 5
                or missing_financial_scores[0].get("reason_code") != "no_financial_excluded"
                or missing_financial_scores[0].get("excluded_from_aggregation") is not True
                or "financial_summary" not in missing_financial_scores[0].get("missing_data", [])
            ):
                failures.append(f"quality-value without financial summary should be excluded from aggregation: {missing_financial_review}")
            if missing_financial_review.get("aggregation_score_count") != 2:
                failures.append(f"missing financial and news views should leave two included scores: {missing_financial_review}")
            supplied_financial_quality = [
                item
                for item in review_by_symbol.get("005930", {}).get("agent_scores", [])
                if item.get("agent_role") == "analyst-quality-value"
            ]
            if not supplied_financial_quality or supplied_financial_quality[0].get("excluded_from_aggregation") is True:
                failures.append(f"usable partial financial summary should keep quality-value in aggregation: {review_by_symbol.get('005930')}")
            market_news_sidecar = (
                run_dir
                / "reviews"
                / "analyst-review--analyst-momentum-news--first-analyst-momentum-news.md"
            )
            market_news_text = market_news_sidecar.read_text(encoding="utf-8")
            if "| analyst-news-flow | 005930 삼성전자 | 5 | 뉴스 정보가 없어 평균에서 제외 |" not in market_news_text:
                failures.append(f"news-flow sidecar should reflect average exclusion: {market_news_text}")
            quality_sidecar = (
                run_dir
                / "reviews"
                / "analyst-review--analyst-quality-risk--first-analyst-quality-risk.md"
            )
            quality_text = quality_sidecar.read_text(encoding="utf-8")
            if "| analyst-quality-value | 000660 SK하이닉스 | 5 | 재무 정보가 없어 평균에서 제외 |" not in quality_text:
                failures.append(f"quality-value sidecar should reflect financial exclusion: {quality_text}")
            nested_etf_summary = etf_summary_for(
                {
                    "symbols": {
                        "069500": {
                            "KODEX 200": {
                                "ETF/ETN 현재가": {"응답": [{"nav": "10000"}]},
                                "NAV 비교추이(종목)": {"NAV 비교 요약": [{"nav": "10000"}]},
                            }
                        }
                    }
                },
                "069500",
                "financial-cache.yaml",
            )
            if nested_etf_summary.get("cache_status") != "supplied" or not nested_etf_summary.get("items"):
                failures.append(f"nested ETF summary should be recognized as usable: {nested_etf_summary}")
            usable_etf_score = mark_quality_value_excluded_without_financial(
                {"agent_role": "analyst-quality-value", "score": 7},
                {"product_type": "etf", "etf_summary": nested_etf_summary},
            )
            if usable_etf_score.get("excluded_from_aggregation"):
                failures.append(f"usable ETF summary should keep quality-value in aggregation: {usable_etf_score}")
            etf_dir = tmp / "reports" / "runs" / "etf-quality-probe"
            etf_portfolio_path = tmp / "etf-portfolio.json"
            write_json(
                etf_portfolio_path,
                {"recommanded": [], "specified": ["069500"], "holding": [], "universe": ["069500"]},
            )
            write_json(
                etf_dir / "price-chart.json",
                {
                    "run_id": "etf-quality-probe",
                    "started_at": "2026-06-18T09:00:00+09:00",
                    "symbols": [
                        {
                            "symbol_id": "069500",
                            "symbol_name": "KODEX 200",
                            "product_type": "etf",
                            "eligible_for_review": True,
                            "price": {"current_or_last": 30000, "observed_at": "2026-06-18T09:00:00+09:00"},
                            "required_missing": [],
                            "errors": [],
                        }
                    ],
                },
            )
            write_json(
                etf_dir / "account-before-order.json",
                {
                    "run_id": "etf-quality-probe",
                    "started_at": "2026-06-18T09:00:00+09:00",
                    "symbols": [{"symbol_id": "069500", "symbol_name": "KODEX 200", "current_live_holding_quantity": 0}],
                },
            )
            write_json(
                etf_dir / "today-fills.json",
                {
                    "stage": "today-fills",
                    "status": "success",
                    "skipped": False,
                    "symbols": [{"symbol_id": "069500"}],
                    "fills": [],
                    "errors": [],
                },
            )
            etf_cache_path = tmp / "etf-financial-cache.json"
            write_json(
                etf_cache_path,
                {
                    "symbols": {
                        "069500": {
                            "KODEX 200": {
                                "ETF/ETN 현재가": {"응답": [{"nav": "30000", "dprt": "0.1"}]},
                                "NAV 비교추이(종목)": {"NAV 비교 요약": [{"nav": "30000"}]},
                            }
                        }
                    }
                },
            )
            etf_brief = build_decision_brief(
                argparse.Namespace(
                    output_dir=etf_dir,
                    output=etf_dir / "decision-brief.json",
                    portfolio_json=str(etf_portfolio_path),
                    price_chart=None,
                    account_before_order=None,
                    today_fills=None,
                    run_id=None,
                    started_at=None,
                    financial_cache_path=str(etf_cache_path),
                    news_cache_path="",
                    market_index_snapshot_json="",
                )
            )
            etf_brief_symbol = (etf_brief.get("symbols") or [{}])[0]
            if etf_brief_symbol.get("evidence_mode") != "full" or etf_brief_symbol.get("etf_summary", {}).get("cache_status") != "supplied":
                failures.append(f"usable ETF summary should produce full evidence mode: {etf_brief_symbol}")
            etf_subagent_dir = etf_dir / "subagents"
            for role in ANALYST_REVIEW_SPEC_ROLES:
                write_json(
                    etf_subagent_dir / f"first-{role}.wrapper.json",
                    {
                        "stage": "analyst-review",
                        "agent_role": role,
                        "task_name": f"first-{role}",
                        "status": "success",
                        "ended_at": "2026-06-18T00:00:00+00:00",
                        "parsed_json": {
                            "stage": "analyst-review",
                            "symbols": [
                                {
                                    "symbol_id": "069500",
                                    "symbol_name": "KODEX 200",
                                    "views": {
                                        view_role: {
                                            "score": 7,
                                            "reason_code": "buy_candidate",
                                            "one_line_reason": f"{view_role} ETF self-test",
                                            "missing_data": [],
                                        }
                                        for view_role in COMBINED_ANALYST_REVIEW_ROLES[role]
                                    },
                                }
                            ],
                        },
                    },
                )
            etf_review = build_analyst_review(
                argparse.Namespace(
                    output_dir=etf_dir,
                    output=etf_dir / "analyst-review.json",
                    decision_brief=str(etf_dir / "decision-brief.json"),
                    symbol_ids="",
                )
            )
            etf_review_symbol = (etf_review.get("symbols") or [{}])[0]
            etf_quality_scores = [
                item
                for item in etf_review_symbol.get("agent_scores", [])
                if item.get("agent_role") == "analyst-quality-value"
            ]
            if (
                etf_review_symbol.get("aggregation_score_count") != 3
                or not etf_quality_scores
                or etf_quality_scores[0].get("excluded_from_aggregation") is True
            ):
                failures.append(f"usable ETF quality-value should remain in aggregation: {etf_review_symbol}")
            missing_etf_score = mark_quality_value_excluded_without_financial(
                {"agent_role": "analyst-quality-value", "score": 7, "missing_data": []},
                {"product_type": "etn", "etf_summary": {"cache_status": "missing", "items": []}},
            )
            if (
                missing_etf_score.get("reason_code") != "no_financial_excluded"
                or missing_etf_score.get("excluded_from_aggregation") is not True
                or "etf_summary" not in missing_etf_score.get("missing_data", [])
            ):
                failures.append(f"ETF without usable summary should be excluded from aggregation: {missing_etf_score}")
            quality_wrapper_path = subagent_dir / "first-analyst-quality-risk.wrapper.json"
            valid_quality_wrapper = load_json(quality_wrapper_path)
            invalid_quality_wrapper = json.loads(json.dumps(valid_quality_wrapper))
            invalid_quality_wrapper["parsed_json"]["symbols"][0]["views"]["analyst-quality-value"]["score"] = "unknown"
            write_json(quality_wrapper_path, invalid_quality_wrapper)
            invalid_score_review = build_analyst_review(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "analyst-review-invalid-score.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    symbol_ids="",
                )
            )
            invalid_score_symbol = next(
                (item for item in invalid_score_review.get("symbols", []) if item.get("symbol_id") == "005930"),
                {},
            )
            if not any(error.get("code") == "invalid_agent_score" for error in invalid_score_review.get("errors", [])):
                failures.append(f"invalid analyst score did not produce a required artifact error: {invalid_score_review}")
            if any(
                score.get("agent_role") == "analyst-quality-value"
                for score in invalid_score_symbol.get("agent_scores", [])
                if isinstance(score, dict)
            ):
                failures.append(f"invalid analyst score was silently converted and aggregated: {invalid_score_symbol}")
            write_json(quality_wrapper_path, valid_quality_wrapper)
            missing_wrapper_path = subagent_dir / "first-analyst-quality-risk.wrapper.json"
            missing_wrapper = load_json(missing_wrapper_path)
            missing_wrapper["parsed_json"]["symbols"][1]["views"].pop("analyst-risk-allocation")
            write_json(missing_wrapper_path, missing_wrapper)
            missing_analyst_review = build_analyst_review(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "analyst-review-missing.json",
                    decision_brief=str(run_dir / "decision-brief.json"),
                    symbol_ids="",
                )
            )
            if missing_analyst_review["status"] != "partial" or not any(
                error.get("code") == "missing_agent_score" for error in missing_analyst_review["errors"]
            ):
                failures.append(f"missing persona score did not produce partial review: {missing_analyst_review}")
            def second_spec_args(analyst_review_path: str, output_name: str) -> argparse.Namespace:
                return argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / output_name,
                    decision_brief=str(run_dir / "decision-brief.json"),
                    analyst_review=analyst_review_path,
                    portfolio_json=str(tmp / "portfolio.json"),
                    run_id=None,
                    started_at=None,
                    workspace_dir=tmp,
                    pipeline_dir=pipeline_dir(),
                    relative_paths=False,
                    buy_min_score=6.0,
                    sell_max_score=4.0,
                )

            second_spec = build_second_spec(second_spec_args(str(run_dir / "analyst-review.json"), "judge-review-spec.json"))
            if second_spec["symbol_ids"] != ["005930"]:
                failures.append(f"unexpected second spec symbols: {second_spec}")
            if second_spec.get("candidate_directions") != {"005930": "buy"}:
                failures.append(f"buy candidate direction missing: {second_spec}")
            snapshot = second_spec.get("portfolio_snapshot") or []
            if len(snapshot) != 1 or snapshot[0].get("symbol_id") != "005930" or snapshot[0].get("final_first_score") != 7.0 or snapshot[0].get("candidate_direction") != "buy":
                failures.append(f"portfolio snapshot should describe every holding: {second_spec}")
            if str(second_spec.get("artifact_paths", {}).get("review_format", "")).rsplit("/", 1)[-1] != "judge-review-format.md":
                failures.append(f"judge spec should reference judge-review-format.md: {second_spec}")
            for debate_key, debate_file in (("debate_bull_persona", "debate-bull.md"), ("debate_bear_persona", "debate-bear.md")):
                debate_path = str(second_spec.get("artifact_paths", {}).get(debate_key, ""))
                if debate_path.rsplit("/", 1)[-1] != debate_file or not Path(debate_path).is_file():
                    failures.append(f"judge spec missing readable {debate_key}: {second_spec}")
            debate_format_path = str(second_spec.get("artifact_paths", {}).get("debate_format", ""))
            if debate_format_path.rsplit("/", 1)[-1] != "debate-format.md" or not Path(debate_format_path).is_file():
                failures.append(f"judge spec missing readable debate_format: {second_spec}")
            if str(second_spec.get("artifact_paths", {}).get("debate_artifact", "")) != str(run_dir / "judge-debate.json"):
                failures.append(f"judge spec missing deterministic debate_artifact path: {second_spec}")
            threshold_analyst_review = load_json(run_dir / "analyst-review.json")
            for item in threshold_analyst_review.get("symbols", []):
                if item.get("symbol_id") == "000660":
                    item["final_first_score"] = 6.0
            write_json(run_dir / "analyst-review-threshold-6.0.json", threshold_analyst_review)
            threshold_60_spec = build_second_spec(second_spec_args(str(run_dir / "analyst-review-threshold-6.0.json"), "judge-review-spec-threshold-6.0.json"))
            if threshold_60_spec["symbol_ids"] != ["005930", "000660"] or threshold_60_spec.get("candidate_directions", {}).get("000660") != "buy":
                failures.append(f"exactly 6.0 must become a buy candidate: {threshold_60_spec}")
            for item in threshold_analyst_review.get("symbols", []):
                if item.get("symbol_id") == "000660":
                    item["final_first_score"] = 6.1
            write_json(run_dir / "analyst-review-threshold-6.1.json", threshold_analyst_review)
            threshold_61_spec = build_second_spec(second_spec_args(str(run_dir / "analyst-review-threshold-6.1.json"), "judge-review-spec-threshold-6.1.json"))
            if threshold_61_spec["symbol_ids"] != ["005930", "000660"] or threshold_61_spec.get("candidate_directions", {}).get("000660") != "buy":
                failures.append(f"score at or above 6.0 should become a buy candidate: {threshold_61_spec}")
            sell_band_review = load_json(run_dir / "analyst-review.json")
            for item in sell_band_review.get("symbols", []):
                if item.get("symbol_id") == "005930":
                    item["final_first_score"] = 3.9
                if item.get("symbol_id") == "000660":
                    item["final_first_score"] = 3.0
            write_json(run_dir / "analyst-review-sell-band.json", sell_band_review)
            sell_band_spec = build_second_spec(second_spec_args(str(run_dir / "analyst-review-sell-band.json"), "judge-review-spec-sell-band.json"))
            if sell_band_spec["symbol_ids"] != ["005930"] or sell_band_spec.get("candidate_directions") != {"005930": "sell"}:
                failures.append(f"only held symbols at or below 4.0 may become sell candidates: {sell_band_spec}")
            mid_band_review = load_json(run_dir / "analyst-review.json")
            for item in mid_band_review.get("symbols", []):
                item["final_first_score"] = 5.0
            write_json(run_dir / "analyst-review-mid-band.json", mid_band_review)
            mid_band_spec = build_second_spec(second_spec_args(str(run_dir / "analyst-review-mid-band.json"), "judge-review-spec-mid-band.json"))
            if mid_band_spec["symbol_ids"] != [] or mid_band_spec.get("candidate_directions") != {}:
                failures.append(f"mid-band symbols must not reach the judge: {mid_band_spec}")
            for item in mid_band_review.get("symbols", []):
                if item.get("symbol_id") == "005930":
                    item["final_first_score"] = 4.0
            write_json(run_dir / "analyst-review-sell-edge.json", mid_band_review)
            sell_edge_spec = build_second_spec(second_spec_args(str(run_dir / "analyst-review-sell-edge.json"), "judge-review-spec-sell-edge.json"))
            if sell_edge_spec["symbol_ids"] != ["005930"] or sell_edge_spec.get("candidate_directions") != {"005930": "sell"}:
                failures.append(f"exactly 4.0 must become a sell candidate: {sell_edge_spec}")
            write_json(
                run_dir / "judge-review.json",
                {
                    "run_id": "daily-trading-test",
                    "started_at": "2026-06-18 09:00:00 KST",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "final_holding_quantity": 2,
                            "relative_attractiveness_rank": 1,
                            "reason_code": "add",
                            "one_line_reason": "test",
                        }
                    ],
                },
            )
            execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution.json",
                    judge_review=str(run_dir / "judge-review.json"),
                    account_before_order=str(run_dir / "account-before-order.json"),
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if execution["orders"][0]["result"] != "blocked":
                failures.append(f"unexpected execution plan: {execution}")
            if execution["orders"][0].get("order_path") != "reservation" or execution["orders"][0].get("order_api") != "order_resv":
                failures.append(f"execution plan did not emit reservation order path/API: {execution['orders'][0]}")
            write_json(
                run_dir / "judge-review-invalid-final.json",
                {
                    "run_id": "daily-trading-test",
                    "started_at": "2026-06-18 09:00:00 KST",
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "relative_attractiveness_rank": 1,
                            "reason_code": "hold",
                            "one_line_reason": "missing final quantity",
                        }
                    ],
                },
            )
            invalid_execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution-invalid-final.json",
                    judge_review=str(run_dir / "judge-review-invalid-final.json"),
                    account_before_order=str(run_dir / "account-before-order.json"),
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if invalid_execution.get("orders"):
                failures.append(f"missing final_holding_quantity was converted into an order: {invalid_execution}")
            if not any(item.get("code") == "invalid_final_holding_quantity" for item in invalid_execution.get("errors", [])):
                failures.append(f"missing final_holding_quantity did not produce execution error: {invalid_execution}")
            account_missing_gates = load_json(run_dir / "account-before-order.json")
            account_missing_gates.pop("active_order_lookup_performed", None)
            account_missing_gates.pop("order_available_lookup_performed", None)
            write_json(run_dir / "account-before-order-missing-gates.json", account_missing_gates)
            missing_gate_execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution-missing-gates.json",
                    judge_review=str(run_dir / "judge-review.json"),
                    account_before_order=str(run_dir / "account-before-order-missing-gates.json"),
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if missing_gate_execution["orders"][0]["result"] != "blocked":
                failures.append(f"missing account gate fields did not block execution: {missing_gate_execution}")
            if missing_gate_execution["orders"][0]["reason"] != "active_order_or_order_available_gate_missing":
                failures.append(f"missing account gate used unexpected reason: {missing_gate_execution['orders'][0]}")
            if "explicit limit price" in (missing_gate_execution.get("errors") or [{}])[0].get("message", ""):
                failures.append(f"missing account gate error still requires explicit limit price: {missing_gate_execution['errors']}")
            account_ready = load_json(run_dir / "account-before-order.json")
            account_ready["active_order_lookup_performed"] = True
            account_ready["order_available_lookup_performed"] = True
            write_json(run_dir / "account-before-order-ready.json", account_ready)
            ready_execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution-ready.json",
                    judge_review=str(run_dir / "judge-review.json"),
                    account_before_order=str(run_dir / "account-before-order-ready.json"),
                    decision_brief=str(run_dir / "decision-brief.json"),
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if ready_execution["orders"][0]["result"] != "skipped" or ready_execution["orders"][0]["reason"] != "ready_for_main_agent_submission":
                failures.append(f"gate-ready execution plan was not marked ready: {ready_execution}")
            account_default_brief = load_json(run_dir / "account-before-order-ready.json")
            account_default_brief["symbols"][0]["current_price"] = None
            write_json(run_dir / "account-before-order-default-brief.json", account_default_brief)
            default_brief_execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution-default-brief.json",
                    judge_review=str(run_dir / "judge-review.json"),
                    account_before_order=str(run_dir / "account-before-order-default-brief.json"),
                    decision_brief="",
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                )
            )
            if default_brief_execution["orders"][0].get("order_price") != 70000:
                failures.append(
                    f"default decision-brief price fallback did not populate order_price: {default_brief_execution}"
                )
            write_json(subagent_dir / "token.wrapper.json", {"token_usage": {"input_tokens": 2, "output_tokens": 3}})
            events = tmp / "events.jsonl"
            events.write_text(
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
                + "\n"
                + json.dumps({"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1, "output_tokens": 1}}})
                + "\n",
                encoding="utf-8",
            )
            token_summary = build_token_summary(
                argparse.Namespace(run_dir=run_dir, main_events=str(events), output=run_dir / "token-summary.json")
            )
            if token_summary["total"]["token_usage"]["total_tokens"] != 22:
                failures.append(f"unexpected token summary: {token_summary}")
        except Exception as exc:  # noqa: BLE001 - self-test reports all issues
            failures.append(str(exc))

    status = "failed" if failures else "passed"
    print(json.dumps({"status": status, "failures": failures}, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


class BuildRunArtifactsSelfTest(unittest.TestCase):
    def test_self_test_suite(self) -> None:
        self.assertEqual(run_self_test(), 0)

    def test_execution_plan_reconciles_orphan_active_order_and_blocks_unverified_holding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            run_dir = Path(tmp_name) / "run"
            run_dir.mkdir()
            write_json(
                run_dir / "account-before-order.json",
                {
                    "run_id": "lifecycle-plan",
                    "started_at": "2026-07-15T15:00:00+09:00",
                    "active_order_lookup_performed": True,
                    "order_available_lookup_performed": False,
                    "active_orders": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "order_id": "active-1",
                            "direction": "buy",
                            "remaining_quantity": 1,
                            "order_price": 70000,
                            "order_path": "immediate",
                            "order_api": "order_cash",
                            "active_status": "active",
                        },
                        {
                            "symbol_id": "000660",
                            "symbol_name": "SK하이닉스",
                            "order_id": "active-2",
                            "direction": "sell",
                            "remaining_quantity": 1,
                            "order_price": 200000,
                            "order_path": "immediate",
                            "order_api": "order_cash",
                            "active_status": "active",
                        },
                    ],
                    "symbols": [
                        {
                            "symbol_id": "005930",
                            "symbol_name": "삼성전자",
                            "current_live_holding_quantity": 10,
                            "current_price": 70000,
                            "holding_state_status": "consistent",
                        },
                        {
                            "symbol_id": "042660",
                            "symbol_name": "한화오션",
                            "current_live_holding_quantity": 0,
                            "current_price": 100000,
                            "holding_state_status": "inconsistent",
                            "holding_state_reasons": [
                                "confirmed_local_buy_fill_exceeds_account_today_buy_quantity"
                            ],
                        },
                        {
                            "symbol_id": "000660",
                            "symbol_name": "SK하이닉스",
                            "current_live_holding_quantity": 3,
                            "current_price": 200000,
                            "holding_state_status": "unconfirmed",
                            "holding_state_reasons": ["previous_submitted_order_status_unconfirmed"],
                        },
                    ],
                },
            )
            write_json(
                run_dir / "judge-review.json",
                {
                    "run_id": "lifecycle-plan",
                    "started_at": "2026-07-15T15:00:00+09:00",
                    "symbols": [
                        {
                            "symbol_id": "042660",
                            "symbol_name": "한화오션",
                            "final_holding_quantity": 1,
                        }
                    ],
                },
            )
            write_json(
                run_dir / "decision-brief.json",
                {
                    "symbols": [
                        {"symbol_id": "005930", "price": {"current_or_last": 70000}},
                        {"symbol_id": "042660", "price": {"current_or_last": 100000}},
                        {"symbol_id": "000660", "price": {"current_or_last": 200000}},
                    ]
                },
            )
            execution = build_execution_plan(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "execution.json",
                    judge_review="",
                    account_before_order="",
                    decision_brief="",
                    analyst_review="",
                    run_id=None,
                    started_at=None,
                    request_type="real-submit",
                    order_path="immediate",
                )
            )

            by_symbol = {item["symbol_id"]: item for item in execution["orders"]}
            self.assertTrue(execution["requires_main_agent_order_execution"])
            self.assertEqual(by_symbol["042660"]["reason"], "holding_state_not_verified")
            self.assertEqual(by_symbol["042660"]["direction"], "none")
            self.assertTrue(by_symbol["005930"]["reconciliation_only"])
            self.assertTrue(by_symbol["005930"]["active_cancel_only"])
            self.assertEqual(by_symbol["005930"]["direction"], "sell")
            self.assertEqual(by_symbol["005930"]["reason"], "stale_active_order_requires_cancellation")
            self.assertTrue(by_symbol["000660"]["reconciliation_only"])
            self.assertEqual(by_symbol["000660"]["direction"], "buy")
            self.assertEqual(
                by_symbol["000660"]["reason"],
                "unverified_holding_requires_active_order_cancellation",
            )

    def test_financial_summary_omits_stale_quote_and_uses_fresh_price_for_target_gap(self) -> None:
        cache = {
            "symbols": {
                "000660": {
                    "주식현재가 시세": {
                        "응답": [
                            {
                                "현재가": "2082000",
                                "전일 대비율": "0.00",
                                "주가수익비율(PER)": "10.5",
                                "주가순자산비율(PBR)": "1.2",
                                "업종명": "반도체",
                            }
                        ]
                    },
                    "국내주식 종목투자의견": {
                        "응답": [
                            {
                                "주식 영업일자": "20260716",
                                "증권사명": "테스트증권",
                                "투자의견": "BUY",
                                "목표가": "2000000",
                            }
                        ]
                    },
                }
            }
        }

        fresh_summary = financial_summary_for(cache, "000660", "financial-cache.yaml", 1838000)
        joined = " ".join(fresh_summary["items"])
        self.assertNotIn("2082000", joined)  # stale cached quote price must not leak into evidence
        self.assertNotIn("등락률", joined)  # stale daily-change label must be removed
        self.assertIn("PER 10.5", joined)
        self.assertIn("PBR 1.2", joined)
        target_item = next(item for item in fresh_summary["items"] if item.startswith("목표가"))
        self.assertIn("현재가대비 괴리율 -8.1%", target_item)
        self.assertNotIn("4.1%", target_item)  # would be the stale-cache-price gap if used incorrectly

        no_fresh_price_summary = financial_summary_for(cache, "000660", "financial-cache.yaml", None)
        no_fresh_target_item = next(item for item in no_fresh_price_summary["items"] if item.startswith("목표가"))
        self.assertNotIn("괴리율", no_fresh_target_item)

    def test_decision_brief_omits_target_gap_when_price_lacks_observed_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            run_dir = tmp / "run"
            write_json(tmp / "portfolio.json", {"specified": ["000660"], "holding": [], "universe": ["000660"]})
            write_json(
                run_dir / "price-chart.json",
                {
                    "run_id": "no-observed-at-test",
                    "started_at": "2026-07-17 09:00:00 KST",
                    "symbols": [
                        {
                            "symbol_id": "000660",
                            "symbol_name": "SK하이닉스",
                            "product_type": "stock",
                            "eligible_for_review": True,
                            # current_or_last is present but observed_at is missing, so this run's
                            # price is not validated as a fresh/usable price.
                            "price": {"current_or_last": 1838000, "observed_at": "", "snapshot_mode": "live"},
                            "required_missing": [],
                            "errors": [],
                        }
                    ],
                },
            )
            write_json(
                run_dir / "account-before-order.json",
                {
                    "run_id": "no-observed-at-test",
                    "started_at": "2026-07-17 09:00:00 KST",
                    "active_order_lookup_performed": False,
                    "order_available_lookup_performed": False,
                    "account_summary": {"cash_amount": 1000000, "total_evaluation_amount": 1000000},
                    "active_orders": [],
                    "symbols": [
                        {
                            "symbol_id": "000660",
                            "symbol_name": "SK하이닉스",
                            "current_live_holding_quantity": 0,
                            "current_price": 1838000,
                            "valuation_amount": 0,
                        }
                    ],
                },
            )
            financial_cache_path = tmp / "memory" / "collect-financial-information" / "financial-2026-07-17.yaml"
            financial_cache_path.parent.mkdir(parents=True, exist_ok=True)
            financial_cache_path.write_text(
                '''date: "2026-07-17"
source: kis_open_api
symbols:
  "000660":
    SK하이닉스:
      주식현재가 시세:
        응답:
          - 현재가: "2082000"
            전일 대비율: "0.00"
      국내주식 종목투자의견:
        응답:
          - 주식 영업일자: "20260717"
            증권사명: "테스트증권"
            투자의견: "BUY"
            목표가: "2000000"
''',
                encoding="utf-8",
            )
            brief = build_decision_brief(
                argparse.Namespace(
                    output_dir=run_dir,
                    output=run_dir / "decision-brief.json",
                    portfolio_json=str(tmp / "portfolio.json"),
                    price_chart=None,
                    account_before_order=None,
                    today_fills=None,
                    run_id=None,
                    started_at=None,
                    financial_cache_path=str(financial_cache_path),
                    news_cache_path=None,
                    market_index_snapshot_json=None,
                )
            )
            by_symbol = {item.get("symbol_id"): item for item in brief["symbols"]}
            symbol = by_symbol["000660"]
            self.assertFalse(symbol.get("eligible_for_review"))
            target_item = next(item for item in symbol["financial_summary"]["items"] if item.startswith("목표가"))
            self.assertNotIn("괴리율", target_item)
