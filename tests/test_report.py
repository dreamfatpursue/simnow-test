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


def run_summary_doc(
    contract_rows: list[dict],
    terminal_states: dict[str, str] | None = None,
) -> dict:
    return {
        "terminal_states": terminal_states or {},
        "contracts": contract_rows,
    }


def contract_summary_row(
    symbol: str = "al2609",
    exchange: str = "SHFE",
    terminal_state: str = "FINISHED",
    round_trips: int = 1,
    failure_reason: str | None = None,
    stop_reason: str | None = None,
) -> dict:
    return {
        "target_symbol": symbol,
        "target_exchange": exchange,
        "terminal_state": terminal_state,
        "round_trips": round_trips,
        "failure_reason": failure_reason,
        "stop_reason": stop_reason,
    }


class BuildDaysTests(unittest.TestCase):
    def test_submit_time_prefers_exchange_report_over_local_submitting_push(self) -> None:
        """实盘序列：vnpy 先推无时间的 SUBMITTING，交易所回报带 InsertTime 在后。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [
                contract_line(1),
                order_line(2, "o1", "quote-1-buy", "BUY", None),
                order_line(3, "o1", "quote-1-buy", "BUY", "2026-08-17T21:00:16+08:00"),
                order_line(4, "o1", "quote-1-sell", "SELL", "2026-08-17T21:00:16+08:00"),
                trade_line(5, "o1", "quote-1-buy", "BUY", 1, 7856.0, "t1", "2026-08-17T21:00:30+08:00"),
                order_line(6, "o2", "flatten-2", "SELL", "2026-08-17T21:00:32+08:00", price=7856.0, status="ALLTRADED"),
                trade_line(7, "o2", "flatten-2", "SELL", 1, 7856.0, "tf1", "2026-08-17T21:00:32+08:00"),
            ]
            write_run(root, "20260817T130000.000000Z-abc", {"al2609@SHFE": lines})

            days, _ = report.build_days(root)

            record = days["2026-08-17"].contracts[0].rounds[0]
            self.assertEqual(record.submit_time, "2026-08-17T21:00:16+08:00")

    def test_submit_time_prefers_exchange_report_over_local_submitting_push_funds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = funds_round_lines()
            # 实盘序列里第一条委托回报来自 vnpy 本地推送，没有交易所时间。
            local_push = order_line(9.9, "o1", "quote-1-buy", "BUY", None)
            local_push["actions"] = [
                {"type": "Action", "data": {"kind": "submit_order", "payload": {"client_id": "quote-1-buy"}}}
            ]
            lines.insert(1, local_push)
            write_run(
                root,
                "20260817T130000.000000Z-run1",
                {"al2609@SHFE": lines},
                account_lines=[
                    {"at": 9.0, "balance": 1_000_000.0, "available": 900_000.0},
                    {"at": 21.0, "balance": 999_820.0, "available": 899_820.0},
                ],
            )

            days, _ = report.build_days(root)

            self.assertEqual(len(days["2026-08-17"].funds), 1)
            record = days["2026-08-17"].contracts[0].rounds[0]
            self.assertEqual(record.submit_time, "2026-08-17T21:00:00+08:00")

    def test_submit_time_uses_earliest_report_time_across_order_callbacks(self) -> None:
        """实盘序列：同一委托的录入确认回报可能比撮合回报的时间戳晚 1 秒。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [
                contract_line(1),
                order_line(2, "o1", "quote-1-buy", "BUY", None),
                order_line(3, "o1", "quote-1-buy", "BUY", "2026-08-17T09:55:33+08:00"),
                order_line(4, "o1", "quote-1-buy", "BUY", "2026-08-17T09:55:32+08:00"),
                trade_line(5, "o1", "quote-1-buy", "BUY", 1, 7852.0, "t1", "2026-08-17T09:55:32+08:00"),
                order_line(6, "o2", "flatten-2", "SELL", "2026-08-17T09:55:32+08:00", price=7851.0, status="ALLTRADED"),
                trade_line(7, "o2", "flatten-2", "SELL", 1, 7851.0, "tf1", "2026-08-17T09:55:32+08:00"),
            ]
            write_run(root, "20260817T130000.000000Z-abc", {"al2609@SHFE": lines})

            days, _ = report.build_days(root)

            record = days["2026-08-17"].contracts[0].rounds[0]
            self.assertEqual(record.submit_time, "2026-08-17T09:55:32+08:00")

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


