"""Incremental, audit-only projection for the local trading console."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .activity import ActivityIdentity
from report import _reason_zh, _trace_label


_STAGE_BY_STATE = {
    "RISK_HOLD": "风险",
    "CLOSING_CANCELS": "安全收口",
    "CLOSING_RECONCILE": "安全收口",
    "FLATTENING": "安全收口",
    "WAITING_FOR_STABLE_QUOTE": "交易运行",
    "PAUSED": "交易运行",
    "QUOTE_PENDING": "交易运行",
    "QUOTING": "交易运行",
    "REPLACING": "交易运行",
    "CLOSING_WAIT": "交易运行",
    "WAITING_FOR_CONTRACT": "启动准备",
    "WAITING_FOR_ZERO_POSITION": "启动准备",
    "FINISHED": "终态",
    "FAILED": "终态",
}
_LABEL_BY_STATE = {
    "WAITING_FOR_CONTRACT": "正在连接 CTP／查询目标合约信息",
    "WAITING_FOR_ZERO_POSITION": "合约已就绪／正在核对初始持仓",
    "WAITING_FOR_STABLE_QUOTE": "交易运行／等待稳定行情",
    "PAUSED": "交易运行／等待下一个报价窗口",
    "QUOTE_PENDING": "交易运行／等待报价回报",
    "QUOTING": "交易运行／正在报价",
    "REPLACING": "交易运行／正在撤换报价",
    "CLOSING_WAIT": "交易运行／等待对侧成交",
    "CLOSING_CANCELS": "安全收口／正在撤单",
    "CLOSING_RECONCILE": "安全收口／正在持仓对账",
    "FLATTENING": "安全收口／正在 FAK 平仓",
    "RISK_HOLD": "风险／等待风险解除与对账",
    "FINISHED": "终态／安全完成",
    "FAILED": "终态／安全失败",
}
_STAGE_PRIORITY = {"终态": 0, "启动准备": 1, "交易运行": 2, "安全收口": 3, "风险": 4}
_TERMINAL_ORDER_STATUSES = {"ALLTRADED", "CANCELLED", "REJECTED"}
_MAX_TIMELINE = 100
_MAX_ORDERS = 200
_MAX_TRADES = 200


def _contract_item(name: str) -> dict[str, Any]:
    symbol, _, exchange = name.partition("@")
    return {
        "symbol": symbol,
        "exchange": exchange,
        "state": "WAITING_FOR_CONTRACT",
        "stage": "启动准备",
        "state_label": label_for_state("WAITING_FOR_CONTRACT"),
        "trade_connected": None,
        "market_connected": None,
        "latest_tick": None,
        "last_event_at": None,
        "last_event_age_seconds": None,
        "tick_age_seconds": None,
        "tick_stale": None,
        "max_tick_age_seconds": None,
        "last_wall_time": None,
        "quote_windows": [],
        "current_quote_window": None,
        "round_trips": 0,
        "max_round_trips": None,
        "logical_orders": [],
        "active_order_count": 0,
        "trades": [],
        "confirmed_position": None,
        "last_position_query_error": None,
        "causal_timeline": [],
        "risk_events": [],
        "risk": False,
        "risk_reason": None,
        "risk_key": None,
    }


def stage_for_state(state: str | None) -> str:
    return _STAGE_BY_STATE.get(state or "", "启动准备")


def label_for_state(state: str | None) -> str:
    return _LABEL_BY_STATE.get(state or "", "启动准备／等待会话事实")


class AuditProjector:
    """Keep a bounded current view while consuming complete JSONL records only."""

    def __init__(self, identity: ActivityIdentity) -> None:
        self.identity = identity
        self._offsets: dict[str, int] = {}
        self._contracts: dict[str, dict[str, Any]] = {
            name: _contract_item(name) for name in identity.contracts
        }
        self._order_maps: dict[str, dict[str, dict[str, Any]]] = {
            name: {} for name in identity.contracts
        }
        self._order_id_keys: dict[str, dict[str, str]] = {name: {} for name in identity.contracts}
        self._trade_keys: dict[str, set[str]] = {name: set() for name in identity.contracts}
        self._risk_active: dict[str, str | None] = {name: None for name in identity.contracts}
        self._effective: dict[str, Any] = {}
        self._last_event_at: float | None = None
        self._first_event_at: float | None = None
        self._summary: dict[str, Any] | None = None

    def _apply_record(self, name: str, record: dict[str, Any]) -> None:
        item = self._contracts.setdefault(name, _contract_item(name))
        self._order_maps.setdefault(name, {})
        self._order_id_keys.setdefault(name, {})
        self._trade_keys.setdefault(name, set())
        self._risk_active.setdefault(name, None)
        at = record.get("at")
        if isinstance(at, (int, float)):
            self._first_event_at = at if self._first_event_at is None else min(self._first_event_at, at)
            item["last_event_at"] = at
            self._last_event_at = at if self._last_event_at is None else max(self._last_event_at, at)
        state = record.get("state_after")
        if isinstance(state, str):
            item["state"] = state
            item["stage"] = stage_for_state(state)
            item["state_label"] = label_for_state(state)
            item["risk"] = state == "RISK_HOLD"
            if not item["risk"]:
                item["risk_reason"] = None
                item["risk_key"] = None
                self._risk_active[name] = None
        event = record.get("event")
        if isinstance(event, dict):
            event_type = event.get("type")
            data = event.get("data")
            if isinstance(data, dict) and event_type == "ConnectionEvent":
                kind = data.get("kind")
                if kind == "trade":
                    item["trade_connected"] = bool(data.get("connected"))
                elif kind == "market":
                    item["market_connected"] = bool(data.get("connected"))
            if isinstance(data, dict) and event_type == "TickEvent":
                latest_tick = {
                    key: data.get(key)
                    for key in ("last_price", "bid_price", "ask_price", "exchange_time", "at")
                }
                if not isinstance(latest_tick["at"], (int, float)):
                    latest_tick["at"] = at
                item["latest_tick"] = latest_tick
            if isinstance(data, dict) and event_type == "ClockEvent":
                item["last_wall_time"] = data.get("wall_time") or item["last_wall_time"]
            if isinstance(data, dict) and event_type == "OrderEvent":
                self._upsert_order(name, data, at)
            if isinstance(data, dict) and event_type == "OrderQueryCompleteEvent" and not data.get("error_id"):
                for order in data.get("orders", []):
                    if isinstance(order, dict) and self._find_order(name, order) is not None:
                        self._upsert_order(name, order, at)
            if isinstance(data, dict) and event_type == "TradeEvent":
                self._record_trade(name, data, at)
            if isinstance(data, dict) and event_type == "PositionQueryCompleteEvent":
                self._record_position_query(item, data, at)
                if str(data.get("request_id", "")).startswith("recovery-manual-"):
                    item["manual_flatten_inflight"] = False

        for action in record.get("actions", ()) if isinstance(record.get("actions"), list) else ():
            if not isinstance(action, dict) or action.get("type") != "Action":
                continue
            action_data = action.get("data") if isinstance(action.get("data"), dict) else action
            if action_data.get("kind") == "submit_order" and isinstance(action_data.get("payload"), dict):
                self._upsert_order(name, action_data["payload"], at, from_action=True)
            elif action_data.get("kind") == "cancel_order" and isinstance(action_data.get("payload"), dict):
                order = self._find_order(name, action_data["payload"])
                if order is not None:
                    order["cancel_requested"] = True
                    order["last_at"] = at

        self._apply_traces(name, item, record)

    def _upsert_order(
        self,
        name: str,
        data: dict[str, Any],
        at: Any,
        *,
        from_action: bool = False,
    ) -> None:
        """Merge callbacks and submit actions into one bounded logical order."""
        orders = self._order_maps[name]
        order_id = str(data.get("order_id") or "").strip()
        client_id = str(data.get("client_id") or "").strip()
        key = self._order_id_keys[name].get(order_id) if order_id else None
        if key is None and order_id and not client_id:
            candidates = [
                candidate_key
                for candidate_key, candidate in orders.items()
                if not candidate.get("order_id")
                and candidate.get("side") == data.get("side")
                and candidate.get("price") == data.get("price")
                and candidate.get("volume") == data.get("volume")
            ]
            if len(candidates) == 1:
                key = candidates[0]
        key = key or client_id or order_id
        if not key:
            return
        if key not in orders:
            if len(orders) >= _MAX_ORDERS:
                orders.pop(next(iter(orders)))
            orders[key] = {
                "logical_id": key,
                "order_id": order_id or None,
                "client_id": client_id or None,
                "symbol": data.get("symbol"),
                "exchange": data.get("exchange"),
                "side": data.get("side"),
                "offset": data.get("offset"),
                "order_type": data.get("order_type"),
                "price": data.get("price"),
                "volume": data.get("volume", 0),
                "traded": data.get("traded", 0),
                "status": data.get("status", "SUBMITTING"),
                "status_unknown": bool(data.get("status_unknown", False)),
                "status_path": [],
                "first_at": at,
                "last_at": at,
                "exchange_time": data.get("exchange_time"),
                "cancel_requested": False,
            }
        order = orders[key]
        for field in ("order_id", "client_id", "symbol", "exchange", "side", "offset", "order_type", "exchange_time"):
            value = data.get(field)
            if value not in (None, ""):
                order[field] = value
        for field in ("price", "volume", "traded"):
            value = data.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if field == "traded":
                    order[field] = max(order.get(field, 0), value)
                elif value or field != "price":
                    order[field] = value
        if not from_action:
            status = data.get("status")
            if isinstance(status, str) and status:
                if status != order.get("status"):
                    order["status"] = status
                if status not in order["status_path"]:
                    order["status_path"].append(status)
            order["status_unknown"] = bool(data.get("status_unknown", False))
        elif not order["status_path"]:
            order["status_path"].append(order["status"])
        order["last_at"] = at
        if order_id:
            self._order_id_keys[name][order_id] = key
        if client_id:
            order["client_id"] = client_id

    def _sync_order_view(self, name: str) -> None:
        orders = list(self._order_maps[name].values())
        for order in orders:
            order["active"] = bool(order.get("status_unknown")) or order.get("status") not in _TERMINAL_ORDER_STATUSES
            order_id = str(order.get("order_id") or "").strip()
            client_id = str(order.get("client_id") or "").strip()
            for trade in self._contracts[name]["trades"]:
                same_order = bool(order_id) and order_id == str(trade.get("order_id") or "").strip()
                same_client = bool(client_id) and client_id == str(trade.get("client_id") or "").strip()
                if same_order or same_client:
                    trade["offset"] = order.get("offset")
        self._contracts[name]["logical_orders"] = orders
        self._contracts[name]["active_order_count"] = sum(1 for order in orders if order["active"])

    def _record_trade(self, name: str, data: dict[str, Any], at: Any) -> None:
        order = self._find_order(name, data)
        client_id = str(data.get("client_id") or "").strip() or str((order or {}).get("client_id") or "").strip()
        if not client_id:
            return
        trade_id = str(data.get("trade_id") or "").strip()
        if trade_id:
            key = "trade:" + trade_id
        else:
            key = "event:" + ":".join(
                str(data.get(field, "")) for field in ("order_id", "client_id", "side", "volume", "price", at)
            )
        if key in self._trade_keys[name]:
            return
        self._trade_keys[name].add(key)
        trade = {
            "trade_id": trade_id or None,
            "order_id": data.get("order_id"),
            "client_id": client_id or (order or {}).get("client_id"),
            "symbol": data.get("symbol"),
            "exchange": data.get("exchange"),
            "side": data.get("side"),
            "offset": (order or {}).get("offset"),
            "price": data.get("price"),
            "volume": data.get("volume"),
            "exchange_time": data.get("exchange_time"),
            "at": at,
        }
        trades = self._contracts[name]["trades"]
        trades.append(trade)
        if len(trades) > _MAX_TRADES:
            del trades[:-_MAX_TRADES]

    def _find_order(self, name: str, data: dict[str, Any]) -> dict[str, Any] | None:
        order_id = str(data.get("order_id") or "").strip()
        client_id = str(data.get("client_id") or "").strip()
        key = self._order_id_keys[name].get(order_id) if order_id else client_id
        return self._order_maps[name].get(key) if key else None

    @staticmethod
    def _record_position_query(item: dict[str, Any], data: dict[str, Any], at: Any) -> None:
        error_id = data.get("error_id", 0)
        net_position = data.get("net_position")
        if error_id:
            item["last_position_query_error"] = {
                "error_id": error_id,
                "error_msg": data.get("error_msg", ""),
                "request_id": data.get("request_id"),
                "at": at,
            }
            return
        if isinstance(net_position, (int, float)) and not isinstance(net_position, bool):
            item["confirmed_position"] = {
                "net_position": net_position,
                "gross_position": data.get("gross_position"),
                "confirmed_at": at,
                "confirmed_wall_time": item.get("last_wall_time"),
                "request_id": data.get("request_id"),
                "source": "CTP position query",
            }

    def _apply_traces(self, name: str, item: dict[str, Any], record: dict[str, Any]) -> None:
        traces = record.get("trace")
        if not isinstance(traces, list):
            return
        at = record.get("at")
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            entry = {
                "at": at,
                "state_before": record.get("state_before"),
                "state_after": record.get("state_after"),
                "code": trace.get("code"),
                "label": _trace_label(trace.get("code")),
            }
            for field in ("client_ids", "market", "calculation", "replacement"):
                if field in trace:
                    entry[field] = trace[field]
            item["causal_timeline"].append(entry)
            if len(item["causal_timeline"]) > _MAX_TIMELINE:
                del item["causal_timeline"][:-_MAX_TIMELINE]
            calculation = trace.get("calculation") if isinstance(trace.get("calculation"), dict) else {}
            if trace.get("code") == "manual_flatten_requested":
                item["manual_flatten_inflight"] = True
            elif trace.get("code") in {"manual_flatten_query_failed", "manual_flatten_complete"}:
                item["manual_flatten_inflight"] = False
            if calculation.get("reason"):
                entry["reason_label"] = _reason_zh(calculation["reason"])
            if trace.get("code") == "manual_flatten_complete":
                item["resume_quotes_at"] = calculation.get("resume_quotes_at")
            rounds = calculation.get("round_trips")
            if isinstance(rounds, (int, float)) and not isinstance(rounds, bool):
                item["round_trips"] = rounds
            if item.get("state") == "RISK_HOLD" and trace.get("code") == "risk_hold":
                reason = calculation.get("reason") or calculation.get("failure_reason") or trace["code"]
                risk_key = f"{self.identity.run_id}:{name}:{reason}"
                item["risk_reason"] = reason
                item["risk_key"] = risk_key
                if self._risk_active.get(name) != risk_key:
                    item["risk_events"].append({"key": risk_key, "reason": reason, "at": at})
                    if len(item["risk_events"]) > _MAX_TIMELINE:
                        del item["risk_events"][:-_MAX_TIMELINE]
                    self._risk_active[name] = risk_key

    def _load_effective_strategy(self, audit_root: Path) -> None:
        try:
            value = json.loads((audit_root / "effective_strategy.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        effective = value.get("effective") if isinstance(value, dict) else None
        if isinstance(effective, dict):
            self._effective = effective

    def _decorate_contract(self, name: str, item: dict[str, Any], now_value: float) -> dict[str, Any]:
        updated = dict(item)
        updated["resume_wait_seconds"] = max(0, (item.get("resume_quotes_at") or 0) - now_value)
        effective_contract = next(
            (
                contract
                for contract in self._effective.get("contracts", ())
                if isinstance(contract, dict)
                and contract.get("symbol") == item["symbol"]
                and contract.get("exchange") == item["exchange"]
            ),
            {},
        )
        updated["quote_windows"] = list(effective_contract.get("quote_windows", ()))
        updated["max_tick_age_seconds"] = effective_contract.get("max_tick_age_seconds")
        # 旧审计把上限放在运行根节点；新运行只读取各合约项。
        updated["max_round_trips"] = effective_contract.get("max_round_trips", self._effective.get("max_round_trips"))
        updated["current_quote_window"] = self._current_quote_window(
            updated["quote_windows"], updated.get("last_wall_time")
        )
        at = updated["last_event_at"]
        updated["last_event_age_seconds"] = max(0.0, now_value - at) if isinstance(at, (int, float)) else None
        tick = updated.get("latest_tick")
        tick_at = tick.get("at") if isinstance(tick, dict) else None
        updated["tick_age_seconds"] = self._tick_age_seconds(
            tick, updated.get("last_wall_time"), now_value
        )
        threshold = updated.get("max_tick_age_seconds")
        updated["tick_stale"] = (
            updated["tick_age_seconds"] is not None
            and isinstance(threshold, (int, float))
            and updated["tick_age_seconds"] > threshold
        )
        return updated

    def _tick_age_seconds(self, tick: Any, wall_time: Any, now_value: float) -> float | None:
        if not isinstance(tick, dict):
            return None
        if self.identity.market_data_mode != "replay_override":
            try:
                exchange = datetime.fromisoformat(str(tick["exchange_time"]).replace("Z", "+00:00"))
                wall = datetime.fromisoformat(str(wall_time).replace("Z", "+00:00"))
                if exchange.tzinfo is not None and wall.tzinfo is not None:
                    return max(0.0, (wall - exchange).total_seconds())
            except (KeyError, TypeError, ValueError):
                pass
        tick_at = tick.get("at")
        return max(0.0, now_value - tick_at) if isinstance(tick_at, (int, float)) else None

    @staticmethod
    def _current_quote_window(windows: list[Any], wall_time: Any) -> dict[str, Any] | None:
        if not isinstance(wall_time, str):
            return None
        try:
            parsed = datetime.fromisoformat(wall_time.replace("Z", "+00:00"))
            minute = parsed.hour * 60 + parsed.minute
        except ValueError:
            return None
        for index, window in enumerate(windows):
            if not isinstance(window, dict):
                continue
            try:
                start_text, end_text = window["start"], window["end"]
                start = int(start_text[:2]) * 60 + int(start_text[3:])
                end = int(end_text[:2]) * 60 + int(end_text[3:])
            except (KeyError, TypeError, ValueError):
                continue
            active = start <= minute < end if start < end else minute >= start or minute < end
            if active:
                return {"index": index, "start": start_text, "end": end_text, "active": True}
        return None

    def refresh(self, *, now: float | None = None, status: str = "active") -> dict[str, Any]:
        now_value = time.monotonic() if now is None else now
        audit_root = Path(self.identity.audit_dir)
        self._load_effective_strategy(audit_root)
        for name in self.identity.contracts:
            event_path = audit_root / name / "events.jsonl"
            try:
                offset = self._offsets.get(name, 0)
                if event_path.stat().st_size < offset:
                    offset = 0
                with event_path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    while line := handle.readline():
                        if not line.endswith("\n"):
                            break
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            pass
                        else:
                            if isinstance(record, dict):
                                self._apply_record(name, record)
                        offset = handle.tell()
                self._offsets[name] = offset
                self._sync_order_view(name)
            except OSError:
                pass

        summary_path = audit_root / "summary.json"
        try:
            self._summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            self._summary = None

        if self._summary is not None:
            for contract in self._summary.get("contracts", ()):
                if not isinstance(contract, dict):
                    continue
                name = f"{contract.get('target_symbol')}@{contract.get('target_exchange')}"
                item = self._contracts.get(name)
                if item is None:
                    continue
                rounds = contract.get("round_trips")
                if isinstance(rounds, (int, float)) and not isinstance(rounds, bool):
                    item["round_trips"] = rounds
                if item["state"] == "RISK_HOLD" and not item.get("risk_reason"):
                    reason = contract.get("failure_reason") or "risk_hold"
                    item["risk_reason"] = reason
                    item["risk_key"] = f"{self.identity.run_id}:{name}:{reason}"
                    if self._risk_active.get(name) != item["risk_key"]:
                        item["risk_events"].append({"key": item["risk_key"], "reason": reason, "at": None})
                        if len(item["risk_events"]) > _MAX_TIMELINE:
                            del item["risk_events"][:-_MAX_TIMELINE]
                        self._risk_active[name] = item["risk_key"]

        contracts = [
            self._decorate_contract(name, item, now_value)
            for name, item in self._contracts.items()
        ]
        stage = max((item["stage"] for item in contracts), key=lambda value: _STAGE_PRIORITY[value], default="启动准备")
        last_event = self._last_event_at
        stale = last_event is not None and now_value - last_event > 3
        snapshot: dict[str, Any] = {
            "status": status,
            "run": self.identity.as_dict(),
            "overall_stage": stage,
            "last_event_at": last_event,
            "audit_started_at": self._first_event_at,
            "effective": self._effective,
            "data_stale": stale,
            "contracts": contracts,
        }
        if self._summary is not None:
            snapshot["terminal_state"] = self._summary.get("terminal_state")
            snapshot["all_finished"] = self._summary.get("all_finished")
        return snapshot
