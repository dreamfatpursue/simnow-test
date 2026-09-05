#!/usr/bin/env python3
"""Start the fixed localhost trading console."""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

from trading_console import TradingConsoleServer, ConsoleState


HOST = "127.0.0.1"
PORT = 8765


def main() -> int:
    project_root = Path(__file__).resolve().parent
    try:
        server = TradingConsoleServer((HOST, PORT), ConsoleState(project_root))
    except OSError as exc:
        print(f"控制台启动失败：127.0.0.1:{PORT} 可能已被占用。{exc}", file=sys.stderr)
        return 2

    url = f"http://{HOST}:{PORT}/"
    print(f"交易控制台地址：{url}", flush=True)
    try:
        webbrowser.open(url)
    except Exception as exc:  # browser opening is best-effort; the service remains usable.
        print(f"默认浏览器打开失败，请手工访问 {url}：{exc}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("交易控制台已停止。", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
