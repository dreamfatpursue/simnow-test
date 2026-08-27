"""Deterministic live-grid session driven by external CTP facts."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta
from enum import Enum
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from .config import StrategyConfig


CHINA_TZ = ZoneInfo("Asia/Shanghai")


class SessionState(str, Enum):
    PREVIEW = "PREVIEW"
    WAITING_FOR_CONTRACT = "WAITING_FOR_CONTRACT"
    WAITING_FOR_ZERO_POSITION = "WAITING_FOR_ZERO_POSITION"
    WAITING_FOR_STABLE_QUOTE = "WAITING_FOR_STABLE_QUOTE"
    PAUSED = "PAUSED"
    QUOTE_PENDING = "QUOTE_PENDING"
    QUOTING = "QUOTING"
    REPLACING = "REPLACING"
    CLOSING_WAIT = "CLOSING_WAIT"
    CLOSING_CANCELS = "CLOSING_CANCELS"
    CLOSING_RECONCILE = "CLOSING_RECONCILE"
    FLATTENING = "FLATTENING"
    RISK_HOLD = "RISK_HOLD"
    FINISHED = "FINISHED"
    FAILED = "FAILED"


# CTP OnErrRtnOrderAction 错误码：报单已全成交或已撤销，不能再撤。
# 收到它说明该委托在交易所侧已是终态，不是需要风险托管的异常。
_CTP_ORDER_ALREADY_TERMINAL_ERROR_ID = 26



# 收口中与终态的合集：处于其中任何状态时不得开启新一轮收口或报价。
_CLOSING_OR_TERMINAL = frozenset(
    {
        SessionState.CLOSING_WAIT,
        SessionState.CLOSING_CANCELS,
        SessionState.CLOSING_RECONCILE,
        SessionState.FLATTENING,
        SessionState.RISK_HOLD,
        SessionState.FINISHED,
        SessionState.FAILED,
    }
)


@dataclass(frozen=True)
class ContractEvent:
    symbol: str
    exchange: str
    pricetick: float
    size: float | None = None
    product_code: str | None = None


@dataclass(frozen=True)
class TickEvent:
    symbol: str
    exchange: str
    last_price: float
    bid_price: float
    ask_price: float
    at: float
    exchange_time: str | None = None
    trading_day: str | None = None
    action_day: str | None = None
    update_millisec: int | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    receive_sequence: int | None = None


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
    exchange_time: str | None = None
    offset: str = "OPEN"
    order_ref: str | None = None
    order_sys_id: str | None = None
    status_unknown: bool = False


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
    exchange_time: str | None = None
    at: float | None = None


@dataclass(frozen=True)
class PositionQueryCompleteEvent:
    request_id: str
    symbol: str
    exchange: str
    net_position: int
    error_id: int = 0
    error_msg: str = ""


@dataclass(frozen=True)
class ConnectionEvent:
    kind: str
    connected: bool
    reason: str = ""


@dataclass(frozen=True)
class OrderActionErrorEvent:
    order_id: str
    symbol: str
    exchange: str
    client_id: str | None = None
    error_id: int = 0
    error_msg: str = ""
    action: str = "cancel"


@dataclass(frozen=True)
class OrderQueryCompleteEvent:
    request_id: str
    symbol: str
    exchange: str
    orders: tuple[OrderEvent, ...] = ()
    error_id: int = 0
    error_msg: str = ""


@dataclass(frozen=True)
class TradeQueryCompleteEvent:
    request_id: str
    symbol: str
    exchange: str
    trades: tuple[Any, ...] = ()
    error_id: int = 0
    error_msg: str = ""


@dataclass(frozen=True)
class ClockEvent:
    at: float
    # 适配层提供本地墙钟时间；测试 seam 可以省略，使用逻辑时钟。
    wall_time: str | None = None


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
    replay_market_data: bool = False
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
    _audit_trace: list[dict[str, Any]] = field(default_factory=list, init=False)
    state_transitions: list[dict[str, str]] = field(default_factory=list, init=False)
    startup_position_request_id: str | None = field(default=None, init=False)
    closing_position_request_id: str | None = field(default=None, init=False)
    closing_position_result: dict[str, Any] | None = field(default=None, init=False)
    _contract: ContractEvent | None = field(default=None, init=False)
    _latest_tick: TickEvent | None = field(default=None, init=False)
    _last_exchange_tick_time: datetime | None = field(default=None, init=False)
    _last_exchange_tick_key: tuple[str, int] | None = field(default=None, init=False)
    _quote_group_id: str | None = field(default=None, init=False)
    _quote_acknowledged: set[str] = field(default_factory=set, init=False)
    _quote_ack_deadline: float | None = field(default=None, init=False)
    _quote_ack_query_request_id: str | None = field(default=None, init=False)
    _quote_ack_query_started_at: float | None = field(default=None, init=False)
    _quote_order_callbacks_seen: bool = field(default=False, init=False)
    _cancel_attempt_at: dict[str, float] = field(default_factory=dict, init=False)
    _cancel_error_ids: dict[str, str] = field(default_factory=dict, init=False)
    _risk_recovery_pending: bool = field(default=False, init=False)
    _risk_blocked_order: bool = field(default=False, init=False)
    _startup_orders_checked: bool = field(default=False, init=False)
    _resume_after_reconcile: bool = field(default=False, init=False)
    _last_recovery_query_at: float | None = field(default=None, init=False)
    _stable_since: float | None = field(default=None, init=False)
    _stable_tick_count: int = field(default=0, init=False)
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
    _replacement_client_ids: list[str] = field(default_factory=list, init=False)
    _next_quote_context: dict[str, Any] | None = field(default=None, init=False)
    _action_times: deque[float] = field(default_factory=deque, init=False)
    _action_limit_paused: bool = field(default=False, init=False)
    _replacement_started_at: float | None = field(default=None, init=False)
    _replacement_warning_emitted: bool = field(default=False, init=False)
    _round_trips: int = field(default=0, init=False)
    _round_has_fill: bool = field(default=False, init=False)
    _round_open_net: int = field(default=0, init=False)
    _window_started_at: float | None = field(default=None, init=False)
    _schedule_windows: tuple[tuple[datetime, datetime], ...] = field(default=(), init=False)
    _schedule_initialized: bool = field(default=False, init=False)
    _last_wall_time: datetime | None = field(default=None, init=False)
    _window_close_kind: str | None = field(default=None, init=False)
    _created_wall_time: datetime = field(
        default_factory=lambda: datetime.now(CHINA_TZ).replace(tzinfo=None),
        init=False,
    )

    _PRE_CLOSE_SECONDS = 5

    def __post_init__(self) -> None:
        self.state = (
            SessionState.WAITING_FOR_CONTRACT
            if self.config.can_submit(
                simnow_confirmed=self.simnow_confirmed,
            )
            else SessionState.PREVIEW
        )
        self.state_transitions.append({"from": "", "to": self.state.value})

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
        self._audit_trace = []
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
        elif isinstance(event, ConnectionEvent):
            self._on_connection(event)
        elif isinstance(event, OrderActionErrorEvent):
            self._on_order_action_error(event)
        elif isinstance(event, OrderQueryCompleteEvent):
            self._on_order_query_complete(event)
        elif isinstance(event, TradeQueryCompleteEvent):
            self._on_trade_query_complete(event)
        elif isinstance(event, ClockEvent):
            self._on_clock(event)
        elif isinstance(event, InterruptEvent):
            self._on_interrupt()
        else:
            raise TypeError(f"不支持的实时网格事件: {type(event).__name__}")
        new_actions = self.actions[start:]
        if before != self.state:
            self.state_transitions.append({"from": before.value, "to": self.state.value})
        audit_record = {
            "at": self._now,
            "event": self._serialize(event),
            "state_before": before.value,
            "state_after": self.state.value,
            "actions": [self._serialize(action) for action in new_actions],
        }
        if self._audit_trace:
            audit_record["trace"] = self._serialize(self._audit_trace)
        self.audit_events.append(audit_record)
        return new_actions

    @property
    def last_audit_trace(self) -> tuple[dict[str, Any], ...]:
        """Return the latest event's structured causal trace for the audit writer."""
        if not self.audit_events:
            return ()
        return tuple(self.audit_events[-1].get("trace", ()))

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
        self._record_trace(
            "contract_metadata",
            market={
                "symbol": event.symbol,
                "exchange": event.exchange,
                "pricetick": event.pricetick,
                "size": event.size,
                "product_code": event.product_code,
            },
            calculation={"startup_position_request_id": self.startup_position_request_id},
        )
        self._record_trace(
            "startup_position_query",
            calculation={
                "request_id": self.startup_position_request_id,
                "phase": "startup",
                "net_position_required": 0,
            },
        )

    def _reset_stable_quote_gate(self) -> None:
        self._stable_since = None
        self._stable_tick_count = 0

    def _tick_gap_seconds(self, previous: TickEvent | None, current: TickEvent | None) -> float:
        if previous is None or current is None:
            return float("inf")
        previous_exchange = self._exchange_datetime(previous.exchange_time)
        current_exchange = self._exchange_datetime(current.exchange_time)
        if previous_exchange is not None and current_exchange is not None:
            return max(0.0, (current_exchange - previous_exchange).total_seconds())
        return max(0.0, current.at - previous.at)

    def _begin_stable_quote_gate(self, at: float) -> None:
        self._stable_since = at
        self._stable_tick_count = 1

    def _exchange_datetime(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            parsed = None
            for pattern in ("%Y%m%d %H:%M:%S.%f", "%Y%m%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(value, pattern)
                    break
                except (TypeError, ValueError):
                    continue
            if parsed is None:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed

    def _tick_time_valid(self, event: TickEvent) -> tuple[bool, str]:
        """Validate exchange ordering/age; ``at`` remains only a callback clock."""
        exchange_time = self._exchange_datetime(event.exchange_time)
        if event.exchange_time and exchange_time is None:
            return False, "invalid_exchange_time"
        if event.update_millisec is not None and (
            isinstance(event.update_millisec, bool)
            or not isinstance(event.update_millisec, int)
            or not 0 <= event.update_millisec <= 999
        ):
            return False, "invalid_update_millisec"
        if exchange_time is None:
            # Unit-test seam and old gateways have no exchange timestamp. The live
            # adapter always supplies one from CTP.
            return True, "local_clock_seam"
        if self._last_exchange_tick_time is not None and exchange_time <= self._last_exchange_tick_time:
            return False, "exchange_time_not_increasing"
        if not self.replay_market_data:
            now = datetime.now().astimezone()
            age = (now - exchange_time).total_seconds()
            if age > float(self.config.effective["max_tick_age_seconds"]):
                return False, "exchange_time_stale"
            if age < -2:
                return False, "exchange_time_future"
        return True, "exchange_time_valid"

    def _on_tick(self, event: TickEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        valid_time, time_reason = self._tick_time_valid(event)
        if not valid_time:
            self._record_trace(
                "tick_rejected",
                market={"exchange_time": event.exchange_time, "receive_sequence": event.receive_sequence},
                calculation={"reason": time_reason, "max_tick_age_seconds": self.config.effective["max_tick_age_seconds"]},
            )
            if self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
                self._reset_stable_quote_gate()
            elif self.state in {SessionState.QUOTE_PENDING, SessionState.QUOTING}:
                self._begin_replacement("invalid_market_time", safety=True)
            return
        self._now = max(self._now, event.at)
        previous = self._latest_tick
        self._latest_tick = event
        exchange_time = self._exchange_datetime(event.exchange_time)
        if exchange_time is not None:
            self._last_exchange_tick_time = exchange_time
            self._last_exchange_tick_key = (event.exchange_time or "", event.update_millisec or 0)

        if self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            if not self._protected_valid(event):
                self._reset_stable_quote_gate()
                return
            max_age = self.config.effective["max_tick_age_seconds"]
            if self._stable_since is None:
                self._begin_stable_quote_gate(event.at)
            elif previous is None or self._tick_gap_seconds(previous, event) > max_age:
                self._begin_stable_quote_gate(event.at)
            else:
                self._stable_tick_count += 1
            return

        if self.state == SessionState.QUOTE_PENDING:
            if event.exchange_time is None and not self._quote_order_callbacks_seen:
                # Compatibility seam for deterministic tests/old adapters. Live
                # CTP ticks carry exchange_time and therefore require both acks.
                self.state = SessionState.QUOTING
            return

        if self.state == SessionState.QUOTING:
            protection = self._book_protection_facts(event)
            if not protection["passed"]:
                self._record_trace(
                    "market_pause",
                    client_ids=[order.client_id for order in self._active_orders()] + list(self._pending_clients),
                    market={
                        "last_price": event.last_price,
                        "bid_price": event.bid_price,
                        "ask_price": event.ask_price,
                        "limit_up": event.limit_up,
                        "limit_down": event.limit_down,
                    },
                    calculation=protection,
                )
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
                old_anchor_ticks = self._anchor_ticks
                outside_since = self._outside_since
                new_anchor_ticks = self._move_anchor(current_ticks)
                self._record_trace(
                    "reanchor",
                    client_ids=[order.client_id for order in self._active_orders()] + list(self._pending_clients),
                    market={
                        "last_price": event.last_price,
                        "bid_price": event.bid_price,
                        "ask_price": event.ask_price,
                    },
                    calculation={
                        "old_anchor_ticks": old_anchor_ticks,
                        "old_anchor_price": old_anchor_ticks * self._contract.pricetick,
                        "new_anchor_ticks": new_anchor_ticks,
                        "new_anchor_price": new_anchor_ticks * self._contract.pricetick,
                        "current_ticks": current_ticks,
                        "w_ticks": self.config.effective["w_ticks"],
                        "s_ticks": self.config.effective["s_ticks"],
                        "lower_bound_ticks": low,
                        "upper_bound_ticks": high,
                        "outside_since": outside_since,
                        "elapsed_seconds": event.at - outside_since,
                        "confirmation_seconds": self.config.effective["reanchor_confirmation_seconds"],
                    },
                )
                self._anchor_ticks = new_anchor_ticks
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
        if self.state == SessionState.QUOTE_PENDING:
            self._quote_order_callbacks_seen = True
        previous_traded = order.traded
        previous_status = order.status
        if event.status_unknown or event.status not in {"SUBMITTING", "ACCEPTED", "NOTTRADED", "PARTTRADED", "ALLTRADED", "CANCELLED", "REJECTED"}:
            self._risk_hold("order_status_unknown")
        if not order.terminal:
            order.status = event.status
        order.volume = event.volume or order.volume
        order.price = event.price or order.price
        order.traded = max(order.traded, event.traded)
        self._account_flatten_fill(order)
        traded_delta = order.traded - previous_traded

        if event.status != previous_status or traded_delta > 0:
            self._record_trace(
                "order_status",
                client_ids=[order.client_id],
                calculation={
                    "order_id": order.order_id,
                    "previous_status": previous_status,
                    "status": event.status,
                    "traded": order.traded,
                    "traded_delta": traded_delta,
                    "volume": order.volume,
                    "exchange_time": event.exchange_time,
                },
            )

        if order.is_flatten:
            if event.status == "REJECTED":
                self._record_trace(
                    "flatten_rejected",
                    client_ids=[order.client_id],
                    calculation={
                        "order_id": order.order_id,
                        "status": event.status,
                        "traded": order.traded,
                        "volume": order.volume,
                        "reason": "ctp_rejected",
                    },
                )
            elif event.status in {"ALLTRADED", "CANCELLED"}:
                self._record_trace(
                    "flatten_terminal",
                    client_ids=[order.client_id],
                    calculation={
                        "order_id": order.order_id,
                        "status": event.status,
                        "traded": order.traded,
                        "volume": order.volume,
                    },
                )
            if self.state not in _CLOSING_OR_TERMINAL:
                if traded_delta > 0:
                    # 迟到的平仓成交改变了净仓：不得带仓续挂，立即开启新一轮收口。
                    self._enter_closing("late_fill", order=order, volume=traded_delta)
                return
            if self.state == SessionState.FINISHED and self.final_net_position != 0:
                self.failure_reason = self.failure_reason or "late_flatten_fill_after_finish"
                self._record_trace(
                    "late_flatten_fill",
                    client_ids=[order.client_id],
                    calculation={
                        "order_id": order.order_id,
                        "status": event.status,
                        "traded": order.traded,
                        "net_position": self.final_net_position,
                        "reason": self.failure_reason,
                    },
                )
                self._risk_hold("late_flatten_fill_after_finish")
            if self.state == SessionState.FLATTENING:
                if event.status == "REJECTED":
                    self._fail("flatten_rejected")
                    self._cancel_all(safety=True)
                    if self.state in {SessionState.FAILED, SessionState.RISK_HOLD}:
                        return
                elif order.terminal and order.client_id == self._flatten_client_id:
                    self._flatten_client_id = None
                if self.final_net_position == 0:
                    if self._flatten_orders_terminal():
                        self._cancel_remaining_and_reconcile()
                    else:
                        self._cancel_all(safety=True)
                elif self._flatten_client_id is None:
                    self._try_flatten()
        else:
            if traded_delta > 0:
                self._handle_opening_fill(
                    order,
                    traded_delta,
                    price=event.price,
                    exchange_time=event.exchange_time,
                )
            if event.status == "REJECTED" and self.state in {SessionState.QUOTE_PENDING, SessionState.QUOTING}:
                self._quote_pair_failed("opening_order_rejected", order)
            elif (
                self.state == SessionState.QUOTE_PENDING
                and self._quote_ack_query_request_id is None
                and event.status in {"ACCEPTED", "NOTTRADED", "PARTTRADED"}
            ):
                self._quote_acknowledged.add(order.client_id)
                if self._quote_group_id and self._quote_acknowledged >= {
                    f"quote-{self._quote_group_id}-buy",
                    f"quote-{self._quote_group_id}-sell",
                }:
                    self._quote_ack_deadline = None
                    self.state = SessionState.QUOTING
                    self._record_trace(
                        "quote_pair_accepted",
                        client_ids=sorted(self._quote_acknowledged),
                        calculation={"quote_group_id": self._quote_group_id},
                    )

        if self.state == SessionState.REPLACING:
            self._maybe_finish_replacement()
        elif self.state == SessionState.CLOSING_CANCELS:
            self._maybe_reconcile()

    def _on_trade(self, event: TradeEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        if event.at is not None:
            self._now = max(self._now, event.at)
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
        net_before = self.final_net_position
        self._account_flatten_fill(order)
        if order.is_flatten:
            if order.accounted_traded > accounted_before:
                self._record_trace(
                    "flatten_fill",
                    client_ids=[order.client_id],
                    calculation={
                        "order_id": order.order_id,
                        "trade_id": event.trade_id,
                        "exchange_time": event.exchange_time,
                        "side": event.side,
                        "volume": event.volume,
                        "price": event.price,
                        "net_position_before": net_before,
                        "net_position_after": self.final_net_position,
                    },
                )
            if self.state not in _CLOSING_OR_TERMINAL:
                if order.accounted_traded > accounted_before:
                    # 迟到的平仓成交改变了净仓：不得带仓续挂，立即开启新一轮收口。
                    self._enter_closing(
                        "late_fill",
                        order=order,
                        volume=order.accounted_traded - accounted_before,
                        price=event.price,
                        trade_id=event.trade_id,
                        exchange_time=event.exchange_time,
                    )
                return
            current_flatten_terminal = order.client_id == self._flatten_client_id and order.terminal
            if self.state == SessionState.FINISHED and self.final_net_position != 0:
                self.failure_reason = self.failure_reason or "late_flatten_fill_after_finish"
                self._record_trace(
                    "late_flatten_fill",
                    client_ids=[order.client_id],
                    calculation={
                        "order_id": order.order_id,
                        "trade_id": event.trade_id,
                        "volume": event.volume,
                        "net_position": self.final_net_position,
                        "reason": self.failure_reason,
                    },
                )
                self._risk_hold("late_flatten_fill_after_finish")
            if order.client_id == self._flatten_client_id and order.terminal:
                self._flatten_client_id = None
            if self.final_net_position == 0:
                if self._flatten_orders_terminal():
                    self._cancel_remaining_and_reconcile()
                else:
                    self._cancel_all(safety=True)
            elif current_flatten_terminal:
                self._try_flatten()
            return
        self._handle_opening_fill(
            order,
            event.volume,
            price=event.price,
            trade_id=event.trade_id,
            exchange_time=event.exchange_time,
        )

    def _on_position_query_complete(self, event: PositionQueryCompleteEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        if self.state == SessionState.WAITING_FOR_ZERO_POSITION:
            if event.request_id != self.startup_position_request_id:
                return
            if event.error_id:
                self._record_trace(
                    "startup_position_result",
                    calculation={
                        "request_id": event.request_id,
                        "net_position": None,
                        "error_id": event.error_id,
                        "passed": False,
                    },
                )
                self._risk_hold("startup_position_query_failed")
                return
            self._record_trace(
                "startup_position_result",
                calculation={
                    "request_id": event.request_id,
                    "net_position": event.net_position,
                    "error_id": event.error_id,
                    "passed": event.net_position == 0,
                },
            )
            if event.net_position != 0:
                self.startup_position_result = "nonzero"
                self.final_net_position = event.net_position
                self._record_trace(
                    "startup_position_rejected",
                    calculation={"request_id": event.request_id, "net_position": event.net_position},
                )
                self._risk_hold("nonzero_startup_position")
            else:
                self.startup_position_result = "zero"
                self.final_net_position = 0
                self.state = SessionState.WAITING_FOR_STABLE_QUOTE
                self._reset_stable_quote_gate()
                self._record_trace(
                    "zero_position_confirmed",
                    calculation={"request_id": event.request_id, "net_position": 0, "next_state": self.state.value},
                )
            return

        if self.state == SessionState.RISK_HOLD:
            if event.error_id:
                self.final_net_position = None
                return
            self.final_net_position = event.net_position
            if event.net_position == 0 and not self._risk_blocked_order and not self._active_orders() and not self._pending_clients:
                previous_reason = self.failure_reason
                self.failure_reason = None
                self._risk_recovery_pending = False
                # 托管期间已完成的开平成交也要在这里记账：终端回报链被中断跳过时，
                # 漏记会让状态机为凑满 max_round_trips 多跑一整对报撤。
                if self._round_has_fill:
                    self._round_trips += 1
                    self._round_has_fill = False
                if self.stop_reason is None and self._round_trips >= self.config.effective["max_round_trips"]:
                    self.stop_reason = "max_round_trips"
                self._reset_stable_quote_gate()
                self._anchor_ticks = None
                if self.stop_reason == "max_round_trips":
                    self.state = SessionState.FINISHED
                else:
                    self.state = SessionState.WAITING_FOR_STABLE_QUOTE
                self._record_trace(
                    "risk_hold_recovered",
                    calculation={
                        "previous_reason": previous_reason,
                        "net_position": 0,
                        "round_trips": self._round_trips,
                        "next_state": self.state.value,
                    },
                )
            return

        if self.state != SessionState.CLOSING_RECONCILE:
            return
        if event.request_id != self.closing_position_request_id:
            return
        if event.error_id:
            self._record_trace(
                "closing_position_result",
                calculation={
                    "request_id": event.request_id,
                    "net_position": None,
                    "error_id": event.error_id,
                    "passed": False,
                },
            )
            self.closing_position_result = {
                "request_id": event.request_id,
                "net_position": None,
                "error_id": event.error_id,
                "error_msg": event.error_msg,
            }
            # 查仓失败即净仓未知：不得沿用窗口记账的旧值。
            self.final_net_position = None
            self._risk_hold("closing_position_query_failed")
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
        self._record_trace(
            "closing_position_result",
            calculation={
                "request_id": event.request_id,
                "net_position": event.net_position,
                "error_id": event.error_id,
                "passed": event.net_position == 0,
            },
        )
        if event.net_position == 0:
            if self._resume_after_reconcile:
                self._resume_after_reconcile = False
                self._anchor_ticks = None
                self._reset_stable_quote_gate()
                self.state = SessionState.WAITING_FOR_STABLE_QUOTE
                return
            self._finish_or_fail()
            return
        self._round_has_fill = True
        self.state = SessionState.FLATTENING
        self._flatten_started_at = self._now
        self._try_flatten()

    def _on_connection(self, event: ConnectionEvent) -> None:
        if event.connected:
            if self.state == SessionState.RISK_HOLD:
                self._risk_recovery_pending = True
                self._record_trace("connection_recovered", calculation={"kind": event.kind})
            return
        if self.state in {SessionState.FINISHED, SessionState.PREVIEW}:
            return
        self._risk_hold(f"{event.kind}_front_disconnected")

    def _on_order_action_error(self, event: OrderActionErrorEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        order = self._find_order(event.order_id, event.client_id)
        if order is None:
            self._risk_hold("order_action_unknown")
            return
        if event.action == "insert":
            self._record_trace(
                "order_insert_error",
                client_ids=[order.client_id],
                calculation={"order_id": order.order_id, "error_id": event.error_id, "error_msg": event.error_msg},
            )
            if self.state in {SessionState.QUOTE_PENDING, SessionState.QUOTING}:
                self._quote_pair_failed("order_insert_failed", order)
            return
        if event.action != "cancel":
            self._risk_hold("order_action_unknown")
            return
        order.cancel_requested = False
        self._cancel_error_ids[order.client_id] = event.error_msg or str(event.error_id)
        self._record_trace(
            "cancel_error",
            client_ids=[order.client_id],
            calculation={"order_id": order.order_id, "error_id": event.error_id, "error_msg": event.error_msg},
        )
        if event.error_id == _CTP_ORDER_ALREADY_TERMINAL_ERROR_ID:
            # CTP 错误26=报单已全成交或已撤销：这本身就是该委托已终态的证明。
            # 终端回报可能在途，等它到达或走撤单超时对账即可；
            # 升级 RISK_HOLD 只会触发整套恢复链并重复撤同一张已死订单。
            return
        if self.state not in {SessionState.FINISHED, SessionState.FAILED, SessionState.PREVIEW}:
            self._risk_hold("cancel_failed")

    def _on_order_query_complete(self, event: OrderQueryCompleteEvent) -> None:
        if not self._is_target(event.symbol, event.exchange):
            return
        if self._quote_ack_query_request_id == event.request_id:
            self._on_quote_ack_query_complete(event)
            return
        if event.error_id:
            self._risk_hold("order_query_failed")
            return
        unknown = [
            order for order in event.orders
            if getattr(order, "status", "") not in {"SUBMITTING", "ACCEPTED", "NOTTRADED", "PARTTRADED", "ALLTRADED", "CANCELLED", "REJECTED"}
        ]
        if unknown:
            self._risk_blocked_order = True
            self._risk_hold("startup_unknown_order_status")
            return
        active_open = [
            order for order in event.orders
            if getattr(order, "offset", "OPEN") in {"OPEN", "开仓"}
            and getattr(order, "status", "") not in {"ALLTRADED", "CANCELLED", "REJECTED"}
        ]
        active_close = [
            order for order in event.orders
            if getattr(order, "offset", "OPEN") not in {"OPEN", "开仓"}
            and getattr(order, "status", "") not in {"ALLTRADED", "CANCELLED", "REJECTED"}
        ]
        if active_close:
            self._risk_blocked_order = True
            self._risk_hold("startup_active_close_order")
            return
        if active_open:
            for order in active_open:
                self._record_trace(
                    "orphan_open_order",
                    client_ids=[getattr(order, "client_id", None)],
                    calculation={"order_id": getattr(order, "order_id", "")},
                )
                self._emit(
                    "cancel_order",
                    order_id=getattr(order, "order_id", ""),
                    client_id=getattr(order, "client_id", None),
                    symbol=self.target_symbol,
                    exchange=self.target_exchange,
                    safety=True,
                )
            self._startup_orders_checked = False
            return
        self._risk_blocked_order = False
        self._startup_orders_checked = True

    def _on_quote_ack_query_complete(self, event: OrderQueryCompleteEvent) -> None:
        """Reconcile a timed-out quote pair before deciding whether it is safe to quote."""
        self._quote_ack_query_request_id = None
        self._quote_ack_query_started_at = None
        if event.error_id:
            self._risk_hold("quote_ack_order_query_failed")
            return
        if self.state != SessionState.QUOTE_PENDING:
            return

        expected_clients = {
            f"quote-{self._quote_group_id}-buy",
            f"quote-{self._quote_group_id}-sell",
        }
        observed: dict[str, str] = {}
        for queried in event.orders:
            order = self._find_order(queried.order_id, queried.client_id)
            if order is not None and order.client_id in expected_clients:
                observed[order.client_id] = queried.status
            self._on_order(queried)
            if self.state != SessionState.QUOTE_PENDING:
                return

        accepted = {"ACCEPTED", "NOTTRADED", "PARTTRADED"}
        current_status = {
            client_id: next(
                (order.status for order in self._orders.values() if order.client_id == client_id),
                "UNKNOWN",
            )
            for client_id in expected_clients
        }
        if (
            set(observed) == expected_clients
            and all(status in accepted for status in observed.values())
            and current_status == observed
        ):
            self._quote_acknowledged.update(expected_clients)
            self._quote_ack_deadline = None
            self.state = SessionState.QUOTING
            self._record_trace(
                "quote_pair_accepted_via_query",
                client_ids=sorted(expected_clients),
                calculation={"quote_group_id": self._quote_group_id, "request_id": event.request_id},
            )
            return

        self._quote_pair_failed("quote_ack_reconcile_failed")
        if self._pending_clients:
            self._risk_hold("quote_ack_query_unknown_order")

    def _on_trade_query_complete(self, event: TradeQueryCompleteEvent) -> None:
        if event.error_id:
            self._risk_hold("trade_query_failed")

    def _quote_pair_failed(self, reason: str, order: _Order | None = None) -> None:
        self._record_trace(
            "quote_pair_failed",
            client_ids=[order.client_id] if order is not None else [],
            calculation={"reason": reason, "quote_group_id": self._quote_group_id},
        )
        self._quote_ack_deadline = None
        self._quote_ack_query_request_id = None
        self._quote_ack_query_started_at = None
        self._resume_after_reconcile = True
        self.final_net_position = None
        self.state = SessionState.CLOSING_CANCELS
        self._closing_started_at = self._now
        self.cancellation_terminal = None
        self._cancel_all(safety=True)
        self._maybe_reconcile()

    def _risk_hold(self, reason: str) -> None:
        self.failure_reason = self.failure_reason or reason
        self.state = SessionState.RISK_HOLD
        self._risk_recovery_pending = True
        self._record_trace(
            "risk_hold",
            client_ids=[order.client_id for order in self._active_orders()] + list(self._pending_clients),
            calculation={
                "reason": self.failure_reason,
                "active_order_count": len(self._active_orders()) + len(self._pending_clients),
                "net_position": self.final_net_position,
            },
        )
        self._cancel_all(safety=True)

    def _parse_wall_time(self, value: str | None) -> datetime | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            self._fail(f"invalid_clock_wall_time:{value}")
            self._record_trace("invalid_clock_wall_time", calculation={"value": value, "error": str(exc)})
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(CHINA_TZ).replace(tzinfo=None)
        return parsed

    @staticmethod
    def _wall_clock_aware(value: datetime) -> datetime:
        return value.replace(tzinfo=CHINA_TZ) if value.tzinfo is None else value.astimezone(CHINA_TZ)

    def _build_schedule(self, base_date) -> tuple[tuple[datetime, datetime], ...]:
        previous_start: int | None = None
        day_offset = 0
        schedule: list[tuple[datetime, datetime]] = []
        for window in self.config.effective["quote_windows"]:
            start_text = window["start"]
            end_text = window["end"]
            start = sum(int(part) * factor for part, factor in zip(start_text.split(":"), (60, 1)))
            end = sum(int(part) * factor for part, factor in zip(end_text.split(":"), (60, 1)))
            if previous_start is not None and start < previous_start:
                day_offset += 1
            start_at = datetime.combine(base_date + timedelta(days=day_offset), datetime.min.time()).replace(
                hour=start // 60,
                minute=start % 60,
            )
            end_day_offset = day_offset + (1 if end <= start else 0)
            end_at = datetime.combine(
                base_date + timedelta(days=end_day_offset), datetime.min.time()
            ).replace(hour=end // 60, minute=end % 60)
            schedule.append((start_at, end_at))
            previous_start = start
        return tuple(schedule)

    def _initialize_schedule(self, wall_time: datetime) -> None:
        if self._schedule_initialized:
            return
        self._schedule_initialized = True

        # 允许在当前窗口内启动；跨午夜时，优先选择仍覆盖当前时刻的上一自然日排程。
        current_schedule = self._build_schedule(wall_time.date())
        previous_schedule = self._build_schedule(wall_time.date() - timedelta(days=1))
        self._schedule_windows = next(
            (
                candidate
                for candidate in (current_schedule, previous_schedule)
                if any(start_at <= wall_time < end_at for start_at, end_at in candidate)
            ),
            current_schedule,
        )

    def _window_phase(self, wall_time: datetime | None) -> str:
        """Return open/preclose/pause/final for the configured run.

        ClockEvent without wall_time is the deterministic unit-test seam and keeps
        the schedule open; the live adapter always supplies the local wall clock.
        """
        if wall_time is None:
            return "open"
        self._initialize_schedule(wall_time)
        if self.state == SessionState.FAILED:
            return "invalid"
        for index, (start_at, end_at) in enumerate(self._schedule_windows):
            if wall_time < start_at:
                return "before" if index == 0 else "pause"
            if wall_time < end_at:
                remaining = (end_at - wall_time).total_seconds()
                if remaining <= self._PRE_CLOSE_SECONDS:
                    return "final_preclose" if index == len(self._schedule_windows) - 1 else "preclose"
                return "open"
        return "final_closed"

    def _missed_window_close(self, previous: datetime | None, current: datetime | None) -> str | None:
        # 首个墙钟回调只定位启动时刻，不能把本次启动前结束的窗口算作跨窗。
        if previous is None or current is None or not self._schedule_windows:
            return None
        crossed: list[int] = []
        for index, (_, end_at) in enumerate(self._schedule_windows):
            if current < end_at:
                continue
            if previous < end_at <= current:
                crossed.append(index)
        if not crossed:
            return None
        return "final" if crossed[-1] == len(self._schedule_windows) - 1 else "pause"

    def _begin_window_close(self, *, final: bool) -> None:
        close_kind = "final" if final else "pause"
        if self.state in {SessionState.QUOTE_PENDING, SessionState.QUOTING, SessionState.REPLACING}:
            self._window_close_kind = close_kind
            self._enter_closing("quote_window_end")
        elif self.state == SessionState.CLOSING_WAIT:
            self._window_close_kind = close_kind
            self._cancel_remaining_and_reconcile(reason="quote_window_end")
        elif self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            if final:
                self.stop_reason = "quote_window_end"
                self.state = SessionState.FINISHED
            else:
                self._reset_for_next_window()
                self.state = SessionState.PAUSED
        elif self.state in {SessionState.WAITING_FOR_CONTRACT, SessionState.WAITING_FOR_ZERO_POSITION}:
            if final:
                self._risk_hold("quote_window_end_before_startup_reconcile")
        elif self.state in {SessionState.PREVIEW, SessionState.PAUSED}:
            if final:
                self.stop_reason = "quote_window_end"
                self.state = SessionState.FINISHED

    def _reset_for_next_window(self) -> None:
        self._anchor_ticks = None
        self._outside_since = None
        self._reset_stable_quote_gate()
        self.state = SessionState.WAITING_FOR_STABLE_QUOTE

    def _quote_is_stale(self) -> bool:
        if self._latest_tick is None:
            return True
        exchange_time = self._exchange_datetime(self._latest_tick.exchange_time)
        if exchange_time is not None and self._last_wall_time is not None and not self.replay_market_data:
            age = (self._wall_clock_aware(self._last_wall_time) - exchange_time.astimezone(CHINA_TZ)).total_seconds()
        else:
            age = self._now - self._latest_tick.at
        return age > float(self.config.effective["max_tick_age_seconds"])

    def _on_clock(self, event: ClockEvent) -> None:
        self._now = max(self._now, event.at)
        self._prune_actions()

        wall_time = self._parse_wall_time(event.wall_time)
        phase = self._window_phase(wall_time)
        previous_wall_time = self._last_wall_time
        self._last_wall_time = wall_time
        if self.state == SessionState.FAILED:
            return
        if self.state == SessionState.RISK_HOLD:
            self._cancel_all(safety=True)
            if self._last_recovery_query_at is None or self._now - self._last_recovery_query_at >= 2:
                self._request_sequence += 1
                request_id = f"recovery-{self._request_sequence}"
                self._last_recovery_query_at = self._now
                self._emit(
                    "query_position",
                    request_id=request_id,
                    symbol=self.target_symbol,
                    exchange=self.target_exchange,
                    phase="recovery",
                )
            return
        if phase == "invalid":
            return

        missed_close = self._missed_window_close(previous_wall_time, wall_time)
        if phase in {"final_preclose", "final_closed"}:
            missed_close = "final"
        closing_states = {
            SessionState.CLOSING_CANCELS,
            SessionState.CLOSING_RECONCILE,
            SessionState.FLATTENING,
        }
        if missed_close is not None and self.state in closing_states:
            self._window_close_kind = "final" if missed_close == "final" else "pause"
        if missed_close is not None and self.state in {
            SessionState.QUOTING,
            SessionState.REPLACING,
            SessionState.CLOSING_WAIT,
            SessionState.WAITING_FOR_STABLE_QUOTE,
        }:
            self._begin_window_close(final=missed_close == "final")
            return

        final_phase = phase in {"final_preclose", "final_closed"}
        outside_phase = phase in {"before", "pause", "preclose", "final_preclose", "final_closed"}
        if outside_phase:
            if self.state in {SessionState.QUOTING, SessionState.REPLACING}:
                self._begin_window_close(final=final_phase)
                return
            if self.state == SessionState.CLOSING_WAIT:
                self._begin_window_close(final=final_phase)
                return
            if self.state in {
                SessionState.CLOSING_CANCELS,
                SessionState.CLOSING_RECONCILE,
                SessionState.FLATTENING,
            }:
                self._window_close_kind = "final" if final_phase else "pause"
            if self.state in {
                SessionState.PREVIEW,
                SessionState.WAITING_FOR_CONTRACT,
                SessionState.WAITING_FOR_ZERO_POSITION,
                SessionState.WAITING_FOR_STABLE_QUOTE,
                SessionState.PAUSED,
            }:
                self._begin_window_close(final=final_phase)
                return
        elif self.state == SessionState.PAUSED:
            self._reset_for_next_window()

        if self.state == SessionState.QUOTE_PENDING:
            if self._quote_ack_query_request_id is None and self._quote_ack_deadline is not None and self._now >= self._quote_ack_deadline:
                self._record_trace(
                    "quote_ack_timeout",
                    client_ids=list(self._pending_clients) + [order.client_id for order in self._active_orders()],
                    calculation={"deadline": self._quote_ack_deadline, "quote_group_id": self._quote_group_id},
                )
                self._quote_ack_deadline = None
                self._request_sequence += 1
                self._quote_ack_query_request_id = f"quote-ack-{self._request_sequence}"
                self._quote_ack_query_started_at = self._now
                self._emit(
                    "query_order",
                    request_id=self._quote_ack_query_request_id,
                    symbol=self.target_symbol,
                    exchange=self.target_exchange,
                    phase="quote_ack",
                    quote_group_id=self._quote_group_id,
                )
                return
            if (
                self._quote_ack_query_request_id is not None
                and self._quote_ack_query_started_at is not None
                and self._now - self._quote_ack_query_started_at >= self.config.effective["cancel_timeout_seconds"]
            ):
                self._risk_hold("quote_ack_order_query_timeout")
                return

        if self.state == SessionState.QUOTING and phase == "open" and self._quote_is_stale():
            max_age = float(self.config.effective["max_tick_age_seconds"])
            last_tick_at = self._latest_tick.exchange_time if self._latest_tick is not None and self._latest_tick.exchange_time else (self._latest_tick.at if self._latest_tick is not None else None)
            latest_exchange = self._exchange_datetime(self._latest_tick.exchange_time) if self._latest_tick is not None else None
            if latest_exchange is not None and self._last_wall_time is not None and not self.replay_market_data:
                age_seconds = (self._wall_clock_aware(self._last_wall_time) - latest_exchange.astimezone(CHINA_TZ)).total_seconds()
            else:
                age_seconds = self._now - self._latest_tick.at if self._latest_tick is not None else None
            self._record_trace(
                "quote_stale",
                client_ids=[order.client_id for order in self._active_orders()] + list(self._pending_clients),
                calculation={
                    "last_tick_at": last_tick_at,
                    "age_seconds": age_seconds,
                    "max_tick_age_seconds": max_age,
                    "reason": "quote_stale",
                },
            )
            self._begin_replacement("quote_stale", safety=True)
            return

        if self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            if self._stable_since is None or self._latest_tick is None:
                return
            max_age = self.config.effective["max_tick_age_seconds"]
            exchange_time = self._exchange_datetime(self._latest_tick.exchange_time)
            if exchange_time is not None and wall_time is not None and not self.replay_market_data:
                last_age = (self._wall_clock_aware(wall_time) - exchange_time.astimezone(CHINA_TZ)).total_seconds()
            else:
                last_age = self._now - self._latest_tick.at
            if not self._protected_valid(self._latest_tick) or last_age > max_age:
                self._reset_stable_quote_gate()
                return
            elapsed = self._now - self._stable_since
            if elapsed < self.config.effective["stable_market_seconds"]:
                return
            if self._stable_tick_count < 2:
                self._reset_stable_quote_gate()
                return
            self._record_trace(
                "stable_quote_qualified",
                market={
                    "last_price": self._latest_tick.last_price,
                    "bid_price": self._latest_tick.bid_price,
                    "ask_price": self._latest_tick.ask_price,
                },
                calculation={
                    "stable_since": self._stable_since,
                    "elapsed_seconds": elapsed,
                    "required_seconds": self.config.effective["stable_market_seconds"],
                    "tick_count": self._stable_tick_count,
                    "last_tick_age": last_age,
                    "max_tick_age_seconds": max_age,
                },
            )
            self._submit_quotes()
            return

        if self.state == SessionState.CLOSING_WAIT:
            if self._window_started_at is not None and self._now - self._window_started_at >= self.config.effective["closing_wait_seconds"]:
                self._flatten_window_net()
            return

        if self.state == SessionState.REPLACING:
            self._retry_replacement_cancels()
            self._warn_if_replacement_delayed()
            if (
                self._pending_clients
                and self._replacement_started_at is not None
                and self._now - self._replacement_started_at >= self.config.effective["cancel_timeout_seconds"]
            ):
                self._risk_hold("replacement_pending_order_unknown")
                return
            self._maybe_finish_replacement()
            return

        if self.state == SessionState.CLOSING_CANCELS:
            self._cancel_all(safety=True)
            if self._closing_started_at is not None and self._now - self._closing_started_at >= self.config.effective["cancel_timeout_seconds"]:
                if self.failure_reason is None:
                    self.failure_reason = "cancel_timeout"
                self._record_trace(
                    "cancel_timeout",
                    client_ids=[order.client_id for order in self._active_orders()],
                    calculation={
                        "started_at": self._closing_started_at,
                        "elapsed_seconds": self._now - self._closing_started_at,
                        "timeout_seconds": self.config.effective["cancel_timeout_seconds"],
                        "active_client_ids": [order.client_id for order in self._active_orders()],
                    },
                )
                self.cancellation_terminal = False
                if self._active_orders() or self._pending_clients:
                    self._risk_hold("cancel_timeout")
                else:
                    self._begin_reconcile()
            else:
                self._maybe_reconcile()
            return

        if self.state == SessionState.FLATTENING:
            if self._flatten_started_at is not None and self._now - self._flatten_started_at >= self.config.effective["flatten_timeout_seconds"]:
                self._record_trace(
                    "flatten_timeout",
                    client_ids=[self._flatten_client_id] if self._flatten_client_id else [],
                    calculation={
                        "started_at": self._flatten_started_at,
                        "elapsed_seconds": self._now - self._flatten_started_at,
                        "timeout_seconds": self.config.effective["flatten_timeout_seconds"],
                        "net_position": self.final_net_position,
                    },
                )
                self._cancel_all(safety=True)
                self._fail("flatten_timeout")
            elif self._flatten_client_id is None:
                self._try_flatten()

    def _on_interrupt(self) -> None:
        if self.state == SessionState.RISK_HOLD:
            self._record_trace("interrupt_risk_hold", calculation={"action": "keep_connection"})
            self._emit("audit_warning", code="risk_hold_requires_reconciliation")
            return
        if self.state in {SessionState.PREVIEW, SessionState.FINISHED, SessionState.FAILED}:
            if self.state == SessionState.PREVIEW:
                self.state = SessionState.FINISHED
            return
        self.stop_reason = self.stop_reason or "interrupted"
        self._record_trace(
            "interrupt",
            calculation={"state_before": self.state.value, "reason": self.stop_reason},
        )
        if self.state in {SessionState.WAITING_FOR_CONTRACT, SessionState.WAITING_FOR_ZERO_POSITION}:
            self.failure_reason = self.failure_reason or "interrupted_before_zero_position"
            self._fail("interrupted_before_zero_position")
            return
        if self.state == SessionState.WAITING_FOR_STABLE_QUOTE:
            self.state = SessionState.FINISHED
            return
        if self.state == SessionState.CLOSING_WAIT:
            # 窗口期间中断：立即结束窗口，走撤全部委托的人工结束收口。
            self.state = SessionState.CLOSING_CANCELS
            self._closing_started_at = self._now
            self.cancellation_terminal = None
            self.final_net_position = None
            self._cancel_all(safety=True)
            self._maybe_reconcile()
            return
        if self.state not in {SessionState.CLOSING_CANCELS, SessionState.CLOSING_RECONCILE, SessionState.FLATTENING}:
            self._enter_closing("interrupt")

    def _submit_quotes(self) -> bool:
        tick = self._latest_tick
        if not self._protected_valid(tick) or self._contract is None:
            return False
        if not self._normal_actions_available(2):
            if not self._action_limit_paused:
                self._record_trace(
                    "normal_action_limit",
                    calculation={
                        "requested_actions": 2,
                        "action_limit_per_minute": self.config.effective["action_limit_per_minute"],
                        "used_actions": len(self._action_times),
                        "reason": "quote_submit_blocked",
                    },
                )
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
        if not self._quote_prices_valid(tick, prices):
            self._reset_stable_quote_gate()
            self._record_trace(
                "quote_rejected",
                market={
                    "last_price": tick.last_price,
                    "bid_price": tick.bid_price,
                    "ask_price": tick.ask_price,
                    "limit_up": tick.limit_up,
                    "limit_down": tick.limit_down,
                },
                calculation={"buy_price": prices[0], "sell_price": prices[1], "reason": "book_or_limit_protection"},
            )
            return False
        self._sequence += 1
        self._quote_group_id = str(self._sequence)
        self._quote_acknowledged.clear()
        self._quote_ack_deadline = self._now + float(self.config.effective["quote_ack_timeout_seconds"])
        self._quote_ack_query_request_id = None
        self._quote_ack_query_started_at = None
        client_ids = [f"quote-{self._sequence}-buy", f"quote-{self._sequence}-sell"]
        trace = {
            "code": "quote_submitted",
            "client_ids": client_ids,
            "market": {
                "last_price": tick.last_price,
                "bid_price": tick.bid_price,
                "ask_price": tick.ask_price,
                "limit_up": tick.limit_up,
                "limit_down": tick.limit_down,
            },
            "calculation": {
                "anchor_ticks": self._anchor_ticks,
                "anchor_price": self._anchor_ticks * self._contract.pricetick,
                "pricetick": self._contract.pricetick,
                "w_ticks": w,
                "d_ticks": d,
                "distance_ticks": distance,
                "buy_price": prices[0],
                "sell_price": prices[1],
            },
        }
        if self._next_quote_context is not None:
            trace["replacement"] = self._next_quote_context
            self._next_quote_context = None
        self._record_trace(trace.pop("code"), **trace)
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
                quote_group_id=self._quote_group_id,
            )
            self._record_normal_action()
        self._round_open_net = 0
        self.state = SessionState.QUOTE_PENDING
        return True

    def _begin_replacement(self, reason: str, *, safety: bool) -> None:
        if self.state in _CLOSING_OR_TERMINAL:
            return
        self.state = SessionState.REPLACING
        self._replacement_reason = reason
        self._replacement_safety = safety
        self._replacement_client_ids = [order.client_id for order in self._active_orders()] + list(self._pending_clients)
        self._replacement_started_at = self._now
        self._replacement_warning_emitted = False
        self._cancel_all(safety=safety)
        self._maybe_finish_replacement()

    def _retry_replacement_cancels(self) -> None:
        for order in self._orders.values():
            if order.active and (
                not order.cancel_requested
                or self._now - self._cancel_attempt_at.get(order.client_id, float("-inf")) >= 1
            ):
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
        self._record_trace(
            "replacement_delayed",
            client_ids=list(self._replacement_client_ids),
            calculation={
                "reason": self._replacement_reason,
                "elapsed_seconds": self._now - self._replacement_started_at,
                "waiting_for_terminal_callbacks": True,
            },
        )
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
        self._next_quote_context = {
            "previous_client_ids": list(self._replacement_client_ids),
            "reason": reason,
        }
        self._record_trace(
            "replacement_ready",
            client_ids=list(self._replacement_client_ids),
            calculation={
                "reason": reason,
                "next_state": SessionState.WAITING_FOR_STABLE_QUOTE.value,
            },
        )
        self._replacement_reason = None
        self._replacement_safety = False
        self._replacement_client_ids = []
        self.state = SessionState.WAITING_FOR_STABLE_QUOTE
        self._reset_stable_quote_gate()

    def _handle_opening_fill(
        self,
        order: _Order,
        volume: int,
        *,
        price: float = 0,
        trade_id: str = "",
        exchange_time: str | None = None,
    ) -> None:
        """开仓成交：委托回报与成交流水共用同一套收口入口，避免 CTP 乱序漏触发。"""
        if volume <= 0:
            return
        if self.state in {SessionState.QUOTE_PENDING, SessionState.QUOTING, SessionState.REPLACING}:
            self._enter_closing(
                "first_fill",
                order=order,
                volume=volume,
                price=price,
                trade_id=trade_id,
                exchange_time=exchange_time,
            )
        elif self.state in {SessionState.WAITING_FOR_STABLE_QUOTE, SessionState.PAUSED}:
            self._enter_closing(
                "late_fill",
                order=order,
                volume=volume,
                price=price,
                trade_id=trade_id,
                exchange_time=exchange_time,
            )
        elif self.state == SessionState.CLOSING_WAIT:
            self._apply_window_fill(order, price=price, trade_id=trade_id, exchange_time=exchange_time)
        elif self.state in {
            SessionState.CLOSING_RECONCILE,
            SessionState.FLATTENING,
            SessionState.FINISHED,
            SessionState.FAILED,
        }:
            self._record_late_opening_fill(order)

    def _enter_closing(
        self,
        reason: str,
        *,
        order: _Order | None = None,
        volume: int = 0,
        price: float = 0,
        trade_id: str = "",
        exchange_time: str | None = None,
    ) -> None:
        if self.state in _CLOSING_OR_TERMINAL:
            return
        if reason == "first_fill" and order is not None:
            self._enter_window(order, volume, price=price, trade_id=trade_id, exchange_time=exchange_time)
            if self.config.effective["closing_wait_seconds"] <= 0:
                self._flatten_window_net()
            return
        self.state = SessionState.CLOSING_CANCELS
        self._closing_started_at = self._now
        self.cancellation_terminal = None
        self._round_has_fill = order is not None or self._round_has_fill
        if order is not None:
            self.first_fill = {
                "order_id": order.order_id,
                "client_id": order.client_id,
                "side": order.side,
                "volume": volume,
                "price": price or order.price,
                "trade_id": trade_id,
            }
        self._record_trace(
            "closing_started",
            client_ids=[order.client_id] if order is not None else [],
            calculation={
                "reason": reason,
                "order_id": order.order_id if order is not None else None,
                "volume": volume,
                "price": price or (order.price if order is not None else 0),
                "trade_id": trade_id,
                "exchange_time": exchange_time,
                "next_state": SessionState.CLOSING_CANCELS.value,
            },
        )
        self.final_net_position = None
        self._cancel_all(safety=True)
        self._maybe_reconcile()

    def _enter_window(
        self,
        order: _Order,
        volume: int,
        *,
        price: float = 0,
        trade_id: str = "",
        exchange_time: str | None = None,
    ) -> None:
        """价差窗口：首次成交后对侧报价继续挂单，等待配置的窗口时长。"""
        self.state = SessionState.CLOSING_WAIT
        self._window_started_at = self._now
        self._round_has_fill = True
        self._account_window_fill(order)
        self.first_fill = {
            "order_id": order.order_id,
            "client_id": order.client_id,
            "side": order.side,
            "volume": volume,
            "price": price or order.price,
            "trade_id": trade_id,
            "exchange_time": exchange_time,
        }
        self.final_net_position = None
        self._record_trace(
            "first_fill",
            client_ids=[order.client_id],
            calculation={
                "order_id": order.order_id,
                "volume": volume,
                "price": price or order.price,
                "trade_id": trade_id,
                "exchange_time": exchange_time,
                "window_seconds": self.config.effective["closing_wait_seconds"],
                "window_started_at": self._window_started_at,
                "window_ends_at": self._window_started_at + self.config.effective["closing_wait_seconds"],
                "reason": "first_fill",
            },
        )

    def _apply_window_fill(
        self,
        order: _Order,
        *,
        price: float = 0,
        trade_id: str = "",
        exchange_time: str | None = None,
    ) -> None:
        """窗口期间的报价成交：按已入账差额计入本轮净仓；对侧成交立即结束窗口。"""
        delta = self._account_window_fill(order)
        if delta <= 0:
            return
        first_client_id = (self.first_fill or {}).get("client_id")
        opposite = self._is_opposite_of_first_fill(order)
        opposite_complete = opposite and order.traded >= order.volume
        self._record_trace(
            "spread_complete" if opposite_complete else ("opposite_fill" if opposite else "window_fill"),
            client_ids=[client_id for client_id in (first_client_id, order.client_id) if client_id],
            calculation={
                "order_id": order.order_id,
                "volume": delta,
                "price": price or order.price,
                "trade_id": trade_id,
                "exchange_time": exchange_time,
                "net_position": self._round_open_net,
                "window_started_at": self._window_started_at,
                "elapsed_seconds": self._now - self._window_started_at if self._window_started_at is not None else None,
                "window_seconds": self.config.effective["closing_wait_seconds"],
                "reason": "spread_complete" if opposite_complete else ("opposite_fill" if opposite else "additional_fill"),
                "opposite_complete": opposite_complete,
            },
        )
        if opposite:
            self._cancel_remaining_and_reconcile(reason="spread_complete")

    def _account_window_fill(self, order: _Order) -> int:
        """同一成交的委托/成交回报只计一次；返回本次新计入的量。"""
        delta = order.traded - order.accounted_opening_traded
        if delta <= 0:
            return 0
        order.accounted_opening_traded = order.traded
        self._round_open_net += delta if order.side == "BUY" else -delta
        return delta

    def _is_opposite_of_first_fill(self, order: _Order) -> bool:
        first_id = (self.first_fill or {}).get("client_id") or ""
        if not first_id.startswith("quote-"):
            return False
        prefix, side = first_id.rsplit("-", 1)
        return order.client_id == f"{prefix}-{'sell' if side == 'buy' else 'buy'}"

    def _flatten_window_net(self) -> None:
        """窗口超时：按本轮开仓净仓直接受限 FAK，平仓终态后再撤对侧。"""
        self.final_net_position = self._round_open_net
        self._record_trace(
            "window_timeout",
            client_ids=[order.client_id for order in self._active_orders()],
            calculation={
                "window_started_at": self._window_started_at,
                "ended_at": self._now,
                "elapsed_seconds": self._now - self._window_started_at if self._window_started_at is not None else None,
                "window_seconds": self.config.effective["closing_wait_seconds"],
                "net_position": self.final_net_position,
                "reason": "window_timeout",
            },
        )
        for order in self._orders.values():
            if not order.is_flatten:
                # 已计入窗口净仓的成交在迟到回报记账时不得重复累计。
                order.accounted_opening_traded = order.traded
        if self.final_net_position == 0:
            self._cancel_remaining_and_reconcile(reason="window_net_zero")
            return
        self.state = SessionState.FLATTENING
        self._flatten_started_at = self._now
        self._flatten_initial_price = None
        self._try_flatten()

    def _cancel_remaining_and_reconcile(self, *, reason: str = "flatten_complete") -> None:
        """撤掉剩余委托（对侧报价等），全部终态后对账净仓收尾。"""
        if self.state in {SessionState.FINISHED, SessionState.FAILED}:
            # 已终态的会话（如平仓拒单失败）不得被重复回报复活回收口状态。
            return
        self._record_trace(
            "remaining_cancel",
            client_ids=[order.client_id for order in self._active_orders()] + list(self._pending_clients),
            calculation={
                "reason": reason,
                "net_position": self.final_net_position,
                "active_client_ids": [order.client_id for order in self._active_orders()],
                "pending_client_ids": list(self._pending_clients),
            },
        )
        if self._active_orders() or self._pending_clients:
            self.state = SessionState.CLOSING_CANCELS
            self._closing_started_at = self._now
            self.cancellation_terminal = None
            self._cancel_all(safety=True)
            self._maybe_reconcile()
        else:
            self._begin_reconcile()

    def _cancel_all(self, *, safety: bool) -> None:
        for order in self._orders.values():
            if order.active and (
                not order.cancel_requested
                or self._now - self._cancel_attempt_at.get(order.client_id, float("-inf")) >= 1
            ):
                self._emit_cancel(order, safety=safety)
        for order in self._pending_clients.values():
            order.cancel_requested = True

    def _emit_cancel(self, order: _Order, *, safety: bool) -> None:
        if not safety and not self._normal_actions_available(1):
            if not self._action_limit_paused:
                self._record_trace(
                    "normal_action_limit",
                    client_ids=[order.client_id],
                    calculation={
                        "requested_actions": 1,
                        "action_limit_per_minute": self.config.effective["action_limit_per_minute"],
                        "used_actions": len(self._action_times),
                        "reason": "cancel_blocked",
                    },
                )
                self._emit("audit_warning", code="normal_action_limit_reached")
            self._action_limit_paused = True
            return
        order.cancel_requested = True
        self._cancel_attempt_at[order.client_id] = self._now
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
        if self.state not in {
            SessionState.CLOSING_CANCELS,
            SessionState.CLOSING_WAIT,
            SessionState.FLATTENING,
        }:
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
        self._record_trace(
            "closing_position_query",
            calculation={
                "request_id": self.closing_position_request_id,
                "phase": "closing",
                "cancellation_terminal": self.cancellation_terminal,
                "failure_reason": self.failure_reason,
            },
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
        adverse_ticks = self.config.effective["flatten_adverse_ticks"]
        tick_size = self._contract.pricetick if self._contract else 0
        adverse_price_limit = (
            self._flatten_initial_price - adverse_ticks * tick_size
            if long_position
            else self._flatten_initial_price + adverse_ticks * tick_size
        )
        self._record_trace(
            "flatten_submitted",
            client_ids=[client_id],
            market={
                "bid_price": tick.bid_price,
                "ask_price": tick.ask_price,
                "last_price": tick.last_price,
            },
            calculation={
                "net_position": self.final_net_position,
                "side": side,
                "offset": offset,
                "order_type": "FAK",
                "volume": abs(self.final_net_position),
                "initial_executable_price": self._flatten_initial_price,
                "adverse_ticks": adverse_ticks,
                "adverse_price_limit": adverse_price_limit,
                "market_executable_price": tick.bid_price if long_position else tick.ask_price,
                "actual_price": executable,
                "reprice_attempt": len(self.flatten_attempts),
            },
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
            self._record_trace(
                "round_failed",
                calculation={
                    "failure_reason": self.failure_reason,
                    "final_net_position": self.final_net_position,
                    "next_state": SessionState.FAILED.value,
                },
            )
            self.state = SessionState.FAILED
            return
        window_close_kind = self._window_close_kind
        self._window_close_kind = None
        if self._round_has_fill:
            self._round_trips += 1
            self._round_has_fill = False
        if window_close_kind is not None:
            if window_close_kind == "final":
                self.stop_reason = self.stop_reason or "quote_window_end"
                self.state = SessionState.FINISHED
            elif self._round_trips >= self.config.effective["max_round_trips"]:
                self.stop_reason = self.stop_reason or "max_round_trips"
                self.state = SessionState.FINISHED
            else:
                self._reset_for_next_window()
                self.state = SessionState.PAUSED
            self._record_trace(
                "quote_window_closed",
                calculation={
                    "window_close_kind": window_close_kind,
                    "round_trips": self._round_trips,
                    "stop_reason": self.stop_reason,
                    "next_state": self.state.value,
                },
            )
            return
        if self._round_trips == 0 and not self._round_has_fill:
            self.state = SessionState.FINISHED
            return
        if self.stop_reason is None and self._round_trips >= self.config.effective["max_round_trips"]:
            self.stop_reason = "max_round_trips"
        if self.stop_reason is not None:
            self.state = SessionState.FINISHED
        else:
            # 一轮完成且未到停止条件：清锚重新进入稳定行情门槛，继续下一轮挂单。
            self._anchor_ticks = None
            self._reset_stable_quote_gate()
            self._outside_since = None
            self.state = SessionState.WAITING_FOR_STABLE_QUOTE
        self._record_trace(
            "round_finished",
            calculation={
                "round_trips": self._round_trips,
                "stop_reason": self.stop_reason,
                "final_net_position": self.final_net_position,
                "next_state": self.state.value,
            },
        )

    def _fail(self, reason: str) -> None:
        if self.state in {SessionState.FINISHED, SessionState.FAILED, SessionState.RISK_HOLD}:
            return
        if self._active_orders() or self._pending_clients or self.final_net_position is None or self.final_net_position != 0:
            self._risk_hold(reason)
            return
        self.failure_reason = self.failure_reason or reason
        self._record_trace(
            "failure",
            calculation={"reason": self.failure_reason, "next_state": SessionState.FAILED.value},
        )
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
        if self._quote_is_stale():
            return False
        values = (tick.bid_price, tick.ask_price)
        return all(isfinite(value) and value > 0 for value in values)

    def _protected_valid(self, tick: TickEvent | None) -> bool:
        return self._book_protection_facts(tick)["passed"]

    def _quote_prices_valid(self, tick: TickEvent, prices: tuple[float, float]) -> bool:
        if not all(isfinite(price) and price > 0 for price in prices):
            return False
        buy_price, sell_price = prices
        if not (buy_price < tick.ask_price and sell_price > tick.bid_price):
            return False
        limits = (tick.limit_down, tick.limit_up)
        if limits == (None, None):
            # Legacy deterministic tests have no CTP limit fields. The live CTP
            # adapter always copies them and rejects zero/invalid limits below.
            return True
        if not all(isinstance(value, (int, float)) and isfinite(value) and value > 0 for value in limits):
            return False
        limit_down, limit_up = limits
        if limit_down > limit_up:
            return False
        return all(limit_down <= value <= limit_up for value in (tick.last_price, tick.bid_price, tick.ask_price, buy_price, sell_price))

    def _book_protection_facts(self, tick: TickEvent | None) -> dict[str, Any]:
        w_ticks = self.config.effective["w_ticks"]
        d_ticks = self.config.effective["d_ticks"]
        distance_ticks = w_ticks + d_ticks
        multiple = self.config.effective["book_protection_multiple"]
        if tick is None or self._contract is None:
            return {
                "w_ticks": w_ticks,
                "d_ticks": d_ticks,
                "distance_ticks": distance_ticks,
                "spread_ticks": None,
                "protection_multiple": multiple,
                "comparison": "distance_ticks > protection_multiple * spread_ticks",
                "passed": False,
            }
        values = (tick.last_price, tick.bid_price, tick.ask_price, self._contract.pricetick)
        if not all(isinstance(value, (int, float)) and isfinite(value) and value > 0 for value in values):
            return {
                "w_ticks": w_ticks,
                "d_ticks": d_ticks,
                "distance_ticks": distance_ticks,
                "spread_ticks": None,
                "protection_multiple": multiple,
                "comparison": "distance_ticks > protection_multiple * spread_ticks",
                "passed": False,
            }
        if tick.bid_price > tick.ask_price:
            return {
                "w_ticks": w_ticks,
                "d_ticks": d_ticks,
                "distance_ticks": distance_ticks,
                "spread_ticks": None,
                "protection_multiple": multiple,
                "comparison": "distance_ticks > protection_multiple * spread_ticks",
                "passed": False,
            }
        if (tick.limit_up is not None or tick.limit_down is not None) and not self._quote_prices_valid(tick, (tick.bid_price, tick.ask_price)):
            return {
                "w_ticks": w_ticks,
                "d_ticks": d_ticks,
                "distance_ticks": distance_ticks,
                "spread_ticks": None,
                "protection_multiple": multiple,
                "comparison": "limit_and_book_valid",
                "passed": False,
            }
        spread_ticks = max(
            0,
            int((tick.ask_price - tick.bid_price) / self._contract.pricetick + 0.999999999),
        )
        return {
            "w_ticks": w_ticks,
            "d_ticks": d_ticks,
            "distance_ticks": distance_ticks,
            "spread_ticks": spread_ticks,
            "protection_multiple": multiple,
            "comparison": "distance_ticks > protection_multiple * spread_ticks",
            "passed": distance_ticks > multiple * spread_ticks,
        }

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
        net_before = self.final_net_position
        order.accounted_opening_traded = order.traded
        self.final_net_position += volume if order.side == "BUY" else -volume
        self._record_trace(
            "late_opening_fill",
            client_ids=[order.client_id],
            calculation={
                "order_id": order.order_id,
                "side": order.side,
                "volume": volume,
                "net_position_before": net_before,
                "net_position_after": self.final_net_position,
                "state": self.state.value,
            },
        )
        if self.state == SessionState.FLATTENING and self.final_net_position == 0:
            if self._flatten_client_id is None:
                self._cancel_remaining_and_reconcile()
            else:
                self._cancel_all(safety=True)
        elif self.state == SessionState.FINISHED:
            self.failure_reason = self.failure_reason or "late_opening_fill_after_finish"
            self._record_trace(
                "late_opening_fill_after_finish",
                client_ids=[order.client_id],
                calculation={
                    "order_id": order.order_id,
                    "volume": volume,
                    "net_position": self.final_net_position,
                    "reason": self.failure_reason,
                },
            )
            self._risk_hold("late_opening_fill_after_finish")
        elif self.state == SessionState.FLATTENING and self._flatten_client_id is None:
            self._try_flatten()

    def _prune_actions(self) -> None:
        cutoff = self._now - 60
        while self._action_times and self._action_times[0] <= cutoff:
            self._action_times.popleft()

    def _emit(self, kind: str, **payload: Any) -> None:
        self.actions.append(Action(kind=kind, payload=payload))

    def _record_trace(self, code: str, **payload: Any) -> None:
        self._audit_trace.append({"code": code, **payload})

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
            "market_data_mode": "replay_override" if self.replay_market_data else "normal",
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
            "risk_hold": self.state == SessionState.RISK_HOLD,
        }


LiveGridEvent = (
    ContractEvent
    | TickEvent
    | OrderEvent
    | TradeEvent
    | PositionQueryCompleteEvent
    | ConnectionEvent
    | OrderActionErrorEvent
    | OrderQueryCompleteEvent
    | TradeQueryCompleteEvent
    | ClockEvent
    | InterruptEvent
)
