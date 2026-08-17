#!/usr/bin/env python3
"""Offline daily trade report generator built from audit directories."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Fill:
    client_id: str
    side: str
    volume: int
    price: float
    exchange_time: str


@dataclass
class RoundRecord:
    """One round trip: the filled opening order's lifecycle through the flatten."""

    side: str
    submit_time: str | None
    submit_price: float | None
    submit_volume: int | None
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


def _run_day(run: RunFacts) -> str | None:
    """The run's exchange trading day: the date part of its earliest exchange time."""
    earliest: str | None = None
    for events in run.contract_events.values():
        for record in events:
            exchange_time = record["event"]["data"].get("exchange_time")
            if exchange_time and (earliest is None or exchange_time < earliest):
                earliest = exchange_time
    return earliest[:10] if earliest else None


def _build_rounds(events: list[dict[str, Any]]) -> tuple[list[RoundRecord], float | None, float | None]:
    submissions: dict[str, dict[str, Any]] = {}
    fills: list[Fill] = []
    pricetick: float | None = None
    size: float | None = None
    for record in events:
        event_type = record["event"]["type"]
        data = record["event"]["data"]
        if event_type == "ContractEvent":
            pricetick = data["pricetick"]
            size = data.get("size")
        elif event_type == "OrderEvent":
            client_id = data.get("client_id")
            if client_id:
                existing = submissions.get(client_id)
                # vnpy 本地先推一条无交易所时间的 SUBMITTING；以带 InsertTime 的回报为准。
                if existing is None or (not existing.get("exchange_time") and data.get("exchange_time")):
                    submissions[client_id] = data
        elif event_type == "TradeEvent":
            client_id = data.get("client_id")
            if not client_id:
                continue
            fills.append(
                Fill(
                    client_id=client_id,
                    side=data["side"],
                    volume=int(data["volume"]),
                    price=float(data["price"]),
                    exchange_time=data["exchange_time"],
                )
            )
    rounds: list[RoundRecord] = []
    current: RoundRecord | None = None
    for fill in fills:
        if fill.client_id.startswith("quote-"):
            if current is not None and current.closes:
                current = None
            if current is None:
                submission = submissions.get(fill.client_id, {})
                current = RoundRecord(
                    side=fill.side,
                    submit_time=submission.get("exchange_time"),
                    submit_price=submission.get("price"),
                    submit_volume=submission.get("volume"),
                )
                rounds.append(current)
            current.opens.append(fill)
        elif fill.client_id.startswith("flatten-"):
            if current is None:
                current = RoundRecord(side=fill.side, submit_time=None, submit_price=None, submit_volume=None)
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
            data = record["event"]["data"]
            if record["event"]["type"] == "TradeEvent" and data.get("client_id"):
                has_fill = True
            if any(_action_kind(action) == "submit_order" for action in record["actions"]):
                at = record["at"]
                if first_submit_at is None or at < first_submit_at:
                    first_submit_at = at
            if (
                record["event"]["type"] == "OrderEvent"
                and (data.get("client_id") or "").startswith("flatten-")
                and data["status"] in {"ALLTRADED", "CANCELLED", "REJECTED"}
            ):
                at = record["at"]
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


def _signed(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+g}"


def _gross_pnl(record: RoundRecord, contract: ContractDay) -> tuple[float | None, float | None, float | None]:
    """Signed price diff, tick count, and gross cash PnL for one round.

    Rounds with mixed opening sides or unmatched open/close volumes have no
    well-defined gross PnL and render as em-dashes.
    """
    if not record.opens or not record.closes:
        return None, None, None
    if len({fill.side for fill in record.opens}) > 1:
        return None, None, None
    if record.open_volume != record.close_volume:
        return None, None, None
    diff = record.close_avg_price - record.open_avg_price
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
            "<th>价差</th><th>tick 数</th><th>毛盈亏</th></tr>"
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
        "<p class=\"note\">毛盈亏未含手续费，按成交价与合约乘数计算。</p>"
        f"{body}"
        "</body></html>"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按交易日生成成交明细 HTML 报告")
    parser.add_argument("--audit-dir", default="audit", help="审计根目录")
    parser.add_argument("--out-dir", default="reports", help="报告输出目录")
    parser.add_argument("--date", help="只生成指定交易日（YYYYMMDD）")
    parser.add_argument("--open", action="store_true", help="生成后用浏览器打开")
    args = parser.parse_args(argv)

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