class GrossPnlAndOverviewTests(unittest.TestCase):
    def test_round_rows_show_price_diff_ticks_and_gross_pnl(self) -> None:
        model = self._one_round_model()
        html_text = report.render_html(model)

        self.assertIn("价差", html_text)
        self.assertIn("tick 数", html_text)
        self.assertIn("毛盈亏", html_text)
        self.assertIn("-30", html_text)
        self.assertIn("-6", html_text)
        self.assertIn("-150", html_text)
        self.assertIn("毛盈亏未含手续费", html_text)

    def test_short_round_pnl_is_signed_by_opening_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = quoted_round(
                start_at=50,
                sequence=7,
                insert_time="2026-08-17T21:15:00+08:00",
                open_time="2026-08-17T21:15:30+08:00",
                open_side="SELL",
                open_price=24050.0,
                close_price=24020.0,
            )
            write_run(root, "20260817T130000.000000Z-abc", {"al2609@SHFE": lines})

            days, _ = report.build_days(root)

            html_text = report.render_html(days["2026-08-17"])
            self.assertIn("+30", html_text)
            self.assertIn("+6", html_text)
            self.assertIn("+150", html_text)

    def test_run_overview_lists_every_contract_with_state_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T130000.000000Z-run1",
                {"al2609@SHFE": quoted_round(start_at=10, sequence=1, insert_time="2026-08-17T21:00:00+08:00", open_time="2026-08-17T21:00:42+08:00")},
                run_summary=run_summary_doc(
                    [contract_summary_row(round_trips=1, stop_reason="session_end")],
                    {"al2609@SHFE": "FINISHED"},
                ),
            )
            # 发过委托但零成交的 run：有报单时间，应出现在总览里。
            write_run(
                root,
                "20260817T140000.000000Z-run2",
                {
                    "cu2609@SHFE": [
                        contract_line(1),
                        order_line(2, "o1", "quote-1-buy", "BUY", "2026-08-17T21:20:00+08:00", status="CANCELLED"),
                        order_line(3, "o1", "quote-1-sell", "SELL", "2026-08-17T21:20:00+08:00", status="CANCELLED"),
                    ]
                },
                run_summary=run_summary_doc(
                    [contract_summary_row(symbol="cu2609", terminal_state="FAILED", round_trips=0, failure_reason="nonzero_startup_position")],
                    {"cu2609@SHFE": "FAILED"},
                ),
            )

            days, skipped = report.build_days(root)

            self.assertEqual(skipped, 0)
            html_text = report.render_html(days["2026-08-17"])
            self.assertIn("当日 run 总览", html_text)
            self.assertIn("20260817T130000.000000Z-run1", html_text)
            self.assertIn("20260817T140000.000000Z-run2", html_text)
            self.assertIn("nonzero_startup_position", html_text)
            self.assertIn("session_end", html_text)
            self.assertIn("FINISHED", html_text)
            self.assertIn("FAILED", html_text)

    @staticmethod
    def _one_round_model() -> report.DayModel:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T155000.000000Z-abc",
                {"al2609@SHFE": quoted_round(start_at=10, sequence=3, insert_time="2026-08-17T23:50:00+08:00", open_time="2026-08-17T23:50:42+08:00")},
            )
            days, _ = report.build_days(root)
            return days["2026-08-17"]


def funds_round_lines() -> list[dict]:
    """One BUY round whose submit action sits at 10 and flatten terminal at 20."""
    quote = order_line(10, "o1", "quote-1-buy", "BUY", "2026-08-17T21:00:00+08:00")
    quote["actions"] = [
        {
            "type": "Action",
            "data": {
                "kind": "submit_order",
                "payload": {"client_id": "quote-1-buy", "side": "BUY", "volume": 1, "price": 23990.0},
            },
        }
    ]
    return [
        contract_line(1),
        quote,
        order_line(11, "o1", "quote-1-sell", "SELL", "2026-08-17T21:00:00+08:00"),
        trade_line(15, "o1", "quote-1-buy", "BUY", 1, 23990.0, "t1", "2026-08-17T21:00:42+08:00"),
        order_line(20, "o2", "flatten-2", "SELL", "2026-08-17T21:01:10+08:00", price=23960.0, status="ALLTRADED"),
        trade_line(20, "o2", "flatten-2", "SELL", 1, 23960.0, "tf1", "2026-08-17T21:01:10+08:00"),
    ]


