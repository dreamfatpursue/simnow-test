import json
import tempfile
import unittest
from pathlib import Path

import report
from live_grid.audit import AuditWriter
from live_grid.config import MultiContractConfig, StrategyConfig
from live_grid.session import ContractEvent, InterruptEvent, LiveGridSession, PositionQueryCompleteEvent


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def strategy_doc(symbol: str = "rb2601", exchange: str = "SHFE", target_lots: int = 1) -> dict:
    config = StrategyConfig.from_mapping(
        {
            "version": 1,
            "symbol": symbol,
            "exchange": exchange,
            "target_lots": target_lots,
            "max_tick_age_seconds": 60,
            "quote_windows": [{"start": "00:00", "end": "23:59"}],
        }
    )
    return {"audit_schema_version": 2, "effective": config.effective, "sha256": config.sha256}


class RunReportTests(unittest.TestCase):
    def test_run_mode_generates_single_run_html_from_structured_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "20260818T010203.000000Z-run01"
            contract_dir = run_dir / "rb2601@SHFE"
            contract_dir.mkdir(parents=True)
            effective = strategy_doc()
            write_json(run_dir / "effective_strategy.json", effective)
            write_json(contract_dir / "effective_strategy.json", effective)
            write_json(
                run_dir / "summary.json",
                {
                    "all_finished": True,
                    "contracts": [
                        {
                            "target_symbol": "rb2601",
                            "target_exchange": "SHFE",
                            "terminal_state": "FINISHED",
                            "round_trips": 0,
                            "failure_reason": None,
                            "stop_reason": "max_round_trips",
                            "final_net_position": 0,
                        }
                    ],
                },
            )
            write_json(
                contract_dir / "summary.json",
                {
                    "target_symbol": "rb2601",
                    "target_exchange": "SHFE",
                    "terminal_state": "FINISHED",
                    "final_net_position": 0,
                },
            )
            events = [
                {
                    "at": 1.0,
                    "event": {
                        "type": "ContractEvent",
                        "data": {"symbol": "rb2601", "exchange": "SHFE", "product_code": "rb", "pricetick": 10.0, "size": 5.0},
                    },
                    "state_before": "WAITING_FOR_CONTRACT",
                    "state_after": "WAITING_FOR_STABLE_QUOTE",
                    "actions": [],
                },
                {
                    "at": 2.0,
                    "event": {"type": "ClockEvent", "data": {"at": 2.0}},
                    "state_before": "WAITING_FOR_STABLE_QUOTE",
                    "state_after": "QUOTING",
                    "actions": [
                        {
                            "type": "Action",
                            "data": {
                                "kind": "submit_order",
                                "payload": {
                                    "client_id": "quote-1-buy",
                                    "symbol": "rb2601",
                                    "exchange": "SHFE",
                                    "side": "BUY",
                                    "offset": "OPEN",
                                    "order_type": "LIMIT",
                                    "price": 50.0,
                                    "volume": 1,
                                },
                            },
                        },
                        {
                            "type": "Action",
                            "data": {
                                "kind": "submit_order",
                                "payload": {
                                    "client_id": "quote-1-sell",
                                    "symbol": "rb2601",
                                    "exchange": "SHFE",
                                    "side": "SELL",
                                    "offset": "OPEN",
                                    "order_type": "LIMIT",
                                    "price": 150.0,
                                    "volume": 1,
                                },
                            },
                        },
                    ],
                    "trace": [
                        {
                            "code": "quote_submitted",
                            "client_ids": ["quote-1-buy", "quote-1-sell"],
                            "market": {"last_price": 100.0, "bid_price": 99.0, "ask_price": 101.0},
                            "calculation": {
                                "anchor_ticks": 10,
                                "anchor_price": 100.0,
                                "pricetick": 10.0,
                                "w_ticks": 2,
                                "d_ticks": 3,
                                "distance_ticks": 5,
                                "buy_price": 50.0,
                                "sell_price": 150.0,
                            },
                        }
                    ],
                },
                {
                    "at": 3.0,
                    "event": {
                        "type": "TickEvent",
                        "data": {
                            "symbol": "rb2601",
                            "exchange": "SHFE",
                            "last_price": 100.0,
                            "bid_price": 99.0,
                            "ask_price": 101.0,
                            "at": 3.0,
                        },
                    },
                    "state_before": "QUOTING",
                    "state_after": "REPLACING",
                    "actions": [
                        {
                            "type": "Action",
                            "data": {
                                "kind": "cancel_order",
                                "payload": {"client_id": "quote-1-buy", "order_id": "o-buy", "safety": True},
                            },
                        },
                        {
                            "type": "Action",
                            "data": {
                                "kind": "cancel_order",
                                "payload": {"client_id": "quote-1-sell", "order_id": "o-sell", "safety": True},
                            },
                        },
                    ],
                    "trace": [
                        {
                            "code": "market_pause",
                            "client_ids": ["quote-1-buy", "quote-1-sell"],
                            "market": {"last_price": 100.0, "bid_price": 99.0, "ask_price": 101.0},
                            "calculation": {
                                "distance_ticks": 5,
                                "spread_ticks": 2,
                                "protection_multiple": 3,
                                "passed": False,
                            },
                        }
                    ],
                },
                {
                    "at": 4.0,
                    "event": {"type": "ClockEvent", "data": {"at": 4.0}},
                    "state_before": "WAITING_FOR_STABLE_QUOTE",
                    "state_after": "QUOTING",
                    "actions": [
                        {
                            "type": "Action",
                            "data": {
                                "kind": "submit_order",
                                "payload": {
                                    "client_id": "quote-2-buy",
                                    "symbol": "rb2601",
                                    "exchange": "SHFE",
                                    "side": "BUY",
                                    "offset": "OPEN",
                                    "order_type": "LIMIT",
                                    "price": 40.0,
                                    "volume": 1,
                                },
                            },
                        },
                    ],
                    "trace": [
                        {
                            "code": "quote_submitted",
                            "client_ids": ["quote-2-buy"],
                            "market": {"last_price": 90.0, "bid_price": 89.0, "ask_price": 91.0},
                            "calculation": {
                                "anchor_ticks": 9,
                                "anchor_price": 90.0,
                                "pricetick": 10.0,
                                "w_ticks": 2,
                                "d_ticks": 3,
                                "distance_ticks": 5,
                                "buy_price": 40.0,
                                "sell_price": 140.0,
                            },
                            "replacement": {
                                "previous_client_ids": ["quote-1-buy", "quote-1-sell"],
                                "reason": "market_pause",
                            },
                        }
                    ],
                },
            ]
            (contract_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )

            output_dir = root / "reports"
            self.assertEqual(
                report.main(["--run-dir", str(run_dir), "--out-dir", str(output_dir)]),
                0,
            )

            output = output_dir / f"run-{run_dir.name}.html"
            self.assertTrue(output.exists())
            html = output.read_text(encoding="utf-8")
            self.assertIn("单 run 委托成交报告", html)
            self.assertIn("rb2601@SHFE", html)
            self.assertIn("quote-1-buy", html)
            self.assertIn("首次报价", html)
            self.assertIn("盘口保护失败，暂停报价", html)
            self.assertIn("替代前驱", html)
            self.assertIn('href="#order-quote-1-buy"', html)
            self.assertIn("50", html)

    def test_run_mode_renders_funds_and_round_gross_pnl_without_assigning_net_to_orders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "20260818T010203.000000Z-run02"
            contract_dir = run_dir / "rb2601@SHFE"
            contract_dir.mkdir(parents=True)
            effective = strategy_doc()
            write_json(run_dir / "effective_strategy.json", effective)
            write_json(contract_dir / "effective_strategy.json", effective)
            summary = {
                "all_finished": True,
                "contracts": [
                    {
                        "target_symbol": "rb2601",
                        "target_exchange": "SHFE",
                        "terminal_state": "FINISHED",
                        "round_trips": 1,
                        "failure_reason": None,
                        "stop_reason": "max_round_trips",
                        "final_net_position": 0,
                        "active_order_count": 0,
                    }
                ],
            }
            write_json(run_dir / "summary.json", summary)
            write_json(contract_dir / "summary.json", summary["contracts"][0])

            def event(at: float, event_type: str, data: dict, actions: list[dict] | None = None) -> dict:
                return {
                    "at": at,
                    "event": {"type": event_type, "data": data},
                    "state_before": "QUOTING",
                    "state_after": "QUOTING",
                    "actions": actions or [],
                }

            submit_buy = {
                "type": "Action",
                "data": {
                    "kind": "submit_order",
                    "payload": {
                        "client_id": "quote-1-buy",
                        "symbol": "rb2601",
                        "exchange": "SHFE",
                        "side": "BUY",
                        "offset": "OPEN",
                        "order_type": "LIMIT",
                        "price": 50.0,
                        "volume": 1,
                    },
                },
            }
            submit_flatten = {
                "type": "Action",
                "data": {
                    "kind": "submit_order",
                    "payload": {
                        "client_id": "flatten-2",
                        "symbol": "rb2601",
                        "exchange": "SHFE",
                        "side": "SELL",
                        "offset": "CLOSETODAY",
                        "order_type": "FAK",
                        "price": 45.0,
                        "volume": 1,
                    },
                },
            }
            events = [
                event(0.5, "ContractEvent", {"symbol": "rb2601", "exchange": "SHFE", "product_code": "rb", "pricetick": 10.0, "size": 5.0}),
                event(1.0, "ClockEvent", {"at": 1.0}, [submit_buy]),
                event(1.1, "OrderEvent", {"order_id": "o1", "client_id": "quote-1-buy", "status": "NOTTRADED", "traded": 0, "volume": 1, "price": 50.0, "exchange_time": "2026-08-18T09:00:01+08:00"}),
                event(1.2, "OrderEvent", {"order_id": "o1", "client_id": "quote-1-buy", "status": "PARTTRADED", "traded": 0, "volume": 1, "price": 50.0, "exchange_time": "2026-08-18T09:00:01+08:00"}),
                event(1.3, "OrderEvent", {"order_id": "o1", "client_id": "quote-1-buy", "status": "NOTTRADED", "traded": 0, "volume": 1, "price": 50.0, "exchange_time": "2026-08-18T09:00:01+08:00"}),
                event(2.0, "TradeEvent", {"order_id": "o1", "client_id": "quote-1-buy", "side": "BUY", "volume": 1, "price": 50.0, "trade_id": "t1", "exchange_time": "2026-08-18T09:00:02+08:00"}),
                event(3.1, "ClockEvent", {"at": 3.1}, [submit_flatten]),
                event(3.2, "OrderEvent", {"order_id": "f1", "client_id": "flatten-2", "status": "ALLTRADED", "traded": 1, "volume": 1, "price": 45.0, "exchange_time": "2026-08-18T09:00:03+08:00"}),
                event(3.3, "TradeEvent", {"order_id": "f1", "client_id": "flatten-2", "side": "SELL", "volume": 1, "price": 45.0, "trade_id": "t2", "exchange_time": "2026-08-18T09:00:03+08:00"}),
            ]
            events[1]["trace"] = [
                {
                    "code": "quote_submitted",
                    "client_ids": ["quote-1-buy"],
                    "calculation": {"buy_price": 50.0, "sell_price": 150.0, "distance_ticks": 5},
                }
            ]
            (contract_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n",
                encoding="utf-8",
            )
            (run_dir / "account.jsonl").write_text(
                json.dumps({"at": 0.0, "balance": 1000.0, "available": 900.0})
                + "\n"
                + json.dumps({"at": 4.0, "balance": 990.0, "available": 890.0})
                + "\n",
                encoding="utf-8",
            )

            output_dir = root / "reports"
            self.assertEqual(report.main(["--run-dir", str(run_dir), "--out-dir", str(output_dir)]), 0)
            html = (output_dir / f"run-{run_dir.name}.html").read_text(encoding="utf-8")
            self.assertIn("资金差净盈亏", html)
            self.assertIn("推算手续费", html)
            self.assertIn("-25.00", html)
            self.assertIn("-10.00", html)
            self.assertIn("o1", html)
            self.assertIn("CTP 委托号", html)
            self.assertIn("接受时间", html)
            self.assertIn("终态时间", html)
            self.assertIn("未成交 → 部分成交", html)
            self.assertNotIn("未成交 → 部分成交 → 未成交", html)
            self.assertNotIn("单笔委托净盈亏", html)
            self.assertNotIn("运行因果时间线", html)
            self.assertIn("原始数据", html)
            self.assertIn("买", html)
            self.assertIn("开仓", html)
            self.assertIn("正常结束", html)
            self.assertIn("达到配置的最大完成轮数后正常停止", html)

    def test_report_consumes_real_audit_writer_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "20260818T010203.000000Z-writer"
            contract_dir = run_dir / "rb2601@SHFE"
            config = StrategyConfig.from_mapping(
                {
                    "version": 1,
                    "symbol": "rb2601",
                    "exchange": "SHFE",
                    "target_lots": 1,
                    "max_tick_age_seconds": 60,
                    "quote_windows": [{"start": "00:00", "end": "23:59"}],
                }
            )
            writer = AuditWriter(config, directory=contract_dir)
            session = LiveGridSession(config, simnow_confirmed=True)

            def consume(event: object) -> None:
                state_before = session.state.value
                actions = session.handle(event)
                writer.record(
                    event,
                    actions,
                    session.state.value,
                    session.audit_events[-1]["at"],
                    state_before=state_before,
                    trace=session.last_audit_trace,
                )

            consume(ContractEvent("rb2601", "SHFE", 1.0, size=5.0, product_code="rb"))
            consume(PositionQueryCompleteEvent("position-1", "rb2601", "SHFE", 0))
            consume(InterruptEvent())
            writer.finish(session.summary())
            write_json(
                run_dir / "effective_strategy.json",
                {
                    "audit_schema_version": 2,
                    "effective": config.effective,
                    "sha256": config.sha256,
                },
            )
            write_json(
                run_dir / "summary.json",
                {"all_finished": True, "contracts": [session.summary()]},
            )

            output_dir = root / "reports"
            self.assertEqual(report.main(["--run-dir", str(run_dir), "--out-dir", str(output_dir)]), 0)
            html = (output_dir / f"run-{run_dir.name}.html").read_text(encoding="utf-8")
            self.assertNotIn("运行因果时间线", html)
            self.assertIn("操作员手动中断本 run", html)

    def test_run_mode_rejects_missing_trace_and_forbidden_nested_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def make_run(name: str, trace: list[dict] | None) -> tuple[Path, Path]:
                run_dir = root / name
                contract_dir = run_dir / "rb2601@SHFE"
                contract_dir.mkdir(parents=True)
                effective = strategy_doc()
                write_json(run_dir / "effective_strategy.json", effective)
                write_json(contract_dir / "effective_strategy.json", effective)
                write_json(
                    run_dir / "summary.json",
                    {"contracts": [{"target_symbol": "rb2601", "target_exchange": "SHFE", "terminal_state": "FINISHED"}]},
                )
                write_json(contract_dir / "summary.json", {"terminal_state": "FINISHED"})
                event = {
                    "at": 1.0,
                    "event": {"type": "ContractEvent", "data": {"symbol": "rb2601", "exchange": "SHFE", "product_code": "rb", "pricetick": 1.0, "size": 5.0}},
                    "actions": [],
                }
                if trace is not None:
                    event["trace"] = trace
                (contract_dir / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
                return run_dir, root / f"out-{name}"

            missing, missing_out = make_run("missing-trace", None)
            self.assertEqual(report.main(["--run-dir", str(missing), "--out-dir", str(missing_out)]), 2)
            self.assertFalse(missing_out.exists())

            unsafe, unsafe_out = make_run(
                "unsafe-trace",
                [{"code": "quote_submitted", "calculation": {"nested": {"password": "secret"}}}],
            )
            self.assertEqual(report.main(["--run-dir", str(unsafe), "--out-dir", str(unsafe_out)]), 2)
            self.assertFalse(unsafe_out.exists())

    def test_run_mode_keeps_multi_contract_success_and_failure_sections_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "20260818T010203.000000Z-multi"
            run_dir.mkdir()
            multi = MultiContractConfig.from_mapping(
                {
                    "version": 2,
                    "contracts": [
                        {
                            "symbol": "rb2601", "exchange": "SHFE", "target_lots": 1,
                            "w_ticks": 25, "d_ticks": 20, "s_ticks": 10, "max_round_trips": 3,
                            "max_tick_age_seconds": 60,
                            "quote_windows": [{"start": "00:00", "end": "23:59"}],
                        },
                        {
                            "symbol": "AP610", "exchange": "CZCE", "target_lots": 1,
                            "w_ticks": 30, "d_ticks": 12, "s_ticks": 5, "max_round_trips": 5,
                            "max_tick_age_seconds": 60,
                            "quote_windows": [{"start": "00:00", "end": "23:59"}],
                        },
                    ],
                }
            )
            root_effective = {
                "audit_schema_version": 2,
                "effective": multi.effective,
                "sha256": multi.sha256,
            }
            write_json(run_dir / "effective_strategy.json", root_effective)
            contracts = [("rb2601", "SHFE", "FINISHED", "rb-quote"), ("AP610", "CZCE", "FAILED", "ap-quote")]
            summary_rows = []
            for symbol, exchange, terminal, client_id in contracts:
                contract_dir = run_dir / f"{symbol}@{exchange}"
                contract_dir.mkdir()
                effective = {
                    "audit_schema_version": 2,
                    "effective": multi.contracts[0 if symbol == "rb2601" else 1].effective,
                    "sha256": multi.sha256,
                }
                write_json(contract_dir / "effective_strategy.json", effective)
                write_json(contract_dir / "summary.json", {"terminal_state": terminal})
                summary_rows.append(
                    {
                        "target_symbol": symbol,
                        "target_exchange": exchange,
                        "terminal_state": terminal,
                        "round_trips": 0,
                        "failure_reason": "demo_failure" if terminal == "FAILED" else None,
                        "stop_reason": None,
                        "final_net_position": 0 if terminal == "FINISHED" else None,
                        "active_order_count": 0,
                    }
                )
                event = {
                    "at": 1.0,
                    "event": {
                        "type": "ContractEvent",
                        "data": {"symbol": symbol, "exchange": exchange, "product_code": symbol.rstrip("0123456789").lower(), "pricetick": 1.0, "size": 5.0},
                    },
                    "actions": [],
                    "trace": [
                        {
                            "code": "contract_metadata",
                            "calculation": {"symbol": symbol, "exchange": exchange},
                        }
                    ],
                }
                event["event"]["data"]["client_id"] = client_id
                (contract_dir / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            write_json(run_dir / "summary.json", {"all_finished": False, "contracts": summary_rows})

            output_dir = root / "reports"
            self.assertEqual(report.main(["--run-dir", str(run_dir), "--out-dir", str(output_dir)]), 0)
            html = (output_dir / f"run-{run_dir.name}.html").read_text(encoding="utf-8")
            self.assertIn("rb2601@SHFE", html)
            self.assertIn("AP610@CZCE", html)
            self.assertIn("demo_failure", html)
            self.assertIn("交易所：SHFE", html)
            self.assertIn("交易所：CZCE", html)
            self.assertIn("品种代码：rb", html)
            self.assertIn("准确合约：rb2601", html)
            self.assertIn("<td>rb2601@SHFE</td><td>25</td><td>20</td><td>10</td><td>3</td>", html)
            self.assertIn("<td>AP610@CZCE</td><td>30</td><td>12</td><td>5</td><td>5</td>", html)

    def test_historical_shared_parameters_remain_readable_without_current_defaults(self) -> None:
        single = strategy_doc()["effective"]
        local_keys = {"symbol", "exchange", "target_lots", "max_tick_age_seconds", "quote_windows"}
        common = {k: v for k, v in single.items() if k not in local_keys}
        common.update(version=2, w_ticks=27, max_round_trips=7)
        local = {k: single[k] for k in local_keys}
        effective = {**common, "contracts": [local, {**local, "symbol": "rb2610"}]}
        path = Path("historical/effective_strategy.json")
        self.assertIs(report._validate_effective_payload(effective, path, run_level=True), effective)
        html = report._render_run_parameters(effective)
        self.assertEqual(html.count("<td>27</td><td>20</td><td>10</td><td>7</td>"), 2)
        self.assertIn("<details><summary>完整生效配置", html)
        del effective["w_ticks"]
        with self.assertRaisesRegex(report.RunReportError, "缺少字段 w_ticks"):
            report._validate_effective_payload(effective, path, run_level=True)

    def test_fak_cancelled_without_strategy_cancel_explains_exchange_kill(self) -> None:
        order = report.RunOrder(
            client_id="flatten-7",
            payload={"order_type": "FAK", "side": "BUY", "offset": "CLOSE", "price": 4516.6, "volume": 1},
            order_id="1_301937953_11",
            final_status="CANCELLED",
            traded=0,
            actions=[{"at": 1.0, "kind": "submit_order", "payload": {"order_type": "FAK"}}],
            status_events=[{"at": 1.1, "status": "CANCELLED", "traded": 0, "exchange_time": "2026-09-03T14:23:14+08:00"}],
            traces=[
                {
                    "code": "flatten_submitted",
                    "calculation": {
                        "side": "BUY",
                        "offset": "CLOSE",
                        "volume": 1,
                        "reprice_attempt": 3,
                        "market_executable_price": 4516.6,
                        "initial_executable_price": 4516.6,
                        "adverse_price_limit": 4518.6,
                        "actual_price": 4516.6,
                    },
                },
                {
                    "code": "flatten_terminal",
                    "calculation": {"order_id": "1_301937953_11", "status": "CANCELLED", "traded": 0, "volume": 1},
                },
            ],
        )
        html = report._render_run_order_details(order)
        self.assertIn("Fill-And-Kill", html)
        self.assertIn("不是策略主动撤单", html)
        self.assertIn("FAK 撤销原因：本笔完全未成交", html)


if __name__ == "__main__":
    unittest.main()
