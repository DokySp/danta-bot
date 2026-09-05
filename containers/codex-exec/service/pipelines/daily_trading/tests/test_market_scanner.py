from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import zipfile

from ..scripts import market_scanner
from ..scripts.market_scanner import (
    MASTER_LAYOUTS,
    MasterSymbol,
    bar_on,
    bars_before,
    collection_covers,
    fetch_symbol_bars,
    historical_common_stock,
    live_tradeable_common_stock,
    open_history_database,
    parse_master_archive,
    parse_master_text,
    store_symbol_history,
)


def master_line(exchange: str, **overrides: str) -> bytes:
    layout = MASTER_LAYOUTS[exchange]
    widths = layout["widths"]
    indexes = layout["indexes"]
    defaults = {
        "group_code": "ST",
        "market_cap_size": "1",
        "low_liquidity": "N",
        "etp_code": "",
        "spac": "N",
        "trading_halt": "N",
        "liquidation": "N",
        "managed": "N",
        "market_warning": "00",
        "unfaithful_disclosure": "N",
        "preferred": "0",
    }
    defaults.update(overrides)
    fields = [" " * width for width in widths]
    for name, value in defaults.items():
        width = widths[indexes[name]]
        fields[indexes[name]] = str(value).ljust(width)[:width]
    head = "005930".ljust(9) + "KR7005930003" + "삼성전자"
    return (head + "".join(fields) + "\n").encode("cp949")


class MarketScannerFoundationTest(unittest.TestCase):
    def test_master_parser_keeps_only_tradeable_common_stock(self) -> None:
        parsed = parse_master_text(master_line("KOSPI"), "KOSPI")

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].symbol_id, "005930")
        self.assertEqual(parsed[0].symbol_name, "삼성전자")
        self.assertTrue(historical_common_stock(parsed[0]))
        self.assertTrue(live_tradeable_common_stock(parsed[0]))

        preferred = parse_master_text(master_line("KOSPI", preferred="1"), "KOSPI")[0]
        halted = parse_master_text(master_line("KOSPI", trading_halt="Y"), "KOSPI")[0]
        self.assertFalse(historical_common_stock(preferred))
        self.assertTrue(historical_common_stock(halted))
        self.assertFalse(live_tradeable_common_stock(halted))

        alphanumeric = MasterSymbol("0120G0", "테스트보통주", "KOSPI", parsed[0].fields)
        lowercase = MasterSymbol("0120g0", "잘못된코드", "KOSPI", parsed[0].fields)
        self.assertTrue(historical_common_stock(alphanumeric))
        self.assertFalse(historical_common_stock(lowercase))

    def test_master_archive_rejects_zip_without_master_member(self) -> None:
        payload = BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("error.html", "not a master")

        with self.assertRaisesRegex(ValueError, "no .mst member"):
            parse_master_archive(payload.getvalue(), "KOSPI")

    def test_history_query_excludes_decision_date_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            connection = open_history_database(Path(tmp_name) / "history.sqlite3")
            symbol = MasterSymbol(
                "005930",
                "삼성전자",
                "KOSPI",
                {
                    "group_code": "ST",
                    "etp_code": "",
                    "preferred": "0",
                    "trading_halt": "Y",
                    "market_warning": "01",
                },
            )
            stored_count = store_symbol_history(
                connection,
                symbol,
                [
                    {"date": "20260803", "open": 90, "high": 110, "low": 80, "close": 100, "volume": 10, "trading_value": 1_000},
                    {"date": "20260804", "open": 101, "high": 120, "low": 99, "close": 115, "volume": 20, "trading_value": 2_000},
                ],
                date(2026, 8, 3),
                date(2026, 8, 4),
                observed_at="2026-09-04T00:00:00+00:00",
            )
            connection.commit()

            history = bars_before(connection, "005930", date(2026, 8, 4))
            stored_fields = json.loads(
                connection.execute(
                    "SELECT master_fields_json FROM symbols WHERE symbol_id = ?", ("005930",)
                ).fetchone()[0]
            )

            self.assertEqual([row["trade_date"] for row in history], ["2026-08-03"])
            self.assertEqual(bar_on(connection, "005930", date(2026, 8, 4))["open_price"], 101)
            self.assertEqual(stored_fields, {"etp_code": "", "group_code": "ST", "preferred": "0"})
            self.assertEqual(stored_count, 2)
            connection.close()

    def test_history_fetch_pages_backward_and_marks_full_request_covered(self) -> None:
        end = date(2026, 8, 31)

        def raw_bar(day: date) -> dict[str, str]:
            return {
                "stck_bsop_date": day.strftime("%Y%m%d"),
                "stck_oprc": "100",
                "stck_hgpr": "110",
                "stck_lwpr": "90",
                "stck_clpr": "105",
                "acml_vol": "10",
                "acml_tr_pbmn": "1050",
            }

        first_page = [raw_bar(end - timedelta(days=index)) for index in range(100)]
        second_page = [raw_bar(end - timedelta(days=100 + index)) for index in range(5)]
        calls: list[dict[str, str]] = []

        def fake_call(_name, params, *_args, **_kwargs):
            calls.append(params)
            rows = first_page if len(calls) == 1 else second_page
            return {"output2": rows}, {}

        symbol = MasterSymbol("005930", "삼성전자", "KOSPI", {})
        start = end - timedelta(days=200)
        with patch.object(market_scanner.collect_main_evidence, "call_endpoint", side_effect=fake_call):
            bars = fetch_symbol_bars(
                symbol,
                start,
                end,
                app_key="key",
                app_secret="secret",
                token="token",
                retries=0,
            )

        self.assertEqual(len(bars), 105)
        self.assertEqual(calls[1]["FID_INPUT_DATE_2"], (end - timedelta(days=100)).strftime("%Y%m%d"))
        with tempfile.TemporaryDirectory() as tmp_name:
            connection = open_history_database(Path(tmp_name) / "history.sqlite3")
            store_symbol_history(
                connection,
                symbol,
                bars,
                start,
                end,
                observed_at="2026-09-04T00:00:00+00:00",
            )
            connection.commit()
            self.assertTrue(collection_covers(connection, symbol.symbol_id, start, end))
            connection.close()

    def test_history_fetch_rejects_nonempty_page_with_invalid_dates(self) -> None:
        symbol = MasterSymbol("005930", "삼성전자", "KOSPI", {})
        with patch.object(
            market_scanner.collect_main_evidence,
            "call_endpoint",
            return_value=({"output2": [{"stck_bsop_date": "invalid", "stck_clpr": "100"}]}, {}),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid dates"):
                fetch_symbol_bars(
                    symbol,
                    date(2026, 1, 1),
                    date(2026, 8, 31),
                    app_key="key",
                    app_secret="secret",
                    token="token",
                    retries=0,
                )

    def test_history_fetch_rejects_invalid_close_prices(self) -> None:
        symbol = MasterSymbol("005930", "삼성전자", "KOSPI", {})
        with patch.object(
            market_scanner.collect_main_evidence,
            "call_endpoint",
            return_value=({"output2": [{"stck_bsop_date": "20260831", "stck_clpr": "0"}]}, {}),
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid close prices"):
                fetch_symbol_bars(
                    symbol,
                    date(2026, 1, 1),
                    date(2026, 8, 31),
                    app_key="key",
                    app_secret="secret",
                    token="token",
                    retries=0,
                )


if __name__ == "__main__":
    unittest.main()
