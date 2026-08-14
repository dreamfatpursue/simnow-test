#!/usr/bin/env python3
"""Dedicated order-capable SimNow multi-contract report/cancel entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from live_grid.audit import MultiContractAuditWriter
from live_grid.config import MultiContractConfig, StrategyConfigError
from live_grid.ctp_adapter import CtpLiveGridAdapter
from live_grid.session import LiveGridSession, SessionState
from run import load_settings

_TERMINAL_STATES = {SessionState.FINISHED, SessionState.FAILED}


def print_preview(config: MultiContractConfig) -> None:
    print(json.dumps({"effective": config.effective, "sha256": config.sha256}, ensure_ascii=False, indent=2))


def _contract_key(session: LiveGridSession) -> str:
    return f"{session.target_symbol}@{session.target_exchange}"


def _run_summary(
    config: MultiContractConfig,
    sessions: list[LiveGridSession],
    *,
    failure_reason: str | None = None,
    run_failed: bool = False,
    terminal_override: str | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "strategy_hash": config.sha256,
        "terminal_states": {
            _contract_key(session): (
                session.state.value
                if session.state in _TERMINAL_STATES or terminal_override is None
                else terminal_override
            )
            for session in sessions
        },
        "all_finished": all(session.state == SessionState.FINISHED for session in sessions),
        "contracts": [session.summary() for session in sessions],
    }
    if failure_reason is not None:
        summary["failure_reason"] = failure_reason
    if run_failed:
        summary["terminal_state"] = "FAILED"
    return summary


def _finish_contract_summaries(
    sessions: list[LiveGridSession],
    audits: list,
    *,
    failure_reason: str | None = None,
    terminal_override: str | None = None,
) -> None:
    for session, audit in zip(sessions, audits):
        summary = session.summary()
        if failure_reason is not None and summary.get("failure_reason") is None:
            summary["failure_reason"] = failure_reason
        if terminal_override is not None and session.state not in _TERMINAL_STATES:
            summary["terminal_state"] = terminal_override
        audit.finish(summary)


def _wait_terminal(sessions: list[LiveGridSession]) -> None:
    while any(session.state not in _TERMINAL_STATES for session in sessions):
        time.sleep(0.2)


def _interrupt_and_wait(adapter: CtpLiveGridAdapter) -> None:
    adapter.interrupt()
    # 收口等待中再次 Ctrl-C 不得弃单而逃：继续等待 CTP 撤单/平仓终态。
    while any(session.state not in _TERMINAL_STATES for session in adapter.sessions):
        try:
            time.sleep(0.2)
        except KeyboardInterrupt:
            print("仍在等待 CTP 撤单/平仓终态，继续等待，不直接断开连接。", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="SimNow 多合约报撤联调入口")
    parser.add_argument("--config", required=True, help="无凭证多合约策略 JSON 配置")
    parser.add_argument("--confirm-simnow", action="store_true", help="确认当前连接是 SimNow")
    parser.add_argument("--audit-dir", default="audit", help="测试审计根目录")
    args = parser.parse_args()

    try:
        config = MultiContractConfig.from_json_file(args.config)
    except StrategyConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    run_audit: MultiContractAuditWriter | None = None
    sessions: list[LiveGridSession] = []
    adapter: CtpLiveGridAdapter | None = None
    try:
        try:
            run_audit = MultiContractAuditWriter(config, args.audit_dir)
            sessions = [
                LiveGridSession(contract, simnow_confirmed=args.confirm_simnow)
                for contract in config.contracts
            ]
            audits = run_audit.writers
            print_preview(config)
            if not args.confirm_simnow:
                _finish_contract_summaries(sessions, audits, failure_reason="confirmation_required")
                directory = run_audit.finish(
                    _run_summary(config, sessions, failure_reason="confirmation_required")
                )
                print(f"当前为预览模式：缺少 SimNow 确认，未连接且不会下单。审计目录={directory}")
                return 0
            settings = load_settings()
            adapter = CtpLiveGridAdapter(
                sessions=sessions,
                gateway_setting=settings.gateway_setting(),
                audits=audits,
            )
            adapter.start()
            _wait_terminal(sessions)
        except Exception as exc:
            print(f"启动失败: {exc}", file=sys.stderr)
            if adapter is not None:
                try:
                    _interrupt_and_wait(adapter)
                except Exception as cleanup_exc:
                    print(f"异常收口未完成: {cleanup_exc}", file=sys.stderr)
                adapter.close()
                adapter = None
            if run_audit is not None:
                audits = run_audit.writers
                _finish_contract_summaries(
                    sessions,
                    audits,
                    failure_reason=str(exc),
                    terminal_override="FAILED",
                )
                run_audit.finish(
                    _run_summary(
                        config,
                        sessions,
                        failure_reason=str(exc),
                        run_failed=True,
                        terminal_override="FAILED",
                    )
                )
            return 3
        except KeyboardInterrupt:
            if adapter is not None:
                _interrupt_and_wait(adapter)
        # 先关引擎再写摘要：否则摘要落盘后引擎仍可能投递迟到事件给已关闭的审计写入器。
        if adapter is not None:
            adapter.close()
            adapter = None
        audits = run_audit.writers
        _finish_contract_summaries(sessions, audits)
        directory = run_audit.finish(_run_summary(config, sessions))
        all_finished = all(session.state == SessionState.FINISHED for session in sessions)
        terminal_states = json.dumps({_contract_key(s): s.state.value for s in sessions}, ensure_ascii=False)
        print(f"终态={terminal_states} 审计目录={directory}")
        return 0 if all_finished else 1
    finally:
        if adapter is not None:
            adapter.close()
        if run_audit is not None:
            run_audit.close()


if __name__ == "__main__":
    raise SystemExit(main())
