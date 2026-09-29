import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_grid.account_recheck import LiveRecheck, evaluate_recheck, execute_recheck, read_latest_recheck
from live_grid.activity import ActivityIdentity, ActivityLock


def identity(root: str) -> ActivityIdentity:
    audit_dir = Path(root) / "audit" / "run-1"
    audit_dir.mkdir(parents=True)
    return ActivityIdentity(
        run_id="run-1", pid=123, started_at="2026-09-29T10:00:00+08:00",
        environment="first", market_data_mode="normal", strategy_hash="hash",
        contracts=("IF2610@CFFEX",), audit_dir=str(audit_dir),
    )


class AccountRecheckTests(unittest.TestCase):
    def test_pass_requires_two_empty_order_snapshots_and_zero_positions(self) -> None:
        result = evaluate_recheck(("IF2610@CFFEX",), (), (), ())
        self.assertEqual(result["status"], "passed")

        self.assertEqual(evaluate_recheck(
            ("IF2610@CFFEX",), (),
            (SimpleNamespace(symbol="IF2610", exchange="CFFEX", direction="LONG", volume=1),), (),
        )["status"], "risk")
        unknown = SimpleNamespace(symbol="IF2610", exchange="CFFEX", status="UNKNOWN", orderid="x")
        self.assertEqual(evaluate_recheck(
            ("IF2610@CFFEX",), (unknown,), (), (unknown,),
        )["status"], "risk")
        active = SimpleNamespace(symbol="IF2610", exchange="CFFEX", status="NOTTRADED", orderid="x")
        self.assertEqual(evaluate_recheck(
            ("IF2610@CFFEX",), (active,), (), (active,),
        )["status"], "risk")

    def test_query_sequence_is_order_position_order_and_never_trades(self) -> None:
        gateway = SimpleNamespace(query_order=Mock(side_effect=(11, 13)), query_position=Mock(return_value=12))
        runner = LiveRecheck(("IF2610@CFFEX",), 0.01)
        runner.bind_gateway(gateway)
        runner.on_contract(SimpleNamespace(data=SimpleNamespace(symbol="IF2610", exchange="CFFEX")))
        runner.on_log(SimpleNamespace(data=SimpleNamespace(msg="合约信息查询成功")))
        runner.on_order_complete(SimpleNamespace(data=SimpleNamespace(request_id=11, error_id=0, orders=())))
        runner.on_position_complete(SimpleNamespace(data=SimpleNamespace(request_id=12, error_id=0, positions=())))
        runner.on_order_complete(SimpleNamespace(data=SimpleNamespace(request_id=13, error_id=0, orders=())))

        self.assertEqual(runner.wait()["status"], "passed")
        self.assertEqual(gateway.query_order.call_count, 2)
        gateway.query_position.assert_called_once_with()
        self.assertFalse(hasattr(gateway, "send_order"))
        self.assertFalse(hasattr(gateway, "cancel_order"))

    def test_active_run_blocks_recheck_and_completed_result_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            lock_path = Path(root) / "audit" / ".active-run.lock"
            run = identity(root)
            held, _ = ActivityLock.try_acquire(lock_path, run)
            with patch("live_grid.account_recheck.run_live_recheck") as live:
                self.assertEqual(execute_recheck(Path(root), run.run_id, "context", 0.01), 0)
                live.assert_not_called()
            held.release()

            passed = {"status": "passed", "stage": "完成", "message": "通过", "contracts": [{"contract": "IF2610@CFFEX", "long_position": 0, "short_position": 0, "active_orders": 0, "unknown_orders": 0}]}
            with patch("live_grid.account_recheck.run_live_recheck", return_value=passed) as live:
                self.assertEqual(execute_recheck(Path(root), run.run_id, "context", 0.01), 0)
                live.assert_called_once()
            self.assertEqual(read_latest_recheck(run.audit_dir, run.run_id)["status"], "passed")


if __name__ == "__main__":
    unittest.main()
