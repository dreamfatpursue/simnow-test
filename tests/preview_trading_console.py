"""Offline UI preview: python tests/preview_trading_console.py (no CTP imports)."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class PreviewHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        scenario = parse_qs(urlsplit(self.path).query).get("state", ["active"])[0]
        data = json.loads((ROOT / "tests/console_ui_fixture.json").read_text())
        if scenario != "multi":
            data["contracts"] = data["contracts"][:1]
        if scenario == "idle":
            data["status"] = "idle"
        elif scenario == "starting":
            data = {"status": "starting", "pid": 0}
        elif scenario == "stale":
            data["data_stale"] = True
        elif scenario == "stopping":
            data["stop_requested"] = True
            data["overall_stage"] = "安全收口"
            data["contracts"][0].update(state="CLOSING_CANCELS", stage="安全收口")
        elif scenario == "risk":
            data["overall_stage"] = "风险"
            data["contracts"][0].update(state="RISK_HOLD", stage="风险", risk=True,
                                         risk_reason="cancel_timeout", risk_key="demo-risk")
        elif scenario == "terminal":
            data.update(status="terminal", overall_stage="终态", data_stale=True)
            data["contracts"][0].update(state="FINISHED", stage="终态", round_trips=5, active_order_count=0)
            for order in data["contracts"][0]["logical_orders"]:
                order.update(active=False, status="CANCELLED")
        elif scenario == "abnormal":
            data.update(status="process_abnormal_exit", run_risk={"key": "demo-crash", "reason": "process_abnormal_exit"})
        effective = json.loads((ROOT / "tests/console_ui_fixture.json").read_text())["effective"]
        # The test-only fetch substitute cannot reach the real service or launch any process.
        bootstrap = """<script>
        const demoSnapshot = SNAPSHOT;
        const demoEffective = EFFECTIVE;
        window.fetch = async (path, options = {}) => {
          let body;
          if (path === '/api/session') body = {token: 'offline-only'};
          else if (path === '/api/strategies') body = {strategies: [{name:'strategy-offline-demo.json',valid:true}]};
          else if (path === '/api/run/current') body = demoSnapshot;
          else if (path === '/api/preview') {
            const choice = JSON.parse(options.body);
            body = {strategy:choice.strategy, environment:choice.environment, market_data_mode:choice.allow_replay_market_data?'replay_override':'normal',effective:demoEffective,sha256:'offline-demo',confirmation:'offline-only',environment_status:{ready:true,missing:[]}};
          } else return {ok:false, json:async()=>({error:'离线演示禁止启动或停止交易'})};
          return {ok:true, json:async()=>structuredClone(body)};
        };
        </script>""".replace("SNAPSHOT", json.dumps(data).replace("<", "\\u003c")).replace("EFFECTIVE", json.dumps(effective))
        links = " · ".join(f'<a href="/?state={key}">{label}</a>' for key, label in [
            ("active", "单合约"), ("multi", "多合约"), ("idle", "启动预览"),
            ("starting", "启动中"), ("risk", "风险"), ("stale", "断更"),
            ("stopping", "收口"), ("terminal", "结束"), ("abnormal", "异常退出")])
        page = (ROOT / "trading_console/index.html").read_text()
        page = page.replace("<script>", bootstrap + "<script>", 1)
        page = page.replace("<main>", '<main><div class="message warn"><strong>离线演示 · 合成数据 · 不连接 CTP</strong><br>' + links + "</div>", 1)
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.send_error(405, "Offline preview is read-only")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8766), PreviewHandler)
    print("Offline preview: http://127.0.0.1:8766/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
