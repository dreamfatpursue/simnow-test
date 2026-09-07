#!/usr/bin/env python3
"""Offline daily trade report generator built from audit directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from html import escape
from math import isfinite
from pathlib import Path
from typing import Any

from live_grid.audit import AUDIT_SCHEMA_VERSION, AuditError, AuditWriter


@dataclass(frozen=True)
class Fill:
    client_id: str
    side: str
    volume: int
    price: float
    exchange_time: str
    at: float = 0.0


@dataclass
class RoundRecord:
    """One round trip: the filled opening order's lifecycle through the flatten."""

    side: str
    submit_time: str | None
    submit_price: float | None
    submit_volume: int | None
    submit_at: float | None = None
    opens: list[Fill] = field(default_factory=list)
    closes: list[Fill] = field(default_factory=list)

    @property
    def open_volume(self) -> int:
        return sum(fill.volume for fill in self.opens)

    @property
    def open_avg_price(self) -> float:
        return _weighted_price(self.opens)

    @property
    def open_first_time(self) -> str | None:
        return self.opens[0].exchange_time if self.opens else None

    @property
    def open_last_time(self) -> str | None:
        return self.opens[-1].exchange_time if self.opens else None

    @property
    def close_volume(self) -> int:
        return sum(fill.volume for fill in self.closes)

    @property
    def close_avg_price(self) -> float:
        return _weighted_price(self.closes)

    @property
    def close_first_time(self) -> str | None:
        return self.closes[0].exchange_time if self.closes else None

    @property
    def close_last_time(self) -> str | None:
        return self.closes[-1].exchange_time if self.closes else None

    @property
    def wait_seconds(self) -> float | None:
        """挂单到成交的等待，按审计单调钟毫秒精度计算。"""
        if self.submit_at is None or not self.opens:
            return None
        return self.opens[0].at - self.submit_at

    @property
    def hold_seconds(self) -> float | None:
        """首次成交到最后平仓的持仓时长，按审计单调钟毫秒精度计算。"""
        if not self.opens or not self.closes:
            return None
        return self.closes[-1].at - self.opens[0].at

    @property
    def ending(self) -> str:
        """本轮结束方式：价差完成、FAK 平仓或未平仓。"""
        if self.closes:
            return "FAK 平仓"
        if len({fill.side for fill in self.opens}) > 1:
            return "价差完成"
        return "未平仓"


@dataclass
class ContractDay:
    contract: str
    pricetick: float | None = None
    size: float | None = None
    rounds: list[RoundRecord] = field(default_factory=list)


@dataclass
class RunFacts:
    directory: str
    contract_events: dict[str, list[dict[str, Any]]]
    run_summary: dict[str, Any]


@dataclass
class RunInfo:
    directory: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    gross_pnl: float | None = None


@dataclass
class RunFunds:
    directory: str
    start_balance: float
    end_balance: float
    boundary_note: bool = False

    @property
    def net(self) -> float:
        return self.end_balance - self.start_balance


@dataclass
class DayModel:
    day: str
    runs: list[RunInfo] = field(default_factory=list)
    contracts: list[ContractDay] = field(default_factory=list)
    funds: list[RunFunds] = field(default_factory=list)
    account_snapshots: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class RunOrder:
    client_id: str
    submit_at: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    order_id: str | None = None
    statuses: list[str] = field(default_factory=list)
    final_status: str | None = None
    traded: int = 0
    trade_volume: int = 0
    actions: list[dict[str, Any]] = field(default_factory=list)
    status_events: list[dict[str, Any]] = field(default_factory=list)
    traces: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    trade_ids: set[str] = field(default_factory=set, repr=False)
    successor_client_ids: list[str] = field(default_factory=list)


@dataclass
class RunContractReport:
    contract: str
    symbol: str
    exchange: str
    product_code: str | None
    pricetick: float | None
    size: float | None
    orders: list[RunOrder] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[RoundRecord] = field(default_factory=list)


@dataclass
class RunReport:
    directory: str
    effective: dict[str, Any]
    strategy_hash: str | None
    summary: dict[str, Any]
    contracts: list[RunContractReport]
    funds: RunFunds | None = None
    account_snapshot_count: int = 0
    gross_pnl: float | None = None


class RunReportError(ValueError):
    """Raised when a run cannot be rendered as a causal report."""


_COMMON_EFFECTIVE_KEYS = {
    "version",
    "w_ticks",
    "d_ticks",
    "s_ticks",
    "book_protection_multiple",
    "reanchor_confirmation_seconds",
    "stable_market_seconds",
    "action_limit_per_minute",
    "cancel_timeout_seconds",
    "flatten_timeout_seconds",
    "flatten_adverse_ticks",
    "max_round_trips",
    "closing_wait_seconds",
}
_CONTRACT_EFFECTIVE_KEYS = {"max_tick_age_seconds", "quote_windows"}


def _validate_effective_payload(effective: Any, path: Path, *, run_level: bool) -> dict[str, Any]:
    if not isinstance(effective, dict) or not effective:
        raise RunReportError(f"生效策略缺少有效参数: {path}")
    # 运行入口严格拒绝旧字段；离线报告保留读取历史审计目录的能力。
    legacy_schedule = "session_end_time" in effective and "quote_windows" not in effective
    required = set(_COMMON_EFFECTIVE_KEYS) | (set() if legacy_schedule else _CONTRACT_EFFECTIVE_KEYS) | {
        "symbol",
        "exchange",
        "target_lots",
    }
    if run_level and "contracts" in effective:
        required = {"version"}
        contracts = effective.get("contracts")
        if not isinstance(contracts, list) or not contracts:
            raise RunReportError(f"run 生效策略缺少完整合约列表: {path}")
        for entry in contracts:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("symbol"), str)
                or not entry.get("symbol")
                or not isinstance(entry.get("exchange"), str)
                or not entry.get("exchange")
                or "target_lots" not in entry
                or (not legacy_schedule and (
                    "max_tick_age_seconds" not in entry or "quote_windows" not in entry
                ))
            ):
                raise RunReportError(f"run 生效策略包含不完整合约项: {path}")
            # 仅历史读取兼容根节点公共参数；新记录的参数已全部在合约内。
            _validate_effective_payload({**effective, **entry}, path, run_level=False)
    missing = sorted(required - effective.keys())
    if missing:
        raise RunReportError(f"生效策略缺少字段 {', '.join(missing)}: {path}")
    return effective


