#!/usr/bin/env python3
"""Dedicated order-capable SimNow single-contract report/cancel entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
import time

from live_grid.audit import AuditWriter
from live_grid.config import StrategyConfig, StrategyConfigError
from live_grid.ctp_adapter import CtpLiveGridAdapter
from live_grid.session import SessionState
from run import load_settings


def print_preview(config: StrategyConfig) -> None:
    print(json.dumps({"effective": config.effective, "sha256": config.sha256}, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="SimNow 单合约报撤联调入口")
    parser.add_argument("--config", required=True, help="无凭证策略 JSON 配置")
    parser.add_argument("--confirm-simnow", action="store_true", help="确认当前连接是 SimNow")
    parser.add_argument("--confirm-hash", default="", help="有效策略哈希前缀，至少 8 位")
    parser.add_argument("--audit-dir", default="audit", help="测试审计根目录")
    args = parser.parse_args()

    try:
        config = StrategyConfig.from_json_file(args.config)
    except StrategyConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    try:
        audit = AuditWriter(config, args.audit_dir)
        print_preview(config)
        if not config.can_submit(simnow_confirmed=args.confirm_simnow, hash_prefix=args.confirm_hash):
            preview = _session(config, args).summary()
            preview["failure_reason"] = "confirmation_required"
            directory = audit.finish(preview)
            print(f"当前为预览模式：缺少匹配的 SimNow 确认或策略哈希确认，未连接且不会下单。审计目录={directory}")
            return 0
        settings = load_settings()
        adapter = CtpLiveGridAdapter(session=_session(config, args), gateway_setting=settings.gateway_setting(), audit=audit)
        adapter.start()
        while adapter.session.state not in {SessionState.FINISHED, SessionState.FAILED}:
            time.sleep(0.2)
        directory = audit.finish(adapter.session.summary())
        print(f"终态={adapter.session.state.value} 审计目录={directory}")
        return 0 if adapter.session.state == SessionState.FINISHED else 1
    except Exception as exc:
        print(f"启动失败: {exc}", file=sys.stderr)
        if "audit" in locals():
            if "adapter" in locals():
                try:
                    _interrupt_and_wait(adapter)
                except Exception as cleanup_exc:
                    print(f"异常收口未完成: {cleanup_exc}", file=sys.stderr)
                summary = adapter.session.summary()
            else:
                summary = {
                    "terminal_state": "FAILED",
                    "target_symbol": config.effective["symbol"],
                    "target_exchange": config.effective["exchange"],
                    "target_lots": config.effective["target_lots"],
                    "strategy_hash": config.sha256,
                }
            summary.update({"terminal_state": "FAILED", "failure_reason": str(exc)})
            audit.finish(summary)
        return 3
    except KeyboardInterrupt:
        if "adapter" in locals():
            _interrupt_and_wait(adapter)
            directory = audit.finish(adapter.session.summary())
            print(f"终态={adapter.session.state.value} 审计目录={directory}")
            return 0 if adapter.session.state == SessionState.FINISHED else 1
        return 130
    finally:
        if "adapter" in locals():
            adapter.close()
        elif "audit" in locals():
            audit.close()


def _interrupt_and_wait(adapter: CtpLiveGridAdapter) -> None:
    adapter.interrupt()
    while adapter.session.state not in {SessionState.FINISHED, SessionState.FAILED}:
        try:
            time.sleep(0.2)
        except KeyboardInterrupt:
            print("仍在等待 CTP 撤单/平仓终态，继续等待，不直接断开连接。", file=sys.stderr)


def _session(config: StrategyConfig, args: argparse.Namespace):
    from live_grid.session import LiveGridSession

    return LiveGridSession(config, simnow_confirmed=args.confirm_simnow, hash_prefix=args.confirm_hash)


if __name__ == "__main__":
    raise SystemExit(main())
