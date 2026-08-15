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
class DayModel:
    day: str
    runs: list[str] = field(default_factory=list)
    contracts: list[ContractDay] = field(default_factory=list)
    account_snapshots: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def _weighted_price(fills: list[Fill]) -> float:
    total = sum(fill.volume for fill in fills)
    if total == 0:
        return 0.0
    return sum(fill.price * fill.volume for fill in fills) / total


def _load_run(run_dir: Path) -> RunFacts | None:
    if not (run_dir / "summary.json").exists():
        return None
    contract_events: dict[str, list[dict[str, Any]]] = {}
    for events_file in sorted(run_dir.glob("*/events.jsonl")):
        contract_events[events_file.parent.name] = [
            json.loads(record)
            for record in events_file.read_text(encoding="utf-8").splitlines()
            if record.strip()
        ]
    try:
        run_summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        run_summary = {}
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
            if client_id and client_id not in submissions:
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
        model.runs.append(run.directory)
        account_file = run_dir / "account.jsonl"
        if account_file.exists():
            model.account_snapshots[run.directory] = [
                json.loads(line)
                for line in account_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        for contract, events in sorted(run.contract_events.items()):
            rounds, pricetick, size = _build_rounds(events)
            if not rounds:
                continue
            existing = next((entry for entry in model.contracts if entry.contract == contract), None)
            if existing is None:
                existing = ContractDay(contract=contract, pricetick=pricetick, size=size)
                model.contracts.append(existing)
            existing.rounds.extend(rounds)
    return days, skipped


def _time_of_day(exchange_time: str | None) -> str:
    if not exchange_time:
        return "—"
    return exchange_time[11:19]


def _price(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:g}"


def render_html(model: DayModel) -> str:
    rows: list[str] = []
    for contract in model.contracts:
        rows.append(f"<h2>{escape(contract.contract)}</h2>")
        rows.append(
            "<table><tr><th>#</th><th>方向</th><th>挂单时刻</th><th>挂单价</th><th>挂单量</th>"
            "<th>成交时刻</th><th>成交价</th><th>成交量</th>"
            "<th>平仓时刻</th><th>平仓价</th><th>平仓量</th></tr>"
        )
        for index, record in enumerate(contract.rounds, 1):
            open_times = _time_of_day(record.open_first_time)
            if record.open_last_time != record.open_first_time:
                open_times += f" → {_time_of_day(record.open_last_time)}"
            close_times = _time_of_day(record.close_first_time)
            if record.close_last_time != record.close_first_time:
                close_times += f" → {_time_of_day(record.close_last_time)}"
            rows.append(
                "<tr>"
                f"<td>{index}</td><td>{escape(record.side)}</td>"
                f"<td>{escape(_time_of_day(record.submit_time))}</td>"
                f"<td>{_price(record.submit_price)}</td><td>{record.submit_volume if record.submit_volume is not None else '—'}</td>"
                f"<td>{escape(open_times)}</td><td>{_price(record.open_avg_price)}</td><td>{record.open_volume}</td>"
                f"<td>{escape(close_times)}</td><td>{_price(record.close_avg_price)}</td><td>{record.close_volume}</td>"
                "</tr>"
            )
        rows.append("</table>")
    body = "\n".join(rows)
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<title>交易日成交明细 {escape(model.day)}</title>"
        "<style>"
        "body{font-family:-apple-system,'PingFang SC',sans-serif;margin:24px;color:#222}"
        "h1{font-size:20px}h2{font-size:16px;margin-top:28px}"
        "table{border-collapse:collapse;margin-top:8px}"
        "th,td{border:1px solid #ccc;padding:4px 10px;text-align:right;font-variant-numeric:tabular-nums}"
        "th{background:#f5f5f5}td:nth-child(2){text-align:center}"
        "</style></head><body>"
        f"<h1>交易日成交明细 · {escape(model.day)}</h1>"
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