def _validate_strategy_hash(effective: dict[str, Any], sha256: Any, path: Path) -> str:
    if not isinstance(sha256, str) or not sha256:
        raise RunReportError(f"生效策略缺少策略哈希: {path}")
    canonical = json.dumps(effective, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if sha256 != expected:
        raise RunReportError(f"策略哈希与生效参数不一致: {path}")
    return sha256


def _assert_report_safe(value: Any, path: Path) -> None:
    try:
        AuditWriter._assert_safe(value)
    except AuditError as exc:
        raise RunReportError(f"审计内容包含禁止字段: {path}") from exc


def _weighted_price(fills: list[Fill]) -> float:
    total = sum(fill.volume for fill in fills)
    if total == 0:
        return 0.0
    return sum(fill.price * fill.volume for fill in fills) / total


def _load_run(run_dir: Path) -> RunFacts | None:
    contract_events: dict[str, list[dict[str, Any]]] = {}
    for events_file in sorted(run_dir.glob("*/events.jsonl")):
        contract_events[events_file.parent.name] = [
            json.loads(record)
            for record in events_file.read_text(encoding="utf-8").splitlines()
            if record.strip()
        ]
    if not contract_events:
        return None
    summary_file = run_dir / "summary.json"
    if summary_file.exists():
        try:
            run_summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            run_summary = {}
    else:
        # 崩溃 run：进程没走到写摘要就退出，事件与资金流水仍然可信。
        run_summary = {"summary_missing": True}
    return RunFacts(run_dir.name, contract_events, run_summary)


def _trading_day_of(exchange_time: str) -> str | None:
    """网关时间戳的日期部分是自然日；夜盘成交滚入下一交易日。

    20:00 之后（夜盘时段）成交归属下一个交易日，周五夜盘跳过周末归入周一。
    节假日前夜交易所不开夜盘，因此该规则与交易日历在一切真实成交上一致。
    """
    try:
        moment = datetime.fromisoformat(exchange_time)
    except ValueError:
        return None
    day = moment.date()
    if moment.time() >= time(20, 0):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return day.isoformat()


def _run_day(run: RunFacts) -> str | None:
    """The run's exchange trading day: its earliest exchange time rolled by the night-session rule."""
    earliest: str | None = None
    for events in run.contract_events.values():
        for record in events:
            exchange_time = record["event"]["data"].get("exchange_time")
            if not exchange_time:
                continue
            day = _trading_day_of(exchange_time)
            if day and (earliest is None or day < earliest):
                earliest = day
    return earliest


def _quote_sequence(client_id: str) -> int | None:
    parts = client_id.split("-")
    if len(parts) != 3 or parts[0] != "quote":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _build_rounds(events: list[dict[str, Any]]) -> tuple[list[RoundRecord], float | None, float | None]:
    submissions: dict[str, dict[str, Any]] = {}
    submit_ats: dict[str, float] = {}
    fills: list[Fill] = []
    seen_trade_ids: set[str] = set()
    pricetick: float | None = None
    size: float | None = None
    for record in events:
        at = float(record["at"])
        event_type = record["event"]["type"]
        data = record["event"]["data"]
        for action in record["actions"]:
            if _action_kind(action) != "submit_order":
                continue
            payload = _action_payload(action)
            action_client = payload.get("client_id")
            if action_client:
                previous = submit_ats.get(action_client)
                if previous is None or at < previous:
                    submit_ats[action_client] = at
        if event_type == "ContractEvent":
            pricetick = data["pricetick"]
            size = data.get("size")
        elif event_type == "OrderEvent":
            client_id = data.get("client_id")
            if client_id:
                previous = submit_ats.get(client_id)
                if previous is None or at < previous:
                    submit_ats[client_id] = at
                existing = submissions.get(client_id)
                new_time = data.get("exchange_time")
                # 同一委托的录入确认回报可能比撮合回报晚 1 秒：挂单时刻取全部回报中最早的时间。
                if (
                    existing is None
                    or (new_time and (not existing.get("exchange_time") or new_time < existing["exchange_time"]))
                ):
                    submissions[client_id] = data
        elif event_type == "TradeEvent":
            client_id = data.get("client_id")
            if not client_id:
                continue
            trade_id = str(data.get("trade_id") or "")
            if trade_id and trade_id in seen_trade_ids:
                continue
            if trade_id:
                seen_trade_ids.add(trade_id)
            fills.append(
                Fill(
                    client_id=client_id,
                    side=data["side"],
                    volume=int(data["volume"]),
                    price=float(data["price"]),
                    exchange_time=data["exchange_time"],
                    at=at,
                )
            )
    rounds: list[RoundRecord] = []
    current: RoundRecord | None = None
    current_sequence: int | None = None
    for fill in fills:
        if fill.client_id.startswith("quote-"):
            sequence = _quote_sequence(fill.client_id)
            # 轮次边界：上一轮已有平仓，或成交来自另一对挂单（新报价对/迟到成交）。
            if current is not None and (
                current.closes
                or (sequence is not None and current_sequence is not None and sequence != current_sequence)
            ):
                current = None
            if current is None:
                submission = submissions.get(fill.client_id, {})
                current = RoundRecord(
                    side=fill.side,
                    submit_time=submission.get("exchange_time"),
                    submit_price=submission.get("price"),
                    submit_volume=submission.get("volume"),
                    submit_at=submit_ats.get(fill.client_id),
                )
                current_sequence = sequence
                rounds.append(current)
            current.opens.append(fill)
        elif fill.client_id.startswith("flatten-"):
            if current is None:
                current = RoundRecord(
                    side=fill.side,
                    submit_time=None,
                    submit_price=None,
                    submit_volume=None,
                    submit_at=submit_ats.get(fill.client_id),
                )
                rounds.append(current)
            current.closes.append(fill)
    return rounds, pricetick, size


def _run_overview_rows(run_summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        {
            "contract": f"{entry.get('target_symbol')}@{entry.get('target_exchange')}",
            "terminal_state": entry.get("terminal_state"),
            "round_trips": entry.get("round_trips"),
            "failure_reason": entry.get("failure_reason"),
            "stop_reason": entry.get("stop_reason"),
        }
        for entry in run_summary.get("contracts", [])
        if entry.get("target_symbol")
    ]
    if not rows and run_summary.get("summary_missing"):
        rows = [
            {
                "contract": "—",
                "terminal_state": "CRASHED",
                "round_trips": None,
                "failure_reason": "summary.json 缺失（进程未正常收尾）",
                "stop_reason": None,
            }
        ]
    return rows


def _action_kind(action: Any) -> str | None:
    data = action.get("data")
    if isinstance(data, dict):
        return data.get("kind")
    return action.get("kind")


def _build_funds(
    run: RunFacts,
    snapshots: list[dict[str, Any]],
) -> RunFunds | None:
    """资金差净盈亏窗口：首个委托前与最后平仓终态后的资金快照。"""
    if not snapshots:
        return None
    first_submit_at: float | None = None
    last_flatten_terminal_at: float | None = None
    has_fill = False
    for events in run.contract_events.values():
        for record in events:
            at = float(record["at"])
            data = record["event"]["data"]
            if record["event"]["type"] == "TradeEvent" and data.get("client_id"):
                has_fill = True
            if any(_action_kind(action) == "submit_order" for action in record["actions"]):
                if first_submit_at is None or at < first_submit_at:
                    first_submit_at = at
            if any(trace.get("code") == "round_finished" for trace in record.get("trace", []) if isinstance(trace, dict)):
                if last_flatten_terminal_at is None or at > last_flatten_terminal_at:
                    last_flatten_terminal_at = at
            if (
                record["event"]["type"] == "OrderEvent"
                and (data.get("client_id") or "").startswith("flatten-")
                and data["status"] in {"ALLTRADED", "CANCELLED", "REJECTED"}
            ):
                if last_flatten_terminal_at is None or at > last_flatten_terminal_at:
                    last_flatten_terminal_at = at
    if not has_fill or first_submit_at is None or last_flatten_terminal_at is None:
        return None
    before = [snap for snap in snapshots if snap["at"] <= first_submit_at]
    after = [snap for snap in snapshots if snap["at"] >= last_flatten_terminal_at]
    boundary_note = not before or not after
    start = before[-1]["balance"] if before else snapshots[0]["balance"]
    end = after[0]["balance"] if after else snapshots[-1]["balance"]
    return RunFunds(directory=run.directory, start_balance=start, end_balance=end, boundary_note=boundary_note)


def build_days(audit_root: str | Path) -> tuple[dict[str, DayModel], int]:
    """Group every timed run in the audit root into per-trading-day models."""
    root = Path(audit_root)
    days: dict[str, DayModel] = {}
    skipped = 0
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        run = _load_run(run_dir)
        if run is None:
            continue
        day = _run_day(run)
        if day is None:
            skipped += 1
            continue
        model = days.setdefault(day, DayModel(day=day))
        run_gross: float | None = None
        run_gross_unknown = False
        for contract, events in sorted(run.contract_events.items()):
            rounds, pricetick, size = _build_rounds(events)
            if not rounds:
                continue
            existing = next((entry for entry in model.contracts if entry.contract == contract), None)
            if existing is None:
                existing = ContractDay(contract=contract, pricetick=pricetick, size=size)
                model.contracts.append(existing)
            existing.rounds.extend(rounds)
            for record in rounds:
                cash = _gross_pnl(record, existing)[2]
                if cash is None:
                    run_gross_unknown = True
                else:
                    run_gross = (run_gross or 0.0) + cash
        model.runs.append(
            RunInfo(
                directory=run.directory,
                rows=_run_overview_rows(run.run_summary),
                gross_pnl=None if run_gross_unknown else run_gross,
            )
        )
        account_file = run_dir / "account.jsonl"
        if account_file.exists():
            snapshots = [
                json.loads(line)
                for line in account_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            model.account_snapshots[run.directory] = snapshots
            funds = _build_funds(run, snapshots)
            if funds is not None:
                model.funds.append(funds)
    return days, skipped


def _time_of_day(exchange_time: str | None) -> str:
    if not exchange_time:
        return "—"
    return exchange_time[11:19]


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:g}"


def _seconds(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}s"


def _signed(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+g}"


def _gross_pnl(record: RoundRecord, contract: ContractDay) -> tuple[float | None, float | None, float | None]:
    """Signed price diff, tick count, and gross cash PnL for one round.

    Rounds with mixed opening sides or unmatched open/close volumes have no
    well-defined gross PnL and render as em-dashes. 价差完成轮按两侧成交均价计价差。
    """
    if not record.opens:
        return None, None, None
    if record.closes:
        if len({fill.side for fill in record.opens}) > 1 or record.open_volume != record.close_volume:
            return None, None, None
        diff = record.close_avg_price - record.open_avg_price
    else:
        buys = [fill for fill in record.opens if fill.side == "BUY"]
        sells = [fill for fill in record.opens if fill.side == "SELL"]
        if not buys or not sells:
            return None, None, None
        matched = min(sum(f.volume for f in buys), sum(f.volume for f in sells))
        if matched <= 0:
            return None, None, None
        buy_avg = _weighted_price(buys)
        sell_avg = _weighted_price(sells)
        diff = sell_avg - buy_avg
        if contract.size:
            return diff, diff / contract.pricetick if contract.pricetick else None, diff * matched * contract.size
        return diff, diff / contract.pricetick if contract.pricetick else None, None
    if record.side == "SELL":
        diff = -diff
    ticks = diff / contract.pricetick if contract.pricetick else None
    cash = diff * record.open_volume * contract.size if contract.size else None
    return diff, ticks, cash


def _render_overview(model: DayModel) -> list[str]:
    rows: list[str] = ["<h2>当日 run 总览</h2>"]
    rows.append(
        "<table><tr><th>run</th><th>合约</th><th>终态</th><th>轮数</th><th>失败原因</th><th>停止原因</th></tr>"
    )
    for run in model.runs:
        for index, row in enumerate(run.rows):
            rows.append(
                "<tr>"
                f"<td>{escape(run.directory) if index == 0 else ''}</td>"
                f"<td>{escape(str(row['contract']))}</td>"
                f"<td>{escape(str(row['terminal_state']))}</td>"
                f"<td>{row['round_trips'] if row['round_trips'] is not None else '—'}</td>"
                f"<td>{escape(str(row['failure_reason'] or '—'))}</td>"
                f"<td>{escape(str(row['stop_reason'] or '—'))}</td>"
                "</tr>"
            )
    rows.append("</table>")
    return rows


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _render_funds(model: DayModel) -> list[str]:
    rows: list[str] = ["<h2>资金汇总</h2>"]
    if not model.funds:
        rows.append("<p class=\"note\">无资金快照（该交易日没有可计算资金差的成交 run）。</p>")
        return rows
    rows.append(
        "<table><tr><th>run</th><th>起始资金</th><th>结束资金</th><th>真实净盈亏</th><th>备注</th></tr>"
    )
    for funds in model.funds:
        note = "边界快照缺失，取最近快照" if funds.boundary_note else ""
        rows.append(
            "<tr>"
            f"<td>{escape(funds.directory)}</td>"
            f"<td>{_money(funds.start_balance)}</td>"
            f"<td>{_money(funds.end_balance)}</td>"
            f"<td>{_money(funds.net)}</td>"
            f"<td>{escape(note)}</td>"
            "</tr>"
        )
    day_net = sum(funds.net for funds in model.funds)
    # 推算手续费只聚合有资金快照的 run：混入无快照 run 的毛盈亏会污染口径。
    funded_directories = {funds.directory for funds in model.funds}
    day_gross: float | None = None
    for info in model.runs:
        if info.directory not in funded_directories:
            continue
        if info.gross_pnl is None:
            day_gross = None
            break
        day_gross = (day_gross or 0.0) + info.gross_pnl
    implied_fees = day_gross - day_net if day_gross is not None else None
    rows.append(
        "<tr>"
        "<td>日合计</td>"
        f"<td>{_money(model.funds[0].start_balance)}</td>"
        f"<td>{_money(model.funds[-1].end_balance)}</td>"
        f"<td>{_money(day_net)}</td>"
        f"<td>推算手续费 {_money(implied_fees)}</td>"
        "</tr>"
    )
    rows.append("</table>")
    rows.append(
        "<p class=\"note\">真实净盈亏已含手续费（账户资金差）；手续费为推算值（Σ毛盈亏 − 资金差）。"
        "资金差只在账户仅运行本策略时才等于策略净盈亏——账户内其他仓位的浮动盈亏会直接混入该数字。</p>"
    )
    return rows


def render_html(model: DayModel) -> str:
    sections: list[str] = _render_overview(model)
    sections.extend(_render_funds(model))
    for contract in model.contracts:
        sections.append(f"<h2>{escape(contract.contract)}</h2>")
        sections.append(
            "<table><tr><th>#</th><th>方向</th><th>挂单时刻</th><th>挂单价</th><th>挂单量</th>"
            "<th>成交时刻</th><th>成交价</th><th>成交量</th>"
            "<th>平仓时刻</th><th>平仓价</th><th>平仓量</th>"
            "<th>挂单→成交</th><th>成交→平仓</th><th>结束方式</th><th>价差</th><th>tick 数</th><th>毛盈亏</th></tr>"
        )
        for index, record in enumerate(contract.rounds, 1):
            open_times = _time_of_day(record.open_first_time)
            if record.open_last_time != record.open_first_time:
                open_times += f" → {_time_of_day(record.open_last_time)}"
            close_times = _time_of_day(record.close_first_time)
            if record.close_last_time != record.close_first_time:
                close_times += f" → {_time_of_day(record.close_last_time)}"
            diff, ticks, cash = _gross_pnl(record, contract)
            sections.append(
                "<tr>"
                f"<td>{index}</td><td>{escape(record.side)}</td>"
                f"<td>{escape(_time_of_day(record.submit_time))}</td>"
                f"<td>{_price(record.submit_price)}</td><td>{record.submit_volume if record.submit_volume is not None else '—'}</td>"
                f"<td>{escape(open_times)}</td><td>{_price(record.open_avg_price)}</td><td>{record.open_volume}</td>"
                f"<td>{escape(close_times)}</td><td>{_price(record.close_avg_price)}</td><td>{record.close_volume}</td>"
                f"<td>{_seconds(record.wait_seconds)}</td><td>{_seconds(record.hold_seconds)}</td>"
                f"<td>{escape(record.ending)}</td>"
                f"<td>{_signed(diff)}</td><td>{_signed(ticks)}</td><td>{_signed(cash)}</td>"
                "</tr>"
            )
        sections.append("</table>")
    body = "\n".join(sections)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>交易日成交明细 {escape(model.day)}</title>"
        "<style>"
        "body{font-family:-apple-system,'PingFang SC',sans-serif;margin:24px;color:#222}"
        "h1{font-size:20px}h2{font-size:16px;margin-top:28px}"
        "table{border-collapse:collapse;margin-top:8px}"
        "th,td{border:1px solid #ccc;padding:4px 10px;text-align:right;font-variant-numeric:tabular-nums}"
        "th{background:#f5f5f5}td:nth-child(2){text-align:center}"
        ".note{color:#666;font-size:13px;margin-top:6px}"
        "</style></head><body>"
        f"<h1>交易日成交明细 · {escape(model.day)}</h1>"
        "<p class=\"note\">毛盈亏未含手续费，按成交价与合约乘数计算。"
        "时刻列为交易所秒级时间戳（SimNow 可能带 ±1 秒抖动）；两个间隔列按审计单调钟毫秒精度计算。</p>"
        f"{body}"
        "</body></html>"
    )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunReportError(f"{label} 读取失败: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunReportError(f"{label} 必须是 JSON 对象: {path}")
    return value


def _check_audit_schema(effective: dict[str, Any], path: Path) -> None:
    if effective.get("audit_schema_version") != AUDIT_SCHEMA_VERSION:
        raise RunReportError(f"不支持的审计格式: {path}")


def _action_payload(action: dict[str, Any]) -> dict[str, Any]:
    data = action.get("data")
    if isinstance(data, dict):
        payload = data.get("payload")
        if isinstance(payload, dict):
            return payload
    payload = action.get("payload")
    return payload if isinstance(payload, dict) else {}


_ORDER_TERMINAL_STATUSES = {"ALLTRADED", "CANCELLED", "REJECTED"}


def _trace_client_ids(trace: dict[str, Any]) -> list[str]:
    client_ids = trace.get("client_ids")
    if isinstance(client_ids, list):
        return [str(client_id) for client_id in client_ids if client_id]
    client_id = trace.get("client_id")
    return [str(client_id)] if client_id else []


def _run_contract_from_events(events_file: Path, events: list[dict[str, Any]]) -> RunContractReport:
    contract_name = events_file.parent.name
    symbol, _, exchange = contract_name.partition("@")
    product_code: str | None = None
    pricetick: float | None = None
    size: float | None = None
    orders: dict[str, RunOrder] = {}
    timeline: list[dict[str, Any]] = []

    def order_for(client_id: str) -> RunOrder:
        return orders.setdefault(client_id, RunOrder(client_id=client_id))

    for record in events:
        try:
            at = float(record["at"])
            event = record["event"]
            event_type = event["type"]
            data = event.get("data") or {}
            actions = record.get("actions") or []
        except (KeyError, TypeError, ValueError) as exc:
            raise RunReportError(f"事件格式错误: {events_file}") from exc

        if event_type == "ContractEvent":
            symbol = str(data.get("symbol") or symbol)
            exchange = str(data.get("exchange") or exchange)
            product_code = data.get("product_code") or product_code
            pricetick = data.get("pricetick")
            size = data.get("size")

        record_traces = record.get("trace") or []
        for trace in record_traces:
            if not isinstance(trace, dict):
                raise RunReportError(f"因果轨迹格式错误: {events_file}")
            client_ids = _trace_client_ids(trace)
            if client_ids:
                for client_id in client_ids:
                    order_for(client_id).traces.append(trace)
                replacement = trace.get("replacement")
                if isinstance(replacement, dict):
                    for previous_id in replacement.get("previous_client_ids", []):
                        previous = order_for(str(previous_id))
                        for successor_id in client_ids:
                            if successor_id not in previous.successor_client_ids:
                                previous.successor_client_ids.append(successor_id)
            else:
                timeline.append({"at": at, **trace})

        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = _action_kind(action)
            payload = _action_payload(action)
            client_id = payload.get("client_id")
            if not client_id:
                continue
            order = order_for(str(client_id))
            order.actions.append({"at": at, "kind": kind, "payload": payload})
            if kind == "submit_order":
                if order.submit_at is None or at < order.submit_at:
                    order.submit_at = at
                if not order.payload:
                    order.payload = dict(payload)

        if event_type == "OrderEvent":
            client_id = data.get("client_id")
            if client_id:
                order = order_for(str(client_id))
                order.order_id = data.get("order_id") or order.order_id
                status = data.get("status")
                if status and status not in order.statuses:
                    order.statuses.append(str(status))
                if status:
                    if status in _ORDER_TERMINAL_STATUSES:
                        if order.final_status not in _ORDER_TERMINAL_STATUSES:
                            order.final_status = str(status)
                    elif order.final_status not in _ORDER_TERMINAL_STATUSES:
                        order.final_status = str(status)
                order.traded = max(order.traded, int(data.get("traded") or 0))
                if status:
                    order.status_events.append(
                        {
                            "at": at,
                            "status": str(status),
                            "traded": int(data.get("traded") or 0),
                            "order_id": data.get("order_id"),
                            "exchange_time": data.get("exchange_time"),
                        }
                    )
        elif event_type == "TradeEvent":
            client_id = data.get("client_id")
            if client_id:
                trade_id = str(data.get("trade_id") or "")
                if trade_id and trade_id in order_for(str(client_id)).trade_ids:
                    continue
                order = order_for(str(client_id))
                if trade_id:
                    order.trade_ids.add(trade_id)
                order.trades.append(dict(data))
                order.trade_volume += int(data.get("volume") or 0)
                order.traded = max(order.traded, order.trade_volume)

    rounds, round_pricetick, round_size = _build_rounds(events)
    return RunContractReport(
        contract=f"{symbol}@{exchange}",
        symbol=symbol,
        exchange=exchange,
        product_code=product_code,
        pricetick=pricetick if pricetick is not None else round_pricetick,
        size=size if size is not None else round_size,
        orders=sorted(orders.values(), key=lambda order: (order.submit_at is None, order.submit_at or 0, order.client_id)),
        timeline=sorted(timeline, key=lambda item: item["at"]),
        rounds=rounds,
    )


def build_run_report(run_dir: str | Path) -> RunReport:
    directory = Path(run_dir)
    if not directory.is_dir():
        raise RunReportError(f"run 目录不存在: {directory}")
    effective_doc = _read_json(directory / "effective_strategy.json", "run 生效策略")
    _assert_report_safe(effective_doc, directory / "effective_strategy.json")
    _check_audit_schema(effective_doc, directory / "effective_strategy.json")
    run_effective = _validate_effective_payload(
        effective_doc.get("effective"), directory / "effective_strategy.json", run_level=True
    )
    run_hash = _validate_strategy_hash(run_effective, effective_doc.get("sha256"), directory / "effective_strategy.json")
    summary = _read_json(directory / "summary.json", "run 摘要")
    _assert_report_safe(summary, directory / "summary.json")
    summary_contracts = summary.get("contracts")
    if not isinstance(summary_contracts, list) or not summary_contracts:
        raise RunReportError(f"run 摘要缺少合约终态: {directory / 'summary.json'}")
    summary_rows: dict[str, dict[str, Any]] = {}
    for entry in summary_contracts:
        if not isinstance(entry, dict):
            raise RunReportError(f"run 摘要包含无效合约项: {directory / 'summary.json'}")
        symbol = entry.get("target_symbol")
        exchange = entry.get("target_exchange")
        if not isinstance(symbol, str) or not symbol or not isinstance(exchange, str) or not exchange:
            raise RunReportError(f"run 摘要包含缺少合约身份的项: {directory / 'summary.json'}")
        key = f"{symbol}@{exchange}"
        if key in summary_rows:
            raise RunReportError(f"run 摘要包含重复合约: {key}")
        summary_rows[key] = entry
    if any(entry.get("terminal_state") not in {"FINISHED", "FAILED"} for entry in summary_rows.values()):
        raise RunReportError("run 尚未进入完整终态，拒绝生成报告")

    contracts: list[RunContractReport] = []
    event_inputs: list[tuple[Path, list[dict[str, Any]]]] = []
    event_files = sorted(directory.glob("*/events.jsonl"))
    if not event_files:
        raise RunReportError(f"run 缺少合约事件日志: {directory}")
    for events_file in event_files:
        contract_effective = _read_json(events_file.parent / "effective_strategy.json", "合约生效策略")
        _assert_report_safe(contract_effective, events_file.parent / "effective_strategy.json")
        _check_audit_schema(contract_effective, events_file.parent / "effective_strategy.json")
        _validate_effective_payload(
            contract_effective.get("effective"), events_file.parent / "effective_strategy.json", run_level=False
        )
        if contract_effective.get("sha256") != run_hash:
            raise RunReportError(f"合约生效策略与 run 策略哈希不一致: {events_file.parent / 'effective_strategy.json'}")
        contract_summary = _read_json(events_file.parent / "summary.json", "合约摘要")
        _assert_report_safe(contract_summary, events_file.parent / "summary.json")
        if contract_summary.get("terminal_state") not in {"FINISHED", "FAILED"}:
            raise RunReportError(f"合约尚未进入完整终态: {events_file.parent / 'summary.json'}")
        try:
            events = [
                json.loads(line)
                for line in events_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise RunReportError(f"事件日志读取失败: {events_file}: {exc}") from exc
        if not events:
            raise RunReportError(f"合约事件日志为空: {events_file}")
        _assert_report_safe(events, events_file)
        contract = _run_contract_from_events(events_file, events)
        if (
            not contract.symbol
            or not contract.exchange
            or not isinstance(contract.product_code, str)
            or not contract.product_code.strip()
            or not isinstance(contract.pricetick, (int, float))
            or not isfinite(contract.pricetick)
            or contract.pricetick <= 0
            or not isinstance(contract.size, (int, float))
            or not isfinite(contract.size)
            or contract.size <= 0
        ):
            raise RunReportError(f"合约事件缺少必要审计身份或 pricetick: {events_file}")
        if contract.contract not in summary_rows:
            raise RunReportError(f"合约缺少 run 摘要终态: {contract.contract}")
        contracts.append(contract)
        event_inputs.append((events_file, events))

    if not any(record.get("trace") for _, events in event_inputs for record in events):
        raise RunReportError("run 缺少结构化因果轨迹，拒绝按当前代码推导历史原因")
    event_contracts = {contract.contract for contract in contracts}
    if event_contracts != set(summary_rows):
        raise RunReportError("run 摘要与合约事件日志不一致，拒绝生成报告")

    account_file = directory / "account.jsonl"
    snapshots: list[dict[str, Any]] = []
    if account_file.exists():
        try:
            snapshots = [
                json.loads(line)
                for line in account_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError) as exc:
            raise RunReportError(f"资金快照读取失败: {account_file}: {exc}") from exc
        _assert_report_safe(snapshots, account_file)
        for snapshot in snapshots:
            if (
                not isinstance(snapshot, dict)
                or not isinstance(snapshot.get("at"), (int, float))
                or not isinstance(snapshot.get("balance"), (int, float))
                or not isfinite(float(snapshot["at"]))
                or not isfinite(float(snapshot["balance"]))
            ):
                raise RunReportError(f"资金快照缺少有效 at/balance: {account_file}")
    run_facts = RunFacts(directory.name, {path.parent.name: events for path, events in event_inputs}, summary)
    funds = _build_funds(run_facts, snapshots)
    gross_pnl: float | None = 0.0
    gross_unknown = False
    for contract in contracts:
        contract_day = ContractDay(contract.contract, contract.pricetick, contract.size)
        for round_record in contract.rounds:
            cash = _gross_pnl(round_record, contract_day)[2]
            if cash is None:
                gross_unknown = True
            else:
                gross_pnl += cash
    if gross_unknown:
        gross_pnl = None
    return RunReport(
        directory=directory.name,
        effective=effective_doc.get("effective") or {},
        strategy_hash=run_hash,
        summary=summary,
        contracts=contracts,
        funds=funds,
        account_snapshot_count=len(snapshots),
        gross_pnl=gross_pnl if contracts else None,
    )


def _trace_label(code: str | None) -> str:
    return {
        "contract_metadata": "合约元数据",
        "startup_position_query": "启动查仓",
        "startup_position_result": "启动查仓结果",
        "startup_position_rejected": "启动零仓门槛失败",
        "zero_position_confirmed": "确认零仓",
        "stable_quote_qualified": "稳定行情达标",
        "order_status": "委托状态变化",
        "quote_submitted": "首次报价",
        "market_pause": "盘口保护失败，暂停报价",
        "reanchor": "价格越界，重定锚",
        "quote_stale": "行情超时，安全撤换报价",
        "invalid_market_time": "行情时间异常，安全撤换报价",
        "replacement_ready": "旧报价已终态，重新获得报价资格",
        "replacement_delayed": "等待旧报价终态",
        "normal_action_limit": "报撤动作限制",
        "first_fill": "首次成交，进入价差窗口",
        "window_fill": "窗口内追加成交",
        "opposite_fill": "对侧成交，结束价差窗口",
        "spread_complete": "价差完成",
        "closing_started": "进入收口",
        "window_timeout": "价差窗口超时",
        "flatten_submitted": "提交 FAK 平仓",
        "flatten_fill": "FAK 平仓成交",
        "flatten_rejected": "FAK 平仓拒单",
        "flatten_terminal": "FAK 委托终态",
        "remaining_cancel": "撤销剩余委托",
        "closing_position_query": "收口查仓",
        "closing_position_result": "收口查仓结果",
        "round_finished": "本轮完成",
        "round_failed": "本轮失败",
        "failure": "安全失败",
        "late_opening_fill": "迟到开仓成交",
        "late_opening_fill_after_finish": "终态后迟到开仓成交",
        "late_flatten_fill": "迟到平仓成交",
        "cancel_timeout": "撤单超时",
        "flatten_timeout": "平仓超时",
        "interrupt": "操作员中断",
        "session_end": "到达会话结束时间",
    }.get(code or "", code or "因果记录")


def _order_anchor(client_id: str) -> str:
    return "order-" + "".join(char if char.isalnum() or char in "_-" else "_" for char in client_id)


def _order_link(client_id: str) -> str:
    label = escape(client_id)
    return f'<a href="#{_order_anchor(client_id)}">{label}</a>'


def _order_purpose(client_id: str, payload: dict[str, Any]) -> str:
    if client_id.startswith("flatten-") or payload.get("order_type") == "FAK":
        return "FAK 平仓"
    if client_id.startswith("quote-"):
        return "网格报价"
    return "其他委托"


_ORDER_STATUS_ZH = {
    "SUBMITTING": "提交中",
    "NOTTRADED": "未成交",
    "PARTTRADED": "部分成交",
    "ALLTRADED": "全部成交",
    "CANCELLED": "已撤销",
    "REJECTED": "已拒单",
}
_SIDE_ZH = {"BUY": "买", "SELL": "卖"}
_OFFSET_ZH = {
    "OPEN": "开仓",
    "CLOSE": "平仓",
    "CLOSETODAY": "平今",
    "CLOSEYESTERDAY": "平昨",
}
_ORDER_TYPE_ZH = {"LIMIT": "限价", "FAK": "FAK", "FOK": "FOK", "MARKET": "市价"}
_TERMINAL_STATE_ZH = {"FINISHED": "正常结束", "FAILED": "失败", "CRASHED": "异常退出"}
_ACTION_KIND_ZH = {"submit_order": "提交委托", "cancel_order": "撤单"}
_REASON_ZH = {
    "max_round_trips": "达到配置的最大完成轮数后正常停止",
    "quote_window_end": "到达当前报价窗口结束时刻，收市前安全收口",
    "session_end": "到达会话结束时间，按中断链路收口",
    "interrupted": "操作员手动中断本 run",
    "nonzero_startup_position": "启动查仓发现目标合约非零仓，拒绝开报",
    "flatten_rejected": "受限 FAK 平仓被拒单，收口失败",
    "flatten_timeout": "受限 FAK 平仓超时未终态，收口失败",
    "cancel_timeout": "撤单超过时限仍未收到终态回报",
    "interrupted_before_zero_position": "操作员中断时净仓尚未归零",
    "late_opening_fill_after_finish": "终态后仍收到迟到开仓成交，触发风险收口",
    "late_flatten_fill_after_finish": "终态后仍收到迟到平仓成交，触发风险收口",
    "confirmation_required": "未加 --confirm-simnow，入口拒绝具备下单能力",
    "market_pause": "盘口保护失败（价差过宽或盘口无效），安全撤销双边报价并暂停，待行情重新达标后再挂",
    "reanchor": "最新价持续越出网格带，确认后撤旧单并重定锚点，再走稳定行情门槛重新报价",
    "quote_stale": "报价期间最新有效 Tick 年龄超过该合约 max_tick_age_seconds，视为行情超时，安全撤单后重新等待稳定行情再挂",
    "invalid_market_time": "行情时间无效或相对本地时钟异常，安全撤单并等待有效行情后再挂",
    "spread_complete": "对侧成交使价差窗口完成，撤销剩余开仓委托",
    "window_timeout": "价差窗口超时，净仓未平，转入受限 FAK 收口",
    "window_net_zero": "价差窗口内净仓已归零，结束本轮",
    "window_fill": "价差窗口内又发生同向追加成交",
    "opposite_fill": "对侧报价成交，结束价差窗口",
    "additional_fill": "窗口内追加成交",
}


def _display_zh(value: Any, mapping: dict[str, str]) -> str:
    if value is None or value == "":
        return "—"
    text = str(value)
    return mapping.get(text, text)


def _status_path_zh(statuses: list[str]) -> str:
    if not statuses:
        return "—"
    return " → ".join(_display_zh(status, _ORDER_STATUS_ZH) for status in statuses)


def _reason_zh(value: Any) -> str:
    return _display_zh(value, _REASON_ZH)


def _render_raw_block(title: str, value: Any) -> str:
    return (
        f"<details><summary>{escape(title)}</summary>"
        f"<pre>{escape(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))}</pre>"
        "</details>"
    )


def _fak_cancel_note(order: "RunOrder") -> str | None:
    """Explain CANCELLED FAK flatten using order type + whether a cancel action exists."""
    if order.final_status != "CANCELLED":
        return None
    payload = order.payload or {}
    is_fak = payload.get("order_type") == "FAK" or order.client_id.startswith("flatten-")
    if not is_fak:
        return None
    strategy_cancel = any(item.get("kind") == "cancel_order" for item in order.actions)
    if strategy_cancel:
        return (
            "撤销说明：审计中存在策略撤单动作；在途 FAK 被主动撤销"
            "（常见于净仓在平仓过程中被对侧成交翻向等情况），再按最新净仓决定是否重报。"
        )
    if order.traded == 0:
        return (
            "撤销说明：本单为 FAK 平仓且完全未成交；交易所按 Fill-And-Kill 规则"
            "即时撤销未成交部分，不是策略主动撤单。若净仓仍未归零，会在不利上限内继续重报。"
        )
    return (
        f"撤销说明：本单为 FAK 平仓，已成交 {order.traded} 手，"
        "剩余未成交部分由交易所按 FAK 规则即时撤销，不是策略主动撤单。"
    )


def _render_run_trace(trace: dict[str, Any]) -> str:
    label = _trace_label(trace.get("code"))
    if trace.get("code") == "quote_submitted" and isinstance(trace.get("replacement"), dict):
        label = "替代报价提交"
    parts = [f"<strong>{escape(label)}</strong>"]
    calculation = trace.get("calculation") or {}
    code = trace.get("code")
    if code == "quote_submitted" and calculation:
        parts.append(
            "<div>报价计算："
            f"锚点 {escape(_price(calculation.get('anchor_price')))}，"
            f"距离 {escape(_price(calculation.get('distance_ticks')))} tick；"
            f"买价 {escape(_price(calculation.get('buy_price')))}，"
            f"卖价 {escape(_price(calculation.get('sell_price')))}。</div>"
        )
    elif code == "market_pause" and calculation:
        parts.append(
            "<div>盘口保护未通过："
            f"报价距离 {escape(_price(calculation.get('distance_ticks')))} tick "
            f"未严格大于保护倍数 {escape(_price(calculation.get('protection_multiple')))} × "
            f"盘口价差 {escape(_price(calculation.get('spread_ticks')))} tick，"
            "因此安全撤销当前报价并暂停。</div>"
        )
    elif code == "reanchor" and calculation:
        parts.append(
            "<div>价格越带触发重定锚："
            f"锚点 {escape(_price(calculation.get('old_anchor_ticks')))} → "
            f"{escape(_price(calculation.get('new_anchor_ticks')))} tick，"
            f"越界已持续 {escape(_seconds(calculation.get('elapsed_seconds')))} "
            f"（确认阈值 {escape(_seconds(calculation.get('confirmation_seconds')))}）。</div>"
        )
    elif code == "quote_stale" and calculation:
        parts.append(
            "<div>行情超时："
            f"最新有效 Tick 年龄 {escape(_seconds(calculation.get('age_seconds')))}，"
            f"超过本合约静默阈值 {escape(_seconds(calculation.get('max_tick_age_seconds')))}，"
            "安全撤销当前双边报价，恢复后须重新走稳定行情门槛。</div>"
        )
    elif code == "invalid_market_time" and calculation:
        parts.append(
            "<div>行情时间异常："
            f"{escape(_reason_zh(calculation.get('reason')))}；"
            f"静默阈值 {escape(_seconds(calculation.get('max_tick_age_seconds')))}，"
            "安全撤单并等待有效行情。</div>"
        )
    elif code == "first_fill" and calculation:
        parts.append(
            "<div>成交 "
            f"{escape(_price(calculation.get('price')))} × {calculation.get('volume', '—')}，"
            f"进入 {escape(_seconds(calculation.get('window_seconds')))} 价差窗口，"
            f"结束时刻 {escape(_seconds(calculation.get('window_ends_at')))}。</div>"
        )
    elif code in {"window_fill", "opposite_fill", "spread_complete"} and calculation:
        parts.append(
            "<div>窗口净仓变化："
            f"成交 {escape(_price(calculation.get('price')))} × {calculation.get('volume', '—')}，"
            f"净仓 {calculation.get('net_position', '—')}，"
            f"原因 {escape(_reason_zh(calculation.get('reason')))}。</div>"
        )
    elif code == "window_timeout" and calculation:
        parts.append(
            "<div>窗口超时："
            f"{escape(_seconds(calculation.get('elapsed_seconds')))} / "
            f"{escape(_seconds(calculation.get('window_seconds')))}，"
            f"净仓 {calculation.get('net_position', '—')}，开始受限平仓。</div>"
        )
    elif code == "flatten_submitted" and calculation:
        parts.append(
            "<div>提交受限 FAK 平仓："
            f"方向 {escape(_display_zh(calculation.get('side'), _SIDE_ZH))}，"
            f"开平 {escape(_display_zh(calculation.get('offset'), _OFFSET_ZH))}，"
            f"数量 {calculation.get('volume', '—')}，"
            f"第 {calculation.get('reprice_attempt', '—')} 次尝试；"
            f"盘口可执行价 {escape(_price(calculation.get('market_executable_price')))}，"
            f"初始价 {escape(_price(calculation.get('initial_executable_price')))}，"
            f"不利上限 {escape(_price(calculation.get('adverse_price_limit')))}，"
            f"实际委托价 {escape(_price(calculation.get('actual_price')))}。"
            "FAK 规则下未成交部分会由交易所即时撤销。</div>"
        )
    elif code == "flatten_terminal" and calculation:
        status = calculation.get("status")
        traded = calculation.get("traded", 0)
        volume = calculation.get("volume", "—")
        if status == "ALLTRADED":
            parts.append(
                "<div>FAK 终态：全部成交 "
                f"{traded} / {volume}，本笔平仓完成。</div>"
            )
        elif status == "CANCELLED" and traded == 0:
            parts.append(
                "<div>FAK 撤销原因：本笔完全未成交，交易所按 Fill-And-Kill（FAK）规则"
                "即时撤销未成交部分；审计中无策略撤单动作。若净仓仍未归零，会话会在不利上限内继续重报。</div>"
            )
        elif status == "CANCELLED":
            parts.append(
                "<div>FAK 撤销原因：已成交 "
                f"{traded} / {volume}，剩余未成交部分由交易所按 FAK 规则即时撤销。</div>"
            )
        else:
            parts.append(
                "<div>FAK 终态："
                f"{escape(_display_zh(status, _ORDER_STATUS_ZH))}，"
                f"成交 {traded} / {volume}。</div>"
            )
    elif code == "flatten_rejected" and calculation:
        parts.append(
            "<div>FAK 拒单："
            f"CTP 拒绝本笔平仓（{escape(_reason_zh(calculation.get('reason')))}），"
            f"成交 {calculation.get('traded', 0)} / {calculation.get('volume', '—')}，收口失败。</div>"
        )
    elif code == "flatten_fill" and calculation:
        parts.append(
            "<div>平仓成交："
            f"{escape(_price(calculation.get('price')))} × {calculation.get('volume', '—')}，"
            f"净仓 {calculation.get('net_position_before', '—')} → "
            f"{calculation.get('net_position_after', '—')}。</div>"
        )
    elif code == "closing_position_result" and calculation:
        parts.append(
            "<div>查仓结果："
            f"净仓 {calculation.get('net_position', '—')}，"
            f"校验 {('通过' if calculation.get('passed') else '失败')}。</div>"
        )
    replacement = trace.get("replacement")
    if isinstance(replacement, dict):
        previous = replacement.get("previous_client_ids", [])
        previous_links = ", ".join(_order_link(str(value)) for value in previous)
        parts.append(
            "<div>替代前驱："
            f"{previous_links or '—'}；"
            f"替代原因：{escape(_reason_zh(replacement.get('reason')))}</div>"
        )
    raw_parts: list[str] = []
    for key, title in (("market", "当时行情"), ("calculation", "计算数据")):
        value = trace.get(key)
        if value is not None:
            raw_parts.append(
                f"<div><span class=\"label\">{escape(title)}</span>"
                f"<pre>{escape(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))}</pre></div>"
            )
    if raw_parts:
        parts.append("<details><summary>原始数据</summary>" + "".join(raw_parts) + "</details>")
    return "<li>" + "".join(parts) + "</li>"


def _render_run_order_details(order: RunOrder) -> str:
    details: list[str] = ["<details><summary>查看详情</summary>"]
    accepted_time = order.status_events[0].get("exchange_time") if order.status_events else None
    terminal_time = next(
        (
            item.get("exchange_time")
            for item in reversed(order.status_events)
            if item.get("status") in _ORDER_TERMINAL_STATUSES
        ),
        None,
    )
    details.append(
        "<p>CTP 委托号："
        f"{escape(str(order.order_id or '—'))}；"
        f"接受时间：{escape(str(accepted_time or '—'))}；"
        f"终态时间：{escape(str(terminal_time or '—'))}</p>"
    )
    cancel_note = _fak_cancel_note(order)
    if cancel_note:
        details.append(f"<p>{escape(cancel_note)}</p>")
    if order.successor_client_ids:
        details.append(
            "<p>后继报价："
            + ", ".join(_order_link(client_id) for client_id in order.successor_client_ids)
            + "</p>"
        )
    if order.actions or order.status_events:
        details.append("<h4>动作与状态</h4><ol class=\"trace\">")
        lifecycle: list[tuple[float, int, str]] = []
        for item in order.actions:
            payload = item.get("payload") or {}
            kind = _display_zh(item.get("kind"), _ACTION_KIND_ZH)
            action_line = f"动作 {escape(kind)}"
            if payload:
                action_line += "：" + _render_raw_block("动作参数", payload)
            lifecycle.append((float(item.get("at") or 0), 0, action_line))
        for item in order.status_events:
            lifecycle.append(
                (
                    float(item.get("at") or 0),
                    1,
                    f"状态 {escape(_display_zh(item.get('status'), _ORDER_STATUS_ZH))}，"
                    f"累计成交 {item.get('traded', 0)}；"
                    f"交易所时间 {escape(str(item.get('exchange_time') or '—'))}",
                )
            )
        for at, _, description in sorted(lifecycle, key=lambda row: (row[0], row[1])):
            details.append(f"<li><span class=\"label\">审计时钟 {escape(_seconds(at))}</span>{description}</li>")
        details.append("</ol>")
    if order.traces:
        details.append("<ol class=\"trace\">" + "".join(_render_run_trace(trace) for trace in order.traces) + "</ol>")
    if order.trades:
        details.append("<h4>成交</h4>")
        details.append(_render_raw_block("成交明细", order.trades))
    if not order.traces and not order.trades and not order.successor_client_ids:
        details.append("<p class=\"note\">暂无结构化详情。</p>")
    details.append("</details>")
    return "".join(details)


def _render_run_funds(model: RunReport) -> list[str]:
    rows = ["<h2>资金结果</h2>"]
    if model.funds is None:
        if model.account_snapshot_count:
            rows.append(
                f'<p class="note">已记录 {model.account_snapshot_count} 条资金快照，但缺少首个委托前或最后平仓终态后的边界快照，'
                "不伪造资金差净盈亏。</p>"
            )
        else:
            rows.append('<p class="note">本 run 没有资金快照；保留委托与成交事实，不显示资金数字。</p>')
    else:
        implied_fees = model.gross_pnl - model.funds.net if model.gross_pnl is not None else None
        note = "；边界快照缺失，取最近快照" if model.funds.boundary_note else ""
        rows.append(
            "<table><tr><th>起始资金</th><th>结束资金</th><th>资金差净盈亏</th>"
            "<th>轮次毛盈亏</th><th>推算手续费</th></tr><tr>"
            f"<td>{_money(model.funds.start_balance)}</td>"
            f"<td>{_money(model.funds.end_balance)}</td>"
            f"<td>{_money(model.funds.net)}</td>"
            f"<td>{_money(model.gross_pnl)}</td>"
            f"<td>{_money(implied_fees)}</td></tr></table>"
            f'<p class="note">推算手续费 = Σ轮次毛盈亏 − 资金差；不是 CTP 逐笔费用{escape(note)}。</p>'
        )
    return rows


def _render_run_parameters(effective: dict[str, Any]) -> str:
    rows = ["<table><tr><th>合约</th><th>网格半宽 W（tick）</th><th>额外挂单距离 D（tick）</th>"
            "<th>重定锚步长 S（tick）</th><th>最大完成轮数</th><th>每侧手数</th></tr>"]
    for entry in effective.get("contracts", [effective]):
        config = {**effective, **entry}  # 兼容历史根节点公共参数，不使用当前代码默认值。
        rows.append(
            "<tr><td>" + escape(f"{config['symbol']}@{config['exchange']}") + "</td>"
            + "".join(f"<td>{escape(str(config.get(key, '—')))}</td>"
                      for key in ("w_ticks", "d_ticks", "s_ticks", "max_round_trips", "target_lots"))
            + "</tr>"
        )
    rows.append("</table><details><summary>完整生效配置（审计原文）</summary>"
                f"<pre>{escape(json.dumps(effective, ensure_ascii=False, indent=2, sort_keys=True))}</pre></details>")
    return "".join(rows)


def render_run_html(model: RunReport) -> str:
    sections = [
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">",
        f"<title>单 run 委托成交报告 {escape(model.directory)}</title>",
        "<style>"
        "body{font-family:-apple-system,'PingFang SC',sans-serif;margin:24px;color:#222}"
        "h1{font-size:20px}h2{font-size:16px;margin-top:28px}h3{font-size:14px;margin-top:20px}"
        "table{border-collapse:collapse;margin-top:8px;width:100%}"
        "th,td{border:1px solid #ccc;padding:5px 8px;text-align:left;vertical-align:top;font-variant-numeric:tabular-nums}"
        "th{background:#f5f5f5}.note{color:#666;font-size:13px}.label{color:#666;margin-right:6px}"
        "code{white-space:pre-wrap}.trace{margin:8px 0;padding-left:24px}.trace li{margin:8px 0}"
        "pre{background:#f7f7f7;padding:8px;overflow:auto}summary{cursor:pointer;color:#065}"
        "</style></head><body>",
        f"<h1>单 run 委托成交报告 · {escape(model.directory)}</h1>",
        "<h2>运行参数</h2>",
        f"<p>策略哈希：<code>{escape(str(model.strategy_hash or '—'))}</code></p>",
        _render_run_parameters(model.effective),
        *_render_run_funds(model),
        "<h2>run 总览</h2>",
        "<table><tr><th>合约</th><th>终态</th><th>轮数</th><th>停止原因</th><th>失败原因</th>"
        "<th>最终净仓</th><th>活动委托</th></tr>",
    ]
    for entry in model.summary.get("contracts", []):
        if not isinstance(entry, dict):
            continue
        contract = f"{entry.get('target_symbol')}@{entry.get('target_exchange')}"
        sections.append(
            "<tr>"
            f"<td>{escape(contract)}</td>"
            f"<td>{escape(_display_zh(entry.get('terminal_state'), _TERMINAL_STATE_ZH))}</td>"
            f"<td>{entry.get('round_trips', '—')}</td>"
            f"<td>{escape(_reason_zh(entry.get('stop_reason')))}</td>"
            f"<td>{escape(_reason_zh(entry.get('failure_reason')))}</td>"
            f"<td>{escape(str(entry.get('final_net_position') if entry.get('final_net_position') is not None else '—'))}</td>"
            f"<td>{entry.get('active_order_count', '—')}</td>"
            "</tr>"
        )
    sections.append("</table>")

    for contract in model.contracts:
        sections.append(f"<h2>{escape(contract.contract)}</h2>")
        sections.append(
            f"<p>品种代码：{escape(contract.product_code or '—')}；准确合约：{escape(contract.symbol)}；"
            f"交易所：{escape(contract.exchange)}；"
            f"最小变动价位：{_price(contract.pricetick)}；合约乘数：{_price(contract.size)}</p>"
        )
        if contract.rounds:
            sections.append(
                "<h3>轮次结果（毛盈亏）</h3>"
                "<p class=\"note\">毛盈亏按成交价、数量、合约乘数计算；资金差净盈亏不分摊到单笔委托。</p>"
                "<table><tr><th>轮次</th><th>开仓</th><th>平仓</th><th>结束方式</th>"
                "<th>价差 tick</th><th>毛盈亏</th></tr>"
            )
            contract_day = ContractDay(
                contract=contract.contract,
                pricetick=contract.pricetick,
                size=contract.size,
            )
            for index, round_record in enumerate(contract.rounds, 1):
                _, ticks, cash = _gross_pnl(round_record, contract_day)
                sections.append(
                    "<tr>"
                    f"<td>{index}</td>"
                    f"<td>{escape(_price(round_record.open_avg_price))} × {round_record.open_volume}</td>"
                    f"<td>{escape(_price(round_record.close_avg_price))} × {round_record.close_volume}</td>"
                    f"<td>{escape(round_record.ending)}</td>"
                    f"<td>{escape(_price(ticks))}</td>"
                    f"<td>{escape(_money(cash))}</td>"
                    "</tr>"
                )
            sections.append("</table>")
        sections.append("<h3>逻辑委托</h3>")
        sections.append(
            "<table><tr><th>client identity</th><th>用途</th><th>方向</th><th>开平</th><th>类型</th>"
            "<th>价格</th><th>数量</th><th>提交审计时钟</th><th>状态路径</th><th>最终结果</th>"
            "<th>成交量</th><th>详情</th></tr>"
        )
        for order in contract.orders:
            payload = order.payload
            sections.append(
                f'<tr id="{_order_anchor(order.client_id)}">'
                f"<td>{escape(order.client_id)}</td>"
                f"<td>{escape(_order_purpose(order.client_id, payload))}</td>"
                f"<td>{escape(_display_zh(payload.get('side'), _SIDE_ZH))}</td>"
                f"<td>{escape(_display_zh(payload.get('offset'), _OFFSET_ZH))}</td>"
                f"<td>{escape(_display_zh(payload.get('order_type'), _ORDER_TYPE_ZH))}</td>"
                f"<td>{_price(payload.get('price'))}</td>"
                f"<td>{payload.get('volume', '—')}</td>"
                f"<td>{_seconds(order.submit_at)}</td>"
                f"<td>{escape(_status_path_zh(order.statuses))}</td>"
                f"<td>{escape(_display_zh(order.final_status, _ORDER_STATUS_ZH))}</td>"
                f"<td>{order.traded}</td>"
                f"<td>{_render_run_order_details(order)}</td>"
                "</tr>"
            )
        sections.append("</table>")

    sections.append("</body></html>")
    return "".join(sections)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按交易日生成成交明细 HTML 报告")
    parser.add_argument("--audit-dir", default="audit", help="审计根目录")
    parser.add_argument("--out-dir", default="reports", help="报告输出目录")
    parser.add_argument("--date", help="只生成指定交易日（YYYYMMDD）")
    parser.add_argument("--run-dir", help="只生成指定完整 run 的报告")
    parser.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    args = parser.parse_args(argv)

    if args.run_dir:
        try:
            model = build_run_report(args.run_dir)
        except RunReportError as exc:
            print(f"报告生成失败: {exc}", file=sys.stderr)
            return 2
        out_root = Path(args.out_dir)
        out_root.mkdir(parents=True, exist_ok=True)
        out_path = out_root / f"run-{Path(args.run_dir).name}.html"
        out_path.write_text(render_run_html(model), encoding="utf-8")
        print(f"run {model.directory} 报告已生成: {out_path}")
        if args.open:
            webbrowser.open(out_path.resolve().as_uri())
        return 0

    days, skipped = build_days(args.audit_dir)
    if args.date:
        wanted = f"{args.date[:4]}-{args.date[4:6]}-{args.date[6:8]}"
        days = {day: model for day, model in days.items() if day == wanted}

    if not days:
        print("没有可生成的交易日报告。", file=sys.stderr)
    out_root = Path(args.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    for day, model in sorted(days.items()):
        out_path = out_root / f"trades-{day.replace('-', '')}.html"
        out_path.write_text(render_html(model), encoding="utf-8")
        print(f"交易日 {day} 报告已生成: {out_path}")
        if args.open:
            webbrowser.open(out_path.resolve().as_uri())
    if skipped:
        print(f"跳过 {skipped} 个缺少交易所时间戳的 run。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
