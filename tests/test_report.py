import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import report


def write_run(
    root: Path,
    name: str,
    contract_events: dict[str, list[dict]],
    *,
    run_summary: dict | None = None,
    account_lines: list[dict] | None = None,
) -> Path:
    run_dir = root / name
    run_dir.mkdir(parents=True)
    (run_dir / "effective_strategy.json").write_text("{}", encoding="utf-8")
    for key, lines in contract_events.items():
        contract_dir = run_dir / key
        contract_dir.mkdir()
        (contract_dir / "effective_strategy.json").write_text("{}", encoding="utf-8")
        (contract_dir / "events.jsonl").write_text(
            "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines),
            encoding="utf-8",
        )
    summary = run_summary or {"terminal_states": {}, "contracts": []}
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    if account_lines is not None:
        (run_dir / "account.jsonl").write_text(
            "".join(json.dumps(line, sort_keys=True) + "\n" for line in account_lines),
            encoding="utf-8",
        )
    return run_dir


def line(at: float, event_type: str, data: dict) -> dict:
    return {
        "at": at,
        "event": {"type": event_type, "data": data},
        "state_before": "QUOTING",
        "state_after": "QUOTING",
        "actions": [],
    }


def contract_line(at: float, pricetick: float = 5.0, size: float | None = 5.0) -> dict:
    data = {"symbol": "al2609", "exchange": "SHFE", "pricetick": pricetick}
    if size is not None:
        data["size"] = size
    return line(at, "ContractEvent", data)


def order_line(
    at: float,
    order_id: str,
    client_id: str,
    side: str,
    exchange_time: str | None,
    volume: int = 1,
    price: float = 23990.0,
    status: str = "NOTTRADED",
) -> dict:
    data = {
        "order_id": order_id,
        "symbol": "al2609",
        "exchange": "SHFE",
        "side": side,
        "status": status,
        "volume": volume,
        "traded": 1 if status == "ALLTRADED" else 0,
        "price": price,
        "client_id": client_id,
    }
    if exchange_time is not None:
        data["exchange_time"] = exchange_time
    return line(at, "OrderEvent", data)


def trade_line(
    at: float,
    order_id: str,
    client_id: str,
    side: str,
    volume: int,
    price: float,
    trade_id: str,
    exchange_time: str,
) -> dict:
    return line(
        at,
        "TradeEvent",
        {
            "order_id": order_id,
            "symbol": "al2609",
            "exchange": "SHFE",
            "side": side,
            "volume": volume,
            "price": price,
            "trade_id": trade_id,
            "client_id": client_id,
            "exchange_time": exchange_time,
        },
    )


def quoted_round(
    *,
    start_at: float,
    sequence: int,
    insert_time: str,
    open_time: str,
    open_side: str = "BUY",
    open_price: float = 23990.0,
    close_price: float = 23960.0,
    close_time: str = "2026-08-17T23:51:10+08:00",
    volume: int = 1,
) -> list[dict]:
    """One round: a quote pair where one side fills, then a FAK flatten fills."""
    opposite = "SELL" if open_side == "BUY" else "BUY"
    return [
        contract_line(start_at),
        order_line(start_at + 1, f"o{sequence}-{open_side}", f"quote-{sequence}-{'buy' if open_side == 'BUY' else 'sell'}", open_side, insert_time, volume, open_price),
        order_line(start_at + 1, f"o{sequence}-{opposite}", f"quote-{sequence}-{'sell' if open_side == 'BUY' else 'buy'}", opposite, insert_time, volume, open_price + 30.0),
        trade_line(start_at + 2, f"o{sequence}-{open_side}", f"quote-{sequence}-{'buy' if open_side == 'BUY' else 'sell'}", open_side, volume, open_price, f"t{sequence}", open_time),
        order_line(start_at + 3, f"o{sequence + 1}", f"flatten-{sequence + 1}", opposite, close_time, volume, close_price, "ALLTRADED"),
        trade_line(start_at + 4, f"o{sequence + 1}", f"flatten-{sequence + 1}", opposite, volume, close_price, f"tf{sequence}", close_time),
    ]


