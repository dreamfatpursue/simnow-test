"""Deterministic live-grid session driven by external CTP facts."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from math import isfinite
from typing import Any

from .config import StrategyConfig


class SessionState(str, Enum):
    PREVIEW = "PREVIEW"
    WAITING_FOR_CONTRACT = "WAITING_FOR_CONTRACT"
    WAITING_FOR_ZERO_POSITION = "WAITING_FOR_ZERO_POSITION"
    WAITING_FOR_STABLE_QUOTE = "WAITING_FOR_STABLE_QUOTE"
    QUOTING = "QUOTING"
    REPLACING = "REPLACING"
    CLOSING_CANCELS = "CLOSING_CANCELS"
    CLOSING_RECONCILE = "CLOSING_RECONCILE"
    FLATTENING = "FLATTENING"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


# 收口中与终态的合集：处于其中任何状态时不得开启新一轮收口或报价。
_CLOSING_OR_TERMINAL = frozenset(
    {
        SessionState.CLOSING_CANCELS,
        SessionState.CLOSING_RECONCILE,
        SessionState.FLATTENING,
        SessionState.FINISHED,
        SessionState.FAILED,
    }
)


@dataclass(frozen=True)
class ContractEvent:
    symbol: str
    exchange: str
    pricetick: float


@dataclass(frozen=True)
class TickEvent:
    symbol: str
    exchange: str
    last_price: float
    bid_price: float
    ask_price: float
    at: float


@dataclass(frozen=True)
class OrderEvent:
    order_id: str
    symbol: str
    exchange: str
    side: str
    status: str
    volume: int
    traded: int = 0
    price: float = 0
    client_id: str | None = None


@dataclass(frozen=True)
class TradeEvent:
    order_id: str
    symbol: str
    exchange: str
    side: str
    volume: int
    price: float
    trade_id: str = ""
    client_id: str | None = None


@dataclass(frozen=True)
class PositionQueryCompleteEvent:
    request_id: str
    symbol: str
    exchange: str
    net_position: int
    error_id: int = 0
    error_msg: str = ""


@dataclass(frozen=True)
class ClockEvent:
    at: float


@dataclass(frozen=True)
class InterruptEvent:
    pass


@dataclass(frozen=True)
class Action:
    kind: str
    payload: dict[str, Any]


@dataclass
class _Order:
    order_id: str
    client_id: str
    side: str
    volume: int
    traded: int = 0
    status: str = "SUBMITTING"
    cancel_requested: bool = False
    price: float = 0
    trade_ids: set[str] = field(default_factory=set)
    trade_volume: int = 0
    accounted_traded: int = 0
    accounted_opening_traded: int = 0
    is_flatten: bool = False

    @property
    def terminal(self) -> bool:
        return self.status in {"ALLTRADED", "CANCELLED", "REJECTED"}

    @property
    def active(self) -> bool:
        return not self.terminal


@dataclass
class LiveGridSession:
    """A single-contract order-management session.

    The public seam is :meth:`handle`: it consumes normalized external facts and
    returns order, cancel, query, and terminal actions. It never infers fills
    from prices.
    """

    config: StrategyConfig
    simnow_confirmed: bool
    state: SessionState = field(init=False)
    actions: list[Action] = field(default_factory=list, init=False)
    failure_reason: str | None = field(default=None, init=False)
    stop_reason: str | None = field(default=None, init=False)
    first_fill: dict[str, Any] | None = field(default=None, init=False)
    final_net_position: int | None = field(default=None, init=False)
    startup_position_result: str | None = field(default=None, init=False)
    cancellation_terminal: bool | None = field(default=None, init=False)
    flatten_attempts: list[dict[str, Any]] = field(default_factory=list, init=False)
    audit_events: list[dict[str, Any]] = field(default_factory=list, init=False)
    state_transitions: list[dict[str, str]] = field(default_factory=list, init=False)
    startup_position_request_id: str | None = field(default=None, init=False)
    closing_position_request_id: str | None = field(default=None, init=False)
    closing_position_result: dict[str, Any] | None = field(default=None, init=False)
    _contract: ContractEvent | None = field(default=None, init=False)
    _latest_tick: TickEvent | None = field(default=None, init=False)
    _stable_since: float | None = field(default=None, init=False)
    _outside_since: float | None = field(default=None, init=False)
    _anchor_ticks: int | None = field(default=None, init=False)
    _orders: dict[str, _Order] = field(default_factory=dict, init=False)
    _client_to_order: dict[str, str] = field(default_factory=dict, init=False)
    _pending_clients: dict[str, _Order] = field(default_factory=dict, init=False)
    _sequence: int = field(default=0, init=False)
    _request_sequence: int = field(default=0, init=False)
    _now: float = field(default=0.0, init=False)
    _closing_started_at: float | None = field(default=None, init=False)
    _flatten_started_at: float | None = field(default=None, init=False)
    _flatten_initial_price: float | None = field(default=None, init=False)
    _flatten_client_id: str | None = field(default=None, init=False)
    _replacement_reason: str | None = field(default=None, init=False)
    _replacement_safety: bool = field(default=False, init=False)
    _action_times: deque[float] = field(default_factory=deque, init=False)
    _action_limit_paused: bool = field(default=False, init=False)
    _replacement_started_at: float | None = field(default=None, init=False)
    _replacement_warning_emitted: bool = field(default=False, init=False)
    _round_trips: int = field(default=0, init=False)
    _round_has_fill: bool = field(default=False, init=False)
    _end_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.state = (
            SessionState.WAITING_FOR_CONTRACT
            if self.config.can_submit(
                simnow_confirmed=self.simnow_confirmed,
            )
            else SessionState.PREVIEW
        )
        self.state_transitions.append({"from": "", "to": self.state.value})
        # 唯一的墙钟读取点：把 session_end_time 折算为单调时钟期限，此后保持事件驱动确定性。
        # 取未来 24 小时内最近的该时刻，跨午夜收盘时刻（如夜盘 01:00）自然支持。
        end_time = self.config.effective["session_end_time"]
        if end_time:
            hour, minute = (int(part) for part in end_time.split(":"))
            now = datetime.now()
            deadline = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if deadline <= now:
                deadline += timedelta(days=1)
            self._end_at = time.monotonic() + (deadline - now).total_seconds()

    def _past_end(self) -> bool:
        return self._end_at is not None and self._now >= self._end_at

    @property
    def target_symbol(self) -> str:
        return self.config.effective["symbol"]

    @property
    def target_exchange(self) -> str:
        return self.config.effective["exchange"]

    @property
    def orders(self) -> tuple[dict[str, Any], ...]:
        """Return an immutable snapshot suitable for assertions and audit."""
        return tuple(
            {
                "order_id": order.order_id,
                "client_id": order.client_id,
                "side": order.side,
                "volume": order.volume,
                "traded": order.traded,
                "status": order.status,
                "cancel_requested": order.cancel_requested,
                "price": order.price,
            }
            for order in self._orders.values()
        )

    def handle(self, event: object) -> list[Action]:
        """Consume one normalized event and return newly emitted actions."""
        start = len(self.actions)
        before = self.state
        if isinstance(event, ContractEvent):
            self._on_contract(event)
        elif isinstance(event, TickEvent):
            self._on_tick(event)
        elif isinstance(event, OrderEvent):
            self._on_order(event)
        elif isinstance(event, TradeEvent):
            self._on_trade(event)
        elif isinstance(event, PositionQueryCompleteEvent):
            self._on_position_query_complete(event)
        elif isinstance(event, ClockEvent):
            self._on_clock(event)
        elif isinstance(event, InterruptEvent):
            self._on_interrupt()
        else:
            raise TypeError(f"不支持的实时网格事件: {type(event).__name__}")
        new_actions = self.actions[start:]
        if before != self.state:
            self.state_transitions.append({"from": before.value, "to": self.state.value})
        self.audit_events.append(
            {
                "at": self._now,
                "event": self._serialize(event),
                "state_before": before.value,
                "state_after": self.state.value,
                "actions": [self._serialize(action) for action in new_actions],
            }
        )
        return new_actions

    def _on_contract(self, event: ContractEvent) -> None:
        if self.state != SessionState.WAITING_FOR_CONTRACT:
            return
        if not self._is_target(event.symbol, event.exchange):
            return
        self._contract = event
        if not isfinite(event.pricetick) or event.pricetick <= 0:
            return
        self._request_sequence += 1
        self.startup_position_request_id = f"position-{self._request_sequence}"
        self.state = SessionState.WAITING_FOR_ZERO_POSITION
        self._emit(
            "query_position",
            request_id=self.startup_position_request_id,
            symbol=self.target_symbol,
            exchange=self.target_exchange,
            phase="startup",
        )

    def _on_tick(self, event: TickEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        self._now = max(self._now, event.at)
        self._latest_tick = event

        if self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            if self._protected_valid(event):
                if self._stable_since is None:
                    self._stable_since = event.at
            else:
                self._stable_since = None
            return

        if self.state == SessionState.QUOTING:
            if not self._protected_valid(event):
                self._begin_replacement("market_pause", safety=True)
                return
            if self._anchor_ticks is None:
                return
            current_ticks = self._price_ticks(event.last_price)
            low = self._anchor_ticks - self.config.effective["w_ticks"]
            high = self._anchor_ticks + self.config.effective["w_ticks"]
            if low <= current_ticks <= high:
                self._outside_since = None
                return
            if self._outside_since is None:
                self._outside_since = event.at
            elif event.at - self._outside_since >= self.config.effective["reanchor_confirmation_seconds"]:
                self._anchor_ticks = self._move_anchor(current_ticks)
                self._outside_since = None
                self._begin_replacement("reanchor", safety=False)
            return

        if self.state == SessionState.FLATTENING and self._flatten_client_id is None:
            self._try_flatten()

    def _on_order(self, event: OrderEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        order = self._find_or_bind_order(event)
        if order is None:
            return
        previous_traded = order.traded
        if not order.terminal:
            order.status = event.status
        order.volume = event.volume or order.volume
        order.price = event.price or order.price
        order.traded = max(order.traded, event.traded)
        self._account_flatten_fill(order)
        traded_delta = order.traded - previous_traded

        if order.is_flatten:
            if self.state not in _CLOSING_OR_TERMINAL:
                if traded_delta > 0:
                    # 迟到的平仓成交改变了净仓：不得带仓续挂，立即开启新一轮收口。
                    self._enter_closing("late_fill", order=order, volume=traded_delta)
                return
            if self.state == SessionState.FINISHED and self.final_net_position != 0:
                self.failure_reason = self.failure_reason or "late_flatten_fill_after_finish"
                self.state = SessionState.FAILED
            if self.state == SessionState.FLATTENING:
                if event.status == "REJECTED":
                    self._fail("flatten_rejected")
                    self._cancel_all(safety=True)
                elif order.terminal and order.client_id == self._flatten_client_id:
                    self._flatten_client_id = None
                    if self.final_net_position == 0:
                        if self._flatten_orders_terminal():
                            self._finish_or_fail()
                        else:
                            self._cancel_all(safety=True)
                    else:
                        self._try_flatten()
                elif self.final_net_position == 0:
                    self._cancel_all(safety=True)
        elif self.state in {SessionState.QUOTING, SessionState.REPLACING} and traded_delta > 0:
            self._enter_closing("first_fill", order=order, volume=traded_delta)
        elif traded_delta > 0 and self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            # 上一轮撤单竞速失败的迟到成交：立即按新一轮收口处理（净仓以对账查询为准）。
            self._enter_closing("late_fill", order=order, volume=traded_delta)
        elif traded_delta > 0 and self.state in {
            SessionState.CLOSING_RECONCILE,
            SessionState.FLATTENING,
            SessionState.FINISHED,
            SessionState.FAILED,
        }:
            self._record_late_opening_fill(order)

        if self.state == SessionState.REPLACING:
            self._maybe_finish_replacement()
        elif self.state == SessionState.CLOSING_CANCELS:
            self._maybe_reconcile()

    def _on_trade(self, event: TradeEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        order = self._find_order(event.order_id, event.client_id)
        if order is None:
            return
        if event.trade_id and event.trade_id in order.trade_ids:
            return
        if event.trade_id:
            order.trade_ids.add(event.trade_id)
        order.trade_volume += event.volume
        order.traded = max(order.traded, order.trade_volume)
        accounted_before = order.accounted_traded
        self._account_flatten_fill(order)
        if order.is_flatten:
            if self.state not in _CLOSING_OR_TERMINAL:
                if order.accounted_traded > accounted_before:
                    # 迟到的平仓成交改变了净仓：不得带仓续挂，立即开启新一轮收口。
                    self._enter_closing(
                        "late_fill",
                        order=order,
                        volume=order.accounted_traded - accounted_before,
                        price=event.price,
                        trade_id=event.trade_id,
                    )
                return
            current_flatten_terminal = order.client_id == self._flatten_client_id and order.terminal
            if self.state == SessionState.FINISHED and self.final_net_position != 0:
                self.failure_reason = self.failure_reason or "late_flatten_fill_after_finish"
                self.state = SessionState.FAILED
            if order.client_id == self._flatten_client_id and order.terminal:
                self._flatten_client_id = None
            if self.final_net_position == 0:
                if self._flatten_orders_terminal():
                    self._finish_or_fail()
                else:
                    self._cancel_all(safety=True)
            elif current_flatten_terminal:
                self._try_flatten()
            return
        if self.state in {SessionState.QUOTING, SessionState.REPLACING}:
            self._enter_closing(
                "first_fill",
                order=order,
                volume=event.volume,
                price=event.price,
                trade_id=event.trade_id,
            )
        elif self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            self._enter_closing(
                "late_fill",
                order=order,
                volume=event.volume,
                price=event.price,
                trade_id=event.trade_id,
            )
        elif self.state in {
            SessionState.CLOSING_RECONCILE,
            SessionState.FLATTENING,
            SessionState.FINISHED,
            SessionState.FAILED,
        }:
            self._record_late_opening_fill(order)

    def _on_position_query_complete(self, event: PositionQueryCompleteEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        if self.state == SessionState.WAITING_FOR_ZERO_POSITION:
            if event.request_id != self.startup_position_request_id:
                return
            if event.error_id:
                self._fail("startup_position_query_failed")
                return
            if event.net_position != 0:
                self.startup_position_result = "nonzero"
                self.final_net_position = event.net_position
                self._fail("nonzero_startup_position")
            else:
                self.startup_position_result = "zero"
                self.final_net_position = 0
                self.state = SessionState.WAITING_FOR_STABLE_QUOTE
                self._stable_since = None
            return

        if self.state != SessionState.CLOSING_RECONCILE:
            return
        if event.request_id != self.closing_position_request_id:
            return
        if event.error_id:
            self.closing_position_result = {
                "request_id": event.request_id,
                "net_position": None,
                "error_id": event.error_id,
                "error_msg": event.error_msg,
            }
            self._fail("closing_position_query_failed")
            return
        for order in self._orders.values():
            if not order.is_flatten:
                order.accounted_opening_traded = order.traded
        self.closing_position_result = {
            "request_id": event.request_id,
            "net_position": event.net_position,
            "error_id": event.error_id,
            "error_msg": event.error_msg,
        }
        self.final_net_position = event.net_position
        if event.net_position == 0:
            self._finish_or_fail()
            return
        self._round_has_fill = True
        self.state = SessionState.FLATTENING
        self._flatten_started_at = self._now
        self._try_flatten()

    def _on_clock(self, event: ClockEvent) -> None:
        self._now = max(self._now, event.at)
        self._prune_actions()

        if self._past_end():
            if self.state in {
                SessionState.PREVIEW,
                SessionState.WAITING_FOR_CONTRACT,
                SessionState.WAITING_FOR_ZERO_POSITION,
                SessionState.WAITING_FOR_STABLE_QUOTE,
            }:
                self.stop_reason = self.stop_reason or "session_end"
                self.state = SessionState.FINISHED
                return
            if self.state in {SessionState.QUOTING, SessionState.REPLACING}:
                self.stop_reason = "session_end"
                self._enter_closing("session_end")
                return

        if self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            if (
                self._stable_since is not None
                and self._latest_tick is not None
                and self._protected_valid(self._latest_tick)
                and self._now - self._stable_since >= self.config.effective["stable_market_seconds"]
            ):
                self._submit_quotes()
            return

        if self.state == SessionState.REPLACING:
            self._retry_replacement_cancels()
            self._warn_if_replacement_delayed()
            self._maybe_finish_replacement()
            return

        if self.state == SessionState.CLOSING_CANCELS:
            self._cancel_all(safety=True)
            if self._closing_started_at is not None and self._now - self._closing_started_at >= self.config.effective["cancel_timeout_seconds"]:
                if self.failure_reason is None:
                    self.failure_reason = "cancel_timeout"
                self.cancellation_terminal = False
                self._begin_reconcile()
            else:
                self._maybe_reconcile()
            return

        if self.state == SessionState.FLATTENING:
            if self._flatten_started_at is not None and self._now - self._flatten_started_at >= self.config.effective["flatten_timeout_seconds"]:
                self._cancel_all(safety=True)
                self._fail("flatten_timeout")
            elif self._flatten_client_id is None:
                self._try_flatten()

    def _on_interrupt(self) -> None:
        if self.state in {SessionState.PREVIEW, SessionState.FINISHED, SessionState.FAILED}:
            if self.state == SessionState.PREVIEW:
                self.state = SessionState.FINISHED
            return
        self.stop_reason = self.stop_reason or "interrupted"
        if self.state in {SessionState.WAITING_FOR_CONTRACT, SessionState.WAITING_FOR_ZERO_POSITION}:
            self.failure_reason = self.failure_reason or "interrupted_before_zero_position"
            self._fail("interrupted_before_zero_position")
            return
        if self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            self.state = SessionState.FINISHED
            return
        if self.state not in {SessionState.CLOSING_CANCELS, SessionState.CLOSING_RECONCILE, SessionState.FLATTENING}:
            self._enter_closing("interrupt")

    def _submit_quotes(self) -> bool:
        if self._past_end():
            return False
        tick = self._latest_tick
        if not self._protected_valid(tick) or self._contract is None:
            return False
        if not self._normal_actions_available(2):
            if not self._action_limit_paused:
                self._emit("audit_warning", code="normal_action_limit_reached")
            self._action_limit_paused = True
            return False
        self._action_limit_paused = False
        current_ticks = self._price_ticks(tick.last_price)
        if self._anchor_ticks is None:
            self._anchor_ticks = current_ticks
        w = self.config.effective["w_ticks"]
        d = self.config.effective["d_ticks"]
        distance = w + d
        prices = (
            (self._anchor_ticks - distance) * self._contract.pricetick,
            (self._anchor_ticks + distance) * self._contract.pricetick,
        )
        if not all(isfinite(price) and price > 0 for price in prices):
            return False
        self._sequence += 1
        for side, price in zip(("BUY", "SELL"), prices):
            client_id = f"quote-{self._sequence}-{side.lower()}"
            order = _Order(
                order_id="",
                client_id=client_id,
                side=side,
                volume=self.config.effective["target_lots"],
                price=price,
            )
            self._pending_clients[client_id] = order
            self._emit(
                "submit_order",
                client_id=client_id,
                symbol=self.target_symbol,
                exchange=self.target_exchange,
                side=side,
                offset="OPEN",
                order_type="LIMIT",
                volume=order.volume,
                price=order.price,
            )
            self._record_normal_action()
        self.state = SessionState.QUOTING
        return True

    def _begin_replacement(self, reason: str, *, safety: bool) -> None:
        if self.state in _CLOSING_OR_TERMINAL:
            return
        self.state = SessionState.REPLACING
        self._replacement_reason = reason
        self._replacement_safety = safety
        self._replacement_started_at = self._now
        self._replacement_warning_emitted = False
        self._cancel_all(safety=safety)
        self._maybe_finish_replacement()

    def _retry_replacement_cancels(self) -> None:
        for order in self._orders.values():
            if order.active and not order.cancel_requested:
                self._emit_cancel(order, safety=self._replacement_safety)

    def _warn_if_replacement_delayed(self) -> None:
        if (
            self._replacement_warning_emitted
            or self._replacement_started_at is None
            or self._now - self._replacement_started_at < 1
            or (not self._active_orders() and not self._pending_clients)
        ):
            return
        self._replacement_warning_emitted = True
        self._emit(
            "audit_warning",
            code="replacement_waiting_for_terminal_order_callbacks",
            elapsed_seconds=self._now - self._replacement_started_at,
        )

    def _maybe_finish_replacement(self) -> None:
        if self.state != SessionState.REPLACING:
            return
        if self._active_orders() or self._pending_clients:
            return
        reason = self._replacement_reason
        self._replacement_reason = None
        self._replacement_safety = False
        if reason == "market_pause":
            self.state = SessionState.WAITING_FOR_STABLE_QUOTE
            self._stable_since = None
        elif reason == "reanchor":
            self.state = SessionState.WAITING_FOR_STABLE_QUOTE
            self._stable_since = None
        else:
            self.state = SessionState.WAITING_FOR_STABLE_QUOTE
            self._stable_since = None

    def _enter_closing(self, reason: str, *, order: _Order | None = None, volume: int = 0, price: float = 0, trade_id: str = "") -> None:
        if self.state in _CLOSING_OR_TERMINAL:
            return
        self.state = SessionState.CLOSING_CANCELS
        self._closing_started_at = self._now
        self.cancellation_terminal = None
        self._round_has_fill = order is not None
        if order is not None:
            self.first_fill = {
                "order_id": order.order_id,
                "client_id": order.client_id,
                "side": order.side,
                "volume": volume,
                "price": price or order.price,
                "trade_id": trade_id,
            }
        self.final_net_position = None
        self._cancel_all(safety=True)
        self._maybe_reconcile()

    def _cancel_all(self, *, safety: bool) -> None:
        for order in self._orders.values():
            if order.active and not order.cancel_requested:
                self._emit_cancel(order, safety=safety)
        for order in self._pending_clients.values():
            order.cancel_requested = True

    def _emit_cancel(self, order: _Order, *, safety: bool) -> None:
        if not safety and not self._normal_actions_available(1):
            if not self._action_limit_paused:
                self._emit("audit_warning", code="normal_action_limit_reached")
            self._action_limit_paused = True
            return
        order.cancel_requested = True
        self._emit(
            "cancel_order",
            order_id=order.order_id,
            client_id=order.client_id,
            symbol=self.target_symbol,
            exchange=self.target_exchange,
            safety=safety,
        )
        if not safety:
            self._record_normal_action()

    def _maybe_reconcile(self) -> None:
        if self.state == SessionState.CLOSING_CANCELS and not self._active_orders() and not self._pending_clients:
            self.cancellation_terminal = True
            self._begin_reconcile()

    def _begin_reconcile(self) -> None:
        if self.state != SessionState.CLOSING_CANCELS:
            return
        self._request_sequence += 1
        self.closing_position_request_id = f"position-{self._request_sequence}"
        self.state = SessionState.CLOSING_RECONCILE
        self._emit(
            "query_position",
            request_id=self.closing_position_request_id,
            symbol=self.target_symbol,
            exchange=self.target_exchange,
            phase="closing",
        )

    def _try_flatten(self) -> None:
        if self.state != SessionState.FLATTENING or self.final_net_position in {None, 0} or self._flatten_client_id is not None:
            return
        tick = self._latest_tick
        if not self._executable_quote(tick):
            return
        assert tick is not None
        long_position = self.final_net_position > 0
        side = "SELL" if long_position else "BUY"
        executable = tick.bid_price if long_position else tick.ask_price
        if self._flatten_initial_price is None:
            self._flatten_initial_price = executable
        else:
            tick_size = self._contract.pricetick if self._contract else 0
            cap = self.config.effective["flatten_adverse_ticks"] * tick_size
            if long_position:
                executable = max(executable, self._flatten_initial_price - cap)
            else:
                executable = min(executable, self._flatten_initial_price + cap)
        self._sequence += 1
        client_id = f"flatten-{self._sequence}"
        self._flatten_client_id = client_id
        offset = "CLOSETODAY" if self.target_exchange in {"SHFE", "INE"} else "CLOSE"
        flatten_order = _Order(
            order_id="",
            client_id=client_id,
            side=side,
            volume=abs(self.final_net_position),
            price=executable,
            is_flatten=True,
        )
        self._pending_clients[client_id] = flatten_order
        self.flatten_attempts.append(
            {
                "client_id": client_id,
                "side": side,
                "offset": offset,
                "volume": abs(self.final_net_position),
                "price": executable,
                "at": self._now,
            }
        )
        self._emit(
            "submit_order",
            client_id=client_id,
            symbol=self.target_symbol,
            exchange=self.target_exchange,
            side=side,
            offset=offset,
            order_type="FAK",
            volume=abs(self.final_net_position),
            price=executable,
            safety=True,
        )

    def _finish_or_fail(self) -> None:
        if self.failure_reason is not None:
            self.state = SessionState.FAILED
            return
        if not self._round_has_fill:
            self.state = SessionState.FINISHED
            return
        self._round_trips += 1
        self._round_has_fill = False
        if self.stop_reason is None and self._round_trips >= self.config.effective["max_round_trips"]:
            self.stop_reason = "max_round_trips"
        if self.stop_reason is None and self._past_end():
            self.stop_reason = "session_end"
        if self.stop_reason is not None:
            self.state = SessionState.FINISHED
        else:
            # 一轮完成且未到停止条件：清锚重新进入稳定行情门槛，继续下一轮挂单。
            self._anchor_ticks = None
            self._stable_since = None
            self._outside_since = None
            self.state = SessionState.WAITING_FOR_STABLE_QUOTE

    def _fail(self, reason: str) -> None:
        if self.state in {SessionState.FINISHED, SessionState.FAILED}:
            return
        self.failure_reason = self.failure_reason or reason
        self.state = SessionState.FAILED

    def _find_or_bind_order(self, event: OrderEvent) -> _Order | None:
        order = self._orders.get(event.order_id)
        if order is not None:
            return order
        if event.client_id is None:
            return None
        order = self._pending_clients.pop(event.client_id, None)
        if order is None:
            return None
        order.order_id = event.order_id
        self._orders[event.order_id] = order
        self._client_to_order[event.client_id] = event.order_id
        if order.cancel_requested and event.status not in {"ALLTRADED", "CANCELLED", "REJECTED"}:
            order.cancel_requested = False
            if self.state in {
                SessionState.CLOSING_CANCELS,
                SessionState.CLOSING_RECONCILE,
                SessionState.FLATTENING,
                SessionState.REPLACING,
            }:
                self._emit_cancel(order, safety=self.state != SessionState.REPLACING)
        return order

    def _find_order(self, order_id: str, client_id: str | None) -> _Order | None:
        if order_id and order_id in self._orders:
            return self._orders[order_id]
        if client_id:
            mapped = self._client_to_order.get(client_id)
            if mapped:
                return self._orders.get(mapped)
            return self._pending_clients.get(client_id)
        return None

    def _active_orders(self) -> list[_Order]:
        return [order for order in self._orders.values() if order.active]

    def _flatten_orders_terminal(self) -> bool:
        return not any(order.is_flatten and order.active for order in self._orders.values()) and not any(
            order.is_flatten for order in self._pending_clients.values()
        )

    def _is_target(self, symbol: str, exchange: str) -> bool:
        return symbol == self.target_symbol and exchange.upper() == self.target_exchange

    def _executable_quote(self, tick: TickEvent | None) -> bool:
        if tick is None:
            return False
        values = (tick.bid_price, tick.ask_price)
        return all(isfinite(value) and value > 0 for value in values)

    def _protected_valid(self, tick: TickEvent | None) -> bool:
        if tick is None or self._contract is None:
            return False
        values = (tick.last_price, tick.bid_price, tick.ask_price, self._contract.pricetick)
        if not all(isfinite(value) and value > 0 for value in values):
            return False
        if tick.bid_price > tick.ask_price:
            return False
        spread_ticks = max(
            0,
            int((tick.ask_price - tick.bid_price) / self._contract.pricetick + 0.999999999),
        )
        return self.config.effective["w_ticks"] + self.config.effective["d_ticks"] > self.config.effective["book_protection_multiple"] * spread_ticks

    def _price_ticks(self, price: float) -> int:
        assert self._contract is not None
        return round(price / self._contract.pricetick)

    def _move_anchor(self, current_ticks: int) -> int:
        assert self._anchor_ticks is not None
        w = self.config.effective["w_ticks"]
        step = self.config.effective["s_ticks"]
        anchor = self._anchor_ticks
        while current_ticks < anchor - w:
            anchor -= step
        while current_ticks > anchor + w:
            anchor += step
        return anchor

    def _normal_actions_available(self, count: int) -> bool:
        self._prune_actions()
        return len(self._action_times) + count <= self.config.effective["action_limit_per_minute"]

    def _record_normal_action(self) -> None:
        self._action_times.append(self._now)
        self._action_limit_paused = False

    def _record_late_opening_fill(self, order: _Order) -> None:
        volume = order.traded - order.accounted_opening_traded
        if volume <= 0 or self.final_net_position is None:
            return
        order.accounted_opening_traded = order.traded
        self.final_net_position += volume if order.side == "BUY" else -volume
        if self.state == SessionState.FLATTENING and self.final_net_position == 0:
            if self._flatten_client_id is None:
                self._finish_or_fail()
            else:
                self._cancel_all(safety=True)
        elif self.state == SessionState.FINISHED:
            self.failure_reason = self.failure_reason or "late_opening_fill_after_finish"
            self.state = SessionState.FAILED
        elif self.state == SessionState.FLATTENING and self._flatten_client_id is None:
            self._try_flatten()

    def _prune_actions(self) -> None:
        cutoff = self._now - 60
        while self._action_times and self._action_times[0] <= cutoff:
            self._action_times.popleft()

    def _emit(self, kind: str, **payload: Any) -> None:
        self.actions.append(Action(kind=kind, payload=payload))

    def _account_flatten_fill(self, order: _Order) -> None:
        if not order.is_flatten:
            return
        delta = order.traded - order.accounted_traded
        if delta <= 0:
            return
        order.accounted_traded = order.traded
        if order.side == "BUY":
            self.final_net_position = (self.final_net_position or 0) + delta
        else:
            self.final_net_position = (self.final_net_position or 0) - delta

    @staticmethod
    def _serialize(value: Any) -> Any:
        if is_dataclass(value):
            return {"type": type(value).__name__, "data": asdict(value)}
        if isinstance(value, Action):
            return {"type": "Action", "kind": value.kind, "payload": value.payload}
        return value

    def summary(self) -> dict[str, Any]:
        """Return the final safety summary for the current run."""
        return {
            "terminal_state": self.state.value,
            "target_symbol": self.target_symbol,
            "target_exchange": self.target_exchange,
            "target_lots": self.config.effective["target_lots"],
            "strategy_hash": self.config.sha256,
            "round_trips": self._round_trips,
            "stop_reason": self.stop_reason,
            "startup_position_result": self.startup_position_result,
            "first_fill": self.first_fill,
            "cancellation_terminal": self.cancellation_terminal,
            "closing_position_request_id": self.closing_position_request_id,
            "closing_position_result": self.closing_position_result,
            "flatten_attempts": list(self.flatten_attempts),
            "final_net_position": self.final_net_position,
            "active_order_count": len(self._active_orders()) + len(self._pending_clients),
            "active_orders": [
                {
                    "order_id": order.order_id,
                    "client_id": order.client_id,
                    "side": order.side,
                    "volume": order.volume,
                    "traded": order.traded,
                    "status": order.status,
                    "price": order.price,
                }
                for order in self._active_orders()
            ]
            + [
                {
                    "order_id": order.order_id,
                    "client_id": order.client_id,
                    "side": order.side,
                    "volume": order.volume,
                    "traded": order.traded,
                    "status": order.status,
                    "price": order.price,
                }
                for order in self._pending_clients.values()
            ],
            "state_transitions": list(self.state_transitions),
            "failure_reason": self.failure_reason,
        }


LiveGridEvent = (
    ContractEvent
    | TickEvent
    | OrderEvent
    | TradeEvent
    | PositionQueryCompleteEvent
    | ClockEvent
    | InterruptEvent
)