class FundsSummaryTests(unittest.TestCase):
    def test_real_net_pnl_and_implied_fees_from_balance_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T130000.000000Z-run1",
                {"al2609@SHFE": funds_round_lines()},
                account_lines=[
                    {"at": 9.0, "balance": 1_000_000.0, "available": 900_000.0},
                    {"at": 21.0, "balance": 999_820.0, "available": 899_820.0},
                ],
            )

            days, _ = report.build_days(root)
            html_text = report.render_html(days["2026-08-17"])

            self.assertIn("资金汇总", html_text)
            self.assertIn("1,000,000.00", html_text)
            self.assertIn("999,820.00", html_text)
            self.assertIn("-180.00", html_text)
            self.assertIn("30.00", html_text)
            self.assertIn("净盈亏已含手续费", html_text)
            self.assertIn("手续费为推算值", html_text)

    def test_missing_boundary_snapshot_falls_back_to_nearest_with_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T130000.000000Z-run1",
                {"al2609@SHFE": funds_round_lines()},
                account_lines=[
                    {"at": 12.0, "balance": 1_000_000.0, "available": 900_000.0},
                    {"at": 18.0, "balance": 999_820.0, "available": 899_820.0},
                ],
            )

            days, _ = report.build_days(root)

            funds = days["2026-08-17"].funds
            self.assertEqual(len(funds), 1)
            self.assertEqual(funds[0].start_balance, 1_000_000.0)
            self.assertEqual(funds[0].end_balance, 999_820.0)
            self.assertTrue(funds[0].boundary_note)
            html_text = report.render_html(days["2026-08-17"])
            self.assertIn("边界快照缺失", html_text)

    def test_day_fees_only_count_runs_with_funds_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T130000.000000Z-run1",
                {"al2609@SHFE": funds_round_lines()},
                account_lines=[
                    {"at": 9.0, "balance": 1_000_000.0, "available": 900_000.0},
                    {"at": 21.0, "balance": 999_820.0, "available": 899_820.0},
                ],
            )
            # 同日另一个有成交但没有资金快照的 run：它的毛盈亏不得混入推算手续费。
            write_run(
                root,
                "20260817T140000.000000Z-run2",
                {
                    "cu2609@SHFE": quoted_round(
                        start_at=10,
                        sequence=1,
                        insert_time="2026-08-17T21:30:00+08:00",
                        open_time="2026-08-17T21:30:42+08:00",
                    )
                },
                account_lines=None,
            )

            days, _ = report.build_days(root)
            html_text = report.render_html(days["2026-08-17"])

            self.assertIn("推算手续费 30.00", html_text)
            self.assertNotIn("推算手续费 180.00", html_text)

    def test_volume_mismatch_round_has_no_gross_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [
                contract_line(1),
                order_line(2, "o1", "quote-1-buy", "BUY", "2026-08-17T21:00:00+08:00", volume=2, price=23990.0),
                trade_line(3, "o1", "quote-1-buy", "BUY", 1, 23990.0, "t1", "2026-08-17T21:00:05+08:00"),
                trade_line(4, "o1", "quote-1-buy", "BUY", 1, 23995.0, "t2", "2026-08-17T21:00:09+08:00"),
                order_line(5, "o2", "flatten-2", "SELL", "2026-08-17T21:00:15+08:00", volume=2, price=23960.0, status="ALLTRADED"),
                trade_line(6, "o2", "flatten-2", "SELL", 1, 23960.0, "tf1", "2026-08-17T21:00:15+08:00"),
            ]
            write_run(root, "20260817T130000.000000Z-abc", {"al2609@SHFE": lines})

            days, _ = report.build_days(root)
            html_text = report.render_html(days["2026-08-17"])

            self.assertNotIn("-162.5", html_text)
            self.assertNotIn("-6.5", html_text)

    def test_run_without_funds_snapshots_has_no_funds_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_run(
                root,
                "20260817T130000.000000Z-run1",
                {"al2609@SHFE": funds_round_lines()},
                account_lines=None,
            )

            days, _ = report.build_days(root)

            self.assertEqual(days["2026-08-17"].funds, [])
            html_text = report.render_html(days["2026-08-17"])
            self.assertIn("无资金快照", html_text)


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

    def test_open_flag_opens_the_generated_report_in_a_browser(self) -> None:
        with tempfile.TemporaryDirectory() as audit_tmp, tempfile.TemporaryDirectory() as out_tmp:
            root = Path(audit_tmp)
            write_run(
                root,
                "20260817T155000.000000Z-abc",
                {"al2609@SHFE": quoted_round(start_at=10, sequence=1, insert_time="2026-08-17T23:50:00+08:00", open_time="2026-08-17T23:50:42+08:00")},
            )
            argv = [
                "report.py",
                "--audit-dir",
                str(root),
                "--out-dir",
                str(out_tmp),
                "--open",
            ]
            with patch("sys.argv", argv), patch("webbrowser.open") as open_mock:
                exit_code = report.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(open_mock.call_count, 1)
            opened_uri = open_mock.call_args[0][0]
            self.assertTrue(opened_uri.endswith("trades-20260817.html"))


if __name__ == "__main__":
    unittest.main()