class BuildDaysTests(unittest.TestCase):
    def test_one_round_record_from_a_timed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T155000.000000Z-abc",
                {"al2609@SHFE": quoted_round(start_at=10, sequence=3, insert_time="2026-08-17T23:50:00+08:00", open_time="2026-08-17T23:50:42+08:00")},
            )

            days, skipped = report.build_days(root)

            self.assertEqual(skipped, 0)
            self.assertEqual(list(days), ["2026-08-17"])
            contract = days["2026-08-17"].contracts[0]
            self.assertEqual(contract.contract, "al2609@SHFE")
            self.assertEqual(len(contract.rounds), 1)
            record = contract.rounds[0]
            self.assertEqual(record.side, "BUY")
            self.assertEqual(record.submit_time, "2026-08-17T23:50:00+08:00")
            self.assertEqual(record.submit_price, 23990.0)
            self.assertEqual(record.submit_volume, 1)
            self.assertEqual(record.open_first_time, "2026-08-17T23:50:42+08:00")
            self.assertEqual(record.open_avg_price, 23990.0)
            self.assertEqual(record.open_volume, 1)
            self.assertEqual(record.close_first_time, "2026-08-17T23:51:10+08:00")
            self.assertEqual(record.close_avg_price, 23960.0)
            self.assertEqual(record.close_volume, 1)

    def test_night_session_fill_groups_by_exchange_trading_day(self) -> None:
        """The gateway encodes the trading day in the date part, ahead of the calendar day."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260814T155000.000000Z-abc",
                {
                    "al2609@SHFE": quoted_round(
                        start_at=10,
                        sequence=1,
                        insert_time="2026-08-17T23:50:00+08:00",
                        open_time="2026-08-17T23:50:42+08:00",
                    )
                },
            )

            days, skipped = report.build_days(root)

            self.assertEqual(list(days), ["2026-08-17"])
            self.assertEqual(skipped, 0)

    def test_legacy_run_without_exchange_time_is_skipped_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = quoted_round(start_at=10, sequence=1, insert_time=None, open_time=None)
            for record in legacy:
                data = record["event"]["data"]
                data.pop("exchange_time", None)
            write_run(root, "20260813T010000.000000Z-old", {"al2609@SHFE": legacy})

            days, skipped = report.build_days(root)

            self.assertEqual(days, {})
            self.assertEqual(skipped, 1)

    def test_partial_open_and_multi_fill_flatten_merge_into_one_round(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [
                contract_line(1),
                order_line(2, "o1", "quote-1-buy", "BUY", "2026-08-17T21:00:00+08:00", volume=2, price=23990.0),
                trade_line(3, "o1", "quote-1-buy", "BUY", 1, 23990.0, "t1", "2026-08-17T21:00:05+08:00"),
                trade_line(4, "o1", "quote-1-buy", "BUY", 1, 23995.0, "t2", "2026-08-17T21:00:09+08:00"),
                order_line(5, "o2", "flatten-2", "SELL", "2026-08-17T21:00:15+08:00", volume=2, price=23960.0, status="ALLTRADED"),
                trade_line(6, "o2", "flatten-2", "SELL", 1, 23960.0, "tf1", "2026-08-17T21:00:15+08:00"),
                order_line(7, "o3", "flatten-3", "SELL", "2026-08-17T21:00:18+08:00", volume=1, price=23955.0, status="ALLTRADED"),
                trade_line(8, "o3", "flatten-3", "SELL", 1, 23955.0, "tf2", "2026-08-17T21:00:18+08:00"),
            ]
            write_run(root, "20260817T130000.000000Z-abc", {"al2609@SHFE": lines})

            days, skipped = report.build_days(root)

            self.assertEqual(skipped, 0)
            rounds = days["2026-08-17"].contracts[0].rounds
            self.assertEqual(len(rounds), 1)
            record = rounds[0]
            self.assertEqual(record.submit_volume, 2)
            self.assertEqual(record.open_volume, 2)
            self.assertEqual(record.open_avg_price, 23992.5)
            self.assertEqual(record.open_first_time, "2026-08-17T21:00:05+08:00")
            self.assertEqual(record.open_last_time, "2026-08-17T21:00:09+08:00")
            self.assertEqual(record.close_volume, 2)
            self.assertEqual(record.close_avg_price, 23957.5)
            self.assertEqual(record.close_first_time, "2026-08-17T21:00:15+08:00")
            self.assertEqual(record.close_last_time, "2026-08-17T21:00:18+08:00")

    def test_two_rounds_and_cancelled_quotes_without_fills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = quoted_round(
                start_at=10,
                sequence=1,
                insert_time="2026-08-17T21:00:00+08:00",
                open_time="2026-08-17T21:00:42+08:00",
            )
            lines += [
                order_line(30, "o5", "quote-5-buy", "BUY", "2026-08-17T21:10:00+08:00"),
                order_line(31, "o5", "quote-5-sell", "SELL", "2026-08-17T21:10:00+08:00", status="CANCELLED"),
                order_line(32, "o5", "quote-5-buy", "BUY", "2026-08-17T21:10:00+08:00", status="CANCELLED"),
            ]
            lines += quoted_round(
                start_at=50,
                sequence=7,
                insert_time="2026-08-17T21:15:00+08:00",
                open_time="2026-08-17T21:15:30+08:00",
                open_side="SELL",
                open_price=24020.0,
                close_price=24050.0,
            )
            write_run(root, "20260817T130000.000000Z-abc", {"al2609@SHFE": lines})

            days, _ = report.build_days(root)

            rounds = days["2026-08-17"].contracts[0].rounds
            self.assertEqual(len(rounds), 2)
            self.assertEqual(rounds[0].side, "BUY")
            self.assertEqual(rounds[1].side, "SELL")
            self.assertEqual(rounds[1].submit_price, 24020.0)
            self.assertEqual(rounds[1].close_avg_price, 24050.0)

    def test_multiple_contracts_and_runs_group_under_one_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T130000.000000Z-run1",
                {"al2609@SHFE": quoted_round(start_at=10, sequence=1, insert_time="2026-08-17T21:00:00+08:00", open_time="2026-08-17T21:00:42+08:00")},
            )
            cu_lines = []
            for record in quoted_round(start_at=10, sequence=2, insert_time="2026-08-17T21:30:00+08:00", open_time="2026-08-17T21:30:42+08:00"):
                record["event"]["data"]["symbol"] = "cu2609"
                cu_lines.append(record)
            write_run(
                root,
                "20260817T140000.000000Z-run2",
                {"cu2609@SHFE": cu_lines},
            )

            days, skipped = report.build_days(root)

            self.assertEqual(skipped, 0)
            self.assertEqual(list(days), ["2026-08-17"])
            self.assertEqual(
                sorted(contract.contract for contract in days["2026-08-17"].contracts),
                ["al2609@SHFE", "cu2609@SHFE"],
            )


class RenderAndCliTests(unittest.TestCase):
    def test_main_writes_self_contained_html_with_round_rows(self) -> None:
        with tempfile.TemporaryDirectory() as audit_tmp, tempfile.TemporaryDirectory() as out_tmp:
            root = Path(audit_tmp)
            write_run(
                root,
                "20260817T155000.000000Z-abc",
                {"al2609@SHFE": quoted_round(start_at=10, sequence=3, insert_time="2026-08-17T23:50:00+08:00", open_time="2026-08-17T23:50:42+08:00")},
                account_lines=[{"at": 9.0, "balance": 1_000_000.0, "available": 900_000.0}],
            )
            argv = [
                "report.py",
                "--audit-dir",
                str(root),
                "--out-dir",
                str(out_tmp),
            ]
            with patch("sys.argv", argv):
                exit_code = report.main()

            self.assertEqual(exit_code, 0)
            out_path = Path(out_tmp) / "trades-20260817.html"
            html_text = out_path.read_text(encoding="utf-8")
            self.assertIn("al2609@SHFE", html_text)
            self.assertIn("23:50:00", html_text)
            self.assertIn("23990", html_text)
            self.assertIn("23960", html_text)
            self.assertNotIn("http://", html_text)
            self.assertNotIn("https://", html_text)

    def test_main_with_date_generates_only_that_day_and_reports_skips(self) -> None:
        with tempfile.TemporaryDirectory() as audit_tmp, tempfile.TemporaryDirectory() as out_tmp:
            root = Path(audit_tmp)
            write_run(
                root,
                "20260817T155000.000000Z-abc",
                {"al2609@SHFE": quoted_round(start_at=10, sequence=1, insert_time="2026-08-17T23:50:00+08:00", open_time="2026-08-17T23:50:42+08:00")},
            )
            legacy = quoted_round(start_at=10, sequence=1, insert_time=None, open_time=None)
            for record in legacy:
                record["event"]["data"].pop("exchange_time", None)
            write_run(root, "20260813T010000.000000Z-old", {"al2609@SHFE": legacy})
            argv = [
                "report.py",
                "--audit-dir",
                str(root),
                "--out-dir",
                str(out_tmp),
                "--date",
                "20260817",
            ]
            with patch("sys.argv", argv), patch("sys.stdout") as stdout:
                exit_code = report.main()
            printed = "".join(call.args[0] for call in stdout.write.call_args_list)

            self.assertEqual(exit_code, 0)
            self.assertEqual(list(Path(out_tmp).iterdir()), [Path(out_tmp) / "trades-20260817.html"])
            self.assertIn("跳过 1 个缺少交易所时间戳的 run", printed)


if __name__ == "__main__":
    unittest.main()
