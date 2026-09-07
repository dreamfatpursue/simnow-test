"""The offline preview uses the same contract list for every run scenario."""

import io
import json
import re
import unittest

from preview_trading_console import PreviewHandler


def render_scenario(scenario):
    handler = PreviewHandler.__new__(PreviewHandler)
    handler.path = f"/?state={scenario}"
    handler.send_response = lambda *args: None
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.wfile = io.BytesIO()
    handler.do_GET()
    page = handler.wfile.getvalue().decode()
    snapshot = json.loads(re.search(r"const demoSnapshot = (.*);", page)[1])
    return page, snapshot


class ConsolePreviewTests(unittest.TestCase):
    def test_all_run_scenarios_keep_the_contract_list(self):
        for scenario in ("active", "risk", "stale", "stopping", "terminal", "abnormal"):
            with self.subTest(scenario=scenario):
                page, snapshot = render_scenario(scenario)
                self.assertEqual(len(snapshot["contracts"]), 2)
                self.assertNotIn("单合约", page)
                self.assertNotIn("state=multi", page)
                self.assertIn("交易运行", page)

    def test_stop_and_terminal_scenarios_apply_to_all_contracts(self):
        _, stopping = render_scenario("stopping")
        self.assertTrue(all(item["stage"] == "安全收口" for item in stopping["contracts"]))
        _, terminal = render_scenario("terminal")
        for item in terminal["contracts"]:
            self.assertEqual(item["state"], "FINISHED")
            self.assertEqual(item["round_trips"], item["max_round_trips"])
            self.assertEqual(item["confirmed_position"]["net_position"], 0)
            self.assertFalse(any(order["active"] for order in item["logical_orders"]))


if __name__ == "__main__":
    unittest.main()
