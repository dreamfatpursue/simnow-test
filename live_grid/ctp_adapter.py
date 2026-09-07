"""Thin adapter between vn.py CTP events and the deterministic session."""

from __future__ import annotations

import importlib.util
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import AuditWriter, MultiContractAuditWriter
from .session import (
    Action,
    ClockEvent,
    ConnectionEvent,
    ContractEvent,
    InterruptEvent,
    LiveGridSession,
    OrderActionErrorEvent,
    OrderEvent,
    OrderQueryCompleteEvent,
    PositionQueryCompleteEvent,
    SessionState,
    TickEvent,
    TradeQueryCompleteEvent,
    TradeEvent,
)


GATEWAY_NAME = "CTP"

# 零仓门槛尚未评估完的会话状态：只要还有会话处在其一，任何会话都不得开始首次报价。
_PREGATE_STATES = {SessionState.WAITING_FOR_CONTRACT, SessionState.WAITING_FOR_ZERO_POSITION}

# CTP 同一时刻只允许一个在途查询；目标合约查询完成后才发送启动查委托/成交/持仓。
POSITION_QUERY_MAX_ATTEMPTS = 60
# 查询被拒后的重发间隔（秒）：1s→2s→4s，之后固定 5s。
# 固定每秒一发的节奏会持续踩中 CTP 的秒级流控窗口，形成连续拒发自锁。
QUERY_SEND_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 5.0)


class CtpAdapterError(RuntimeError):
    """Raised when the order-capable adapter cannot prove its runtime boundary."""


def verify_project_gateway(project_root: str | Path | None = None) -> Path:
    """Require vnpy_ctp to resolve to this project's managed source tree."""
    try:
        import vnpy_ctp
    except (ImportError, OSError) as exc:
        raise CtpAdapterError(f"项目内 CTP 网关不可加载: {exc}") from exc

    root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
    expected = root.joinpath("vendor", "vnpy_ctp", "vnpy_ctp", "__init__.py")
    loaded = Path(vnpy_ctp.__file__).resolve()
    if loaded != expected:
        raise CtpAdapterError(f"CTP 网关未加载项目内源码: {loaded}; expected={expected}")
    if importlib.util.find_spec("vnpy_ctp.gateway.position_query") is None:
        raise CtpAdapterError("CTP 网关缺少结构化查询/连接事件契约")
    return loaded.parent


class CtpLiveGridAdapter:
    """Translate live CTP callbacks and session actions without strategy logic."""

    def __init__(
        self,
        sessions: list[LiveGridSession],
        gateway_setting: dict[str, str],
        audits: list[AuditWriter],
        project_root: str | Path | None = None,
        run_audit: MultiContractAuditWriter | None = None,
    ) -> None:
        if not sessions or len(sessions) != len(audits):
            raise CtpAdapterError("适配器需要一一对应的会话与审计写入器列表")
        self.sessions = list(sessions)
        self.gateway_setting = gateway_setting
        self.audits = list(audits)
        self.run_audit = run_audit
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self.main_engine: Any | None = None
        self._session_map: dict[tuple[str, str], LiveGridSession] = {}
        self._audit_map: dict[tuple[str, str], AuditWriter] = {}
        for session, audit in zip(self.sessions, self.audits):
            key = (session.target_symbol, session.target_exchange)
            if key in self._session_map:
                raise CtpAdapterError(f"目标合约重复: {key[0]}@{key[1]}")
            self._session_map[key] = session
            self._audit_map[key] = audit
        self._client_to_order: dict[str, str] = {}
        self._subscribed: set[tuple[str, str]] = set()
        # 账户级在途查询的 request_id 集合；一次查询的结果按合约扇出给全部待查会话。
        self._inflight_position_queries: set[int] = set()
        self._inflight_order_queries: set[int] = set()
        self._inflight_trade_queries: set[int] = set()
        self._startup_position_pending: dict[tuple[str, str], str] = {}
        self._closing_position_pending: dict[tuple[str, str], str] = {}
        self._recovery_position_pending: dict[tuple[str, str], str] = {}
        self._startup_order_pending: dict[tuple[str, str], str] = {}
        self._recovery_order_pending: dict[tuple[str, str], str] = {}
        self._quote_ack_order_pending: dict[int, tuple[tuple[str, str], str]] = {}
        self._recovery_cancel_orders: dict[tuple[str, str], set[str]] = {}
        self._recovery_cancel_started_at: dict[tuple[str, str], float] = {}
        self._recovery_cancel_last_sent: dict[tuple[str, str], dict[str, float]] = {}
        self._trade_position_after_query: dict[tuple[str, str], str] = {}
        self._startup_trade_pending: dict[tuple[str, str], str] = {}
        self._recovery_trade_pending: dict[tuple[str, str], str] = {}
        self._query_attempts = 0
        self._position_query_attempts = 0
        # 退避时钟可注入，便于确定性测试；生产环境使用单调时钟。
        self._clock = time.monotonic
        self._order_next_send_at = 0.0
        self._trade_next_send_at = 0.0
        self._position_next_send_at = 0.0
        self._receive_sequence = 0
        self._pending_cancel_sent: set[str] = set()
        self._lock = threading.RLock()

    def connect_setting(self) -> dict[str, Any]:
        setting = dict(self.gateway_setting)
        setting["查询合约"] = [
            f"{session.target_symbol}.{session.target_exchange}"
            for session in self.sessions
        ]
        return setting

    def start(self) -> Any:
        verify_project_gateway(self.project_root)
        try:
            from vnpy.event import EventEngine
            from vnpy.trader.event import (
                EVENT_ACCOUNT,
                EVENT_CONTRACT,
                EVENT_LOG,
                EVENT_ORDER,
                EVENT_TICK,
                EVENT_TRADE,
                EVENT_TIMER,
            )
            from vnpy_ctp import CtpGateway
            from vnpy_ctp.gateway import (
                EVENT_CTP_CONNECTION,
                EVENT_CTP_ORDER_ACTION_ERROR,
                EVENT_CTP_ORDER_QUERY_COMPLETE,
                EVENT_CTP_TRADE_QUERY_COMPLETE,
                EVENT_POSITION_QUERY_COMPLETE,
            )
        except (ImportError, OSError) as exc:
            raise CtpAdapterError(f"项目内 CTP 网关或原生扩展不可加载: {exc}") from exc

        self.main_engine = __import__("vnpy.trader.engine", fromlist=["MainEngine"]).MainEngine(EventEngine())
        self.main_engine.add_gateway(CtpGateway, GATEWAY_NAME)
        engine = self.main_engine.event_engine
        engine.register(EVENT_CONTRACT, self._on_contract)
        engine.register(EVENT_LOG, self._on_log)
        engine.register(EVENT_TICK, self._on_tick)
        engine.register(EVENT_ORDER, self._on_order)
        engine.register(EVENT_TRADE, self._on_trade)
        engine.register(EVENT_ACCOUNT, self._on_account)
        engine.register(EVENT_POSITION_QUERY_COMPLETE, self._on_position_query_complete)
        engine.register(EVENT_CTP_CONNECTION, self._on_connection)
        engine.register(EVENT_CTP_ORDER_ACTION_ERROR, self._on_order_action_error)
        engine.register(EVENT_CTP_ORDER_QUERY_COMPLETE, self._on_order_query_complete)
        engine.register(EVENT_CTP_TRADE_QUERY_COMPLETE, self._on_trade_query_complete)
        engine.register(EVENT_TIMER, self._on_timer)
        self.main_engine.connect(self.connect_setting(), GATEWAY_NAME)
        return self.main_engine

    def interrupt(self) -> None:
        for session in self.sessions:
            self._consume(InterruptEvent(), session)

    def close(self) -> None:
        # 不能持锁关闭事件引擎：其工作线程 join 前可能正阻塞在本锁的回调上。
        # 审计写入器由入口拥有并在引擎关闭之后关闭，避免迟到事件写入已关闭的审计目录。
        with self._lock:
            engine = self.main_engine
            self.main_engine = None
        if engine is not None:
            engine.close()

    def safe_to_close(self) -> bool:
        """Closing is safe only after every session proves no order and no net position."""
        return all(
            session.state in {SessionState.FINISHED, SessionState.FAILED}
            and session.summary()["active_order_count"] == 0
            and session.summary()["final_net_position"] == 0
            for session in self.sessions
        )

    def _on_contract(self, event: Any) -> None:
        contract = event.data
        key = (contract.symbol, contract.exchange.value)
        session = self._session_map.get(key)
        if session is None:
            return
        self._consume(
            ContractEvent(
                contract.symbol,
                contract.exchange.value,
                contract.pricetick,
                size=getattr(contract, "size", None),
                product_code=getattr(contract, "product_code", None)
                or contract.symbol.rstrip("0123456789").lower(),
            ),
            session,
        )
        if self.main_engine is None or key in self._subscribed:
            return
        self._subscribed.add(key)
        from vnpy.trader.constant import Exchange
        from vnpy.trader.object import SubscribeRequest

        self.main_engine.subscribe(
            SubscribeRequest(symbol=contract.symbol, exchange=Exchange(contract.exchange.value)),
            GATEWAY_NAME,
        )

    def _on_log(self, event: Any) -> None:
        """Expose gateway authentication and request errors during live startup."""
        data = event.data
        print(f"[CTP] {getattr(data, 'msg', data)}", flush=True)

    def _on_tick(self, event: Any) -> None:
        with self._lock:
            tick = event.data
            session = self._session_map.get((tick.symbol, tick.exchange.value))
            if session is None:
                return
            exchange_time = self._exchange_time(tick)
            # Live CTP data without an exchange timestamp is not eligible for a
            # quote; the session's timestamp-less seam exists only for old
            # deterministic unit tests.
            if exchange_time is None:
                return
            self._receive_sequence += 1
            self._consume(
                TickEvent(
                    symbol=tick.symbol,
                    exchange=tick.exchange.value,
                    last_price=tick.last_price,
                    bid_price=tick.bid_price_1,
                    ask_price=tick.ask_price_1,
                    at=time.monotonic(),
                    exchange_time=exchange_time,
                    trading_day=getattr(tick, "trading_day", None),
                    action_day=getattr(tick, "action_day", None),
                    update_millisec=getattr(tick, "update_millisec", None),
                    limit_up=getattr(tick, "limit_up", 0),
                    limit_down=getattr(tick, "limit_down", 0),
                    receive_sequence=self._receive_sequence,
                ),
                session,
            )

    @staticmethod
    def _exchange_time(data: Any) -> str | None:
        reported = getattr(data, "datetime", None)
        return reported.isoformat() if reported is not None else None

    def _on_order(self, event: Any) -> None:
        with self._lock:
            order = event.data
            session = self._session_map.get((order.symbol, order.exchange.value))
            if session is None:
                return
            client_id = self._client_to_order.get(order.orderid)
            if client_id is None:
                client_id = order.reference or None
            if client_id:
                self._client_to_order.setdefault(order.orderid, client_id)
            key = (order.symbol, order.exchange.value)
            if key in self._recovery_cancel_orders and self._status_name(order.status) in {"ALLTRADED", "CANCELLED", "REJECTED"}:
                self._recovery_cancel_orders[key].discard(order.orderid)
                if not self._recovery_cancel_orders[key]:
                    self._recovery_cancel_orders.pop(key, None)
                    self._recovery_cancel_started_at.pop(key, None)
                    self._recovery_cancel_last_sent.pop(key, None)
                    self._start_trade_for_key(key)
            if client_id and self._status_name(order.status) in {"ALLTRADED", "CANCELLED", "REJECTED"}:
                self._pending_cancel_sent.discard(client_id)
            self._consume(
                OrderEvent(
                    order_id=order.orderid,
                    symbol=order.symbol,
                    exchange=order.exchange.value,
                    side="BUY" if order.direction.value == "多" else "SELL",
                    status=self._status_name(order.status),
                    volume=int(order.volume),
                    traded=int(order.traded),
                    price=order.price,
                    client_id=client_id,
                    exchange_time=self._exchange_time(order),
                    offset=self._offset_name(getattr(order, "offset", None)),
                    order_ref=getattr(order, "order_ref", None),
                    order_sys_id=getattr(order, "order_sys_id", None),
                    status_unknown=bool(getattr(order, "ctp_status_unknown", False)),
                ),
                session,
            )

    def _on_trade(self, event: Any) -> None:
        with self._lock:
            trade = event.data
            session = self._session_map.get((trade.symbol, trade.exchange.value))
            if session is None:
                return
            client_id = self._client_to_order.get(trade.orderid) or getattr(trade, "reference", None)
            self._consume(
                TradeEvent(
                    order_id=trade.orderid,
                    symbol=trade.symbol,
                    exchange=trade.exchange.value,
                    side="BUY" if trade.direction.value == "多" else "SELL",
                    volume=int(trade.volume),
                    price=trade.price,
                    trade_id=trade.tradeid,
                    client_id=client_id,
                    exchange_time=self._exchange_time(trade),
                    at=time.monotonic(),
                ),
                session,
            )

    def _on_connection(self, event: Any) -> None:
        with self._lock:
            data = event.data
            for session in self.sessions:
                self._consume(
                    ConnectionEvent(
                        kind=data.kind,
                        connected=bool(data.connected),
                        reason=getattr(data, "reason", ""),
                    ),
                    session,
                )

    def _on_order_action_error(self, event: Any) -> None:
        with self._lock:
            data = event.data
            session = self._session_map.get((data.symbol, data.exchange))
            client_id = self._client_to_order.get(data.orderid)
            if client_id:
                self._pending_cancel_sent.discard(client_id)
            if session is not None:
                targets = [session]
            else:
                targets = self.sessions
            for target in targets:
                self._consume(
                    OrderActionErrorEvent(
                        order_id=data.orderid,
                        symbol=target.target_symbol,
                        exchange=target.target_exchange,
                        client_id=client_id,
                        error_id=data.error_id,
                        error_msg=data.error_msg,
                        action=getattr(data, "action", "cancel"),
                    ),
                    target,
                )

    def _on_order_query_complete(self, event: Any) -> None:
        with self._lock:
            result = event.data
            if result.request_id not in self._inflight_order_queries:
                return
            self._inflight_order_queries.discard(result.request_id)
            quote_pending = self._quote_ack_order_pending.pop(result.request_id, None)
            if quote_pending is not None:
                key, logical_id = quote_pending
                orders = []
                for raw in result.orders:
                    if self._raw_order_key(raw) == key:
                        orders.append(self._raw_order_event(raw, key))
                self._consume(
                    OrderQueryCompleteEvent(
                        request_id=logical_id,
                        symbol=key[0],
                        exchange=key[1],
                        orders=tuple(orders),
                        error_id=result.error_id,
                        error_msg=result.error_msg,
                    ),
                    self._session_map[key],
                )
                return
            by_key: dict[tuple[str, str], list[OrderEvent]] = {key: [] for key in self._session_map}
            active_orphans: dict[tuple[str, str], set[str]] = {key: set() for key in self._session_map}
            for raw in result.orders:
                key = self._raw_order_key(raw)
                if key not in by_key:
                    continue
                normalized = self._raw_order_event(raw, key)
                by_key[key].append(normalized)
                if normalized.offset == "OPEN" and normalized.status in {"SUBMITTING", "ACCEPTED", "NOTTRADED", "PARTTRADED"}:
                    active_orphans[key].add(normalized.order_id)
            pending = {**self._startup_order_pending, **self._recovery_order_pending}
            self._startup_order_pending = {}
            self._recovery_order_pending = {}
            for key, logical_id in pending.items():
                session = self._session_map[key]
                if not result.error_id and active_orphans[key]:
                    self._recovery_cancel_orders[key] = set(active_orphans[key])
                    self._recovery_cancel_started_at.setdefault(key, time.monotonic())
                    sent_at = time.monotonic()
                    last_sent = self._recovery_cancel_last_sent.setdefault(key, {})
                    for order_id in active_orphans[key]:
                        last_sent.setdefault(order_id, sent_at)
                    self._trade_position_after_query[key] = logical_id
                self._consume(
                    OrderQueryCompleteEvent(
                        request_id=str(result.request_id),
                        symbol=key[0],
                        exchange=key[1],
                        orders=tuple(by_key[key]),
                        error_id=result.error_id,
                        error_msg=result.error_msg,
                    ),
                    session,
                )
                if result.error_id:
                    continue
                if not active_orphans[key]:
                    self._start_trade_or_position(key, logical_id=logical_id)

    def _on_trade_query_complete(self, event: Any) -> None:
        with self._lock:
            result = event.data
            if result.request_id not in self._inflight_trade_queries:
                return
            self._inflight_trade_queries.discard(result.request_id)
            pending = {**self._startup_trade_pending, **self._recovery_trade_pending}
            self._startup_trade_pending = {}
            self._recovery_trade_pending = {}
            for key, logical_id in pending.items():
                self._consume(
                    TradeQueryCompleteEvent(
                        request_id=str(result.request_id),
                        symbol=key[0],
                        exchange=key[1],
                        trades=tuple(
                            trade for trade in result.trades
                            if str(self._raw_get(trade, "InstrumentID", key[0])) == key[0]
                        ),
                        error_id=result.error_id,
                        error_msg=result.error_msg,
                    ),
                    self._session_map[key],
                )
                if result.error_id:
                    continue
                self._start_position_for_key(key, logical_id=logical_id)

    def _raw_order_key(self, raw: dict[str, Any]) -> tuple[str, str]:
        symbol = str(self._raw_get(raw, "InstrumentID", self._raw_get(raw, "symbol", "")))
        from vnpy_ctp.gateway.ctp_gateway import symbol_contract_map
        mapped = symbol_contract_map.get(symbol)
        exchange = mapped.exchange.value if mapped is not None else str(self._raw_get(raw, "ExchangeID", self._raw_get(raw, "exchange", self._raw_get(raw, "Exchange", ""))))
        return symbol, exchange

    def _raw_order_event(self, raw: dict[str, Any], key: tuple[str, str]) -> OrderEvent:
        order_id = str(
            self._raw_get(
                raw,
                "order_id",
                f"{self._raw_get(raw, 'FrontID', 0)}_{self._raw_get(raw, 'SessionID', 0)}_{self._raw_get(raw, 'OrderRef', '')}",
            )
        )
        raw_status = str(self._raw_get(raw, "OrderStatus", self._raw_get(raw, "status", "")))
        status = "UNKNOWN" if raw_status == "a" else self._status_name(raw_status)
        offset = str(self._raw_get(raw, "offset", self._raw_get(raw, "CombOffsetFlag", "0")))
        offset = "OPEN" if offset in {"0", "OPEN", "开仓"} else "CLOSE"
        side = "BUY" if str(self._raw_get(raw, "direction", self._raw_get(raw, "Direction", ""))) in {"0", "BUY", "多"} else "SELL"
        return OrderEvent(
            order_id=order_id,
            symbol=key[0],
            exchange=key[1],
            side=side,
            status=status,
            volume=int(self._raw_get(raw, "volume", self._raw_get(raw, "VolumeTotalOriginal", 0)) or 0),
            traded=int(self._raw_get(raw, "traded", self._raw_get(raw, "VolumeTraded", 0)) or 0),
            price=float(self._raw_get(raw, "price", self._raw_get(raw, "LimitPrice", 0)) or 0),
            # The query can be the first callback that identifies an order.
            # Reuse the vt-order-id mapping created at submit time so a
            # delayed/missing order callback can still bind the pending quote.
            client_id=self._client_to_order.get(order_id),
            offset=offset,
            order_ref=str(self._raw_get(raw, "order_ref", self._raw_get(raw, "OrderRef", ""))),
            order_sys_id=str(self._raw_get(raw, "order_sys_id", self._raw_get(raw, "OrderSysID", ""))),
        )

    @staticmethod
    def _raw_get(raw: Any, key: str, default: Any = None) -> Any:
        return raw.get(key, default) if isinstance(raw, dict) else getattr(raw, key, default)

    def _on_account(self, event: Any) -> None:
        """Persist gateway account snapshots for the run; account ids are credentials."""
        with self._lock:
            if self.run_audit is None:
                return
            account = event.data
            self.run_audit.record_account(
                balance=float(account.balance),
                available=float(account.available),
                at=time.monotonic(),
            )

    def _on_position_query_complete(self, event: Any) -> None:
        with self._lock:
            self._handle_position_query_complete(event)

    def _handle_position_query_complete(self, event: Any) -> None:
        result = event.data
        if result.request_id not in self._inflight_position_queries:
            return
        self._inflight_position_queries.discard(result.request_id)
        nets: dict[tuple[str, str], int] = {key: 0 for key in self._session_map}
        for position in result.positions:
            key = (position.symbol, position.exchange.value)
            if key not in nets:
                continue
            if position.direction.value == "多":
                nets[key] += int(position.volume)
            elif position.direction.value == "空":
                nets[key] -= int(position.volume)
        startup = self._startup_position_pending
        closing = self._closing_position_pending
        recovery = self._recovery_position_pending
        self._startup_position_pending = {}
        self._closing_position_pending = {}
        self._recovery_position_pending = {}
        self._position_query_attempts = 0
        for key, logical_id in startup.items():
            self._consume(
                self._position_event(key, logical_id, nets[key], result.error_id, result.error_msg),
                self._session_map[key],
            )
        for key, logical_id in closing.items():
            self._consume(
                self._position_event(key, logical_id, nets[key], result.error_id, result.error_msg),
                self._session_map[key],
            )
        for key, logical_id in recovery.items():
            self._consume(
                self._position_event(key, logical_id, nets[key], result.error_id, result.error_msg),
                self._session_map[key],
            )

    def _start_position_for_key(self, key: tuple[str, str], logical_id: str | None = None) -> None:
        session = self._session_map.get(key)
        if session is None:
            return
        if logical_id is None:
            logical_id = self._startup_order_pending.get(key) or self._recovery_order_pending.get(key)
        if logical_id is None:
            logical_id = f"recovery-{int(time.monotonic() * 1000)}"
        phase = "recovery" if logical_id.startswith("recovery-") else "startup"
        pending = self._recovery_position_pending if phase == "recovery" else self._startup_position_pending
        pending[key] = logical_id
        if not self._inflight_position_queries:
            self._try_send_position_query()

    def _start_trade_or_position(self, key: tuple[str, str], logical_id: str) -> None:
        gateway = self.main_engine.get_gateway(GATEWAY_NAME) if self.main_engine is not None else None
        if gateway is not None and hasattr(gateway, "query_trade"):
            if logical_id.startswith("recovery-"):
                self._recovery_trade_pending[key] = logical_id
            else:
                self._startup_trade_pending[key] = logical_id
            if not self._inflight_trade_queries:
                self._try_send_trade_query()
            return
        self._start_position_for_key(key, logical_id=logical_id)

    def _start_trade_for_key(self, key: tuple[str, str]) -> None:
        logical_id = self._trade_position_after_query.pop(key, None)
        if logical_id is None:
            logical_id = self._startup_order_pending.get(key) or self._recovery_order_pending.get(key)
        if logical_id is None:
            logical_id = f"recovery-{int(time.monotonic() * 1000)}"
        self._start_trade_or_position(key, logical_id)

    @staticmethod
    def _position_event(
        key: tuple[str, str],
        logical_id: str,
        net_position: int,
        error_id: int,
        error_msg: str,
    ) -> PositionQueryCompleteEvent:
        return PositionQueryCompleteEvent(
            request_id=logical_id,
            symbol=key[0],
            exchange=key[1],
            net_position=net_position,
            error_id=error_id,
            error_msg=error_msg,
        )

    def _on_timer(self, event: Any) -> None:
        with self._lock:
            states_before = {id(session): session.state for session in self.sessions}
            if (self._startup_order_pending or self._recovery_order_pending) and not self._inflight_order_queries:
                self._try_send_order_query()
            if (self._startup_trade_pending or self._recovery_trade_pending) and not self._inflight_trade_queries:
                self._try_send_trade_query()
            if (self._startup_position_pending or self._closing_position_pending or self._recovery_position_pending) and not self._inflight_position_queries:
                self._try_send_position_query()
            self._retry_recovery_orphan_cancels()
            # 零仓启动门槛未在全部会话上评估完之前，禁止任何会话靠时钟进入首次报价。
            gate_closed = any(
                session.state in _PREGATE_STATES for session in self.sessions
            )
            for session in self.sessions:
                # A query-send exhaustion event just moved this session into
                # RISK_HOLD. Do not immediately spend another query attempt in
                # the same timer turn; recovery cadence starts on the next one.
                if states_before[id(session)] != SessionState.RISK_HOLD and session.state == SessionState.RISK_HOLD:
                    continue
                if gate_closed and session.state == SessionState.WAITING_FOR_STABLE_QUOTE:
                    continue
                self._consume(
                    ClockEvent(time.monotonic(), wall_time=datetime.now().astimezone().isoformat(timespec="seconds")),
                    session,
                )

    def _consume(self, event: object, session: LiveGridSession) -> None:
        with self._lock:
            audit = self._audit_map[(session.target_symbol, session.target_exchange)]
            state_before = session.state.value
            actions = session.handle(event)
            actions = list(actions) + self._recover_pending_order_cancels(session, actions)
            audit.record(
                event,
                actions,
                session.state.value,
                time.monotonic(),
                state_before=state_before,
                trace=session.last_audit_trace,
            )
            for action in actions:
                self._dispatch(action)

    def _recover_pending_order_cancels(self, session: LiveGridSession, actions: list[Action]) -> list[Action]:
        if session.state != SessionState.RISK_HOLD:
            return []
        already = {action.payload.get("order_id") for action in actions if action.kind == "cancel_order"}
        recovered: list[Action] = []
        for item in session.orders:
            if item["status"] in {"ALLTRADED", "CANCELLED", "REJECTED"}:
                continue
            order_id = item["order_id"]
            if not order_id:
                order_id = next((oid for oid, cid in self._client_to_order.items() if cid == item["client_id"]), "")
            if order_id and order_id not in already:
                if item["client_id"] in self._pending_cancel_sent:
                    continue
                self._pending_cancel_sent.add(item["client_id"])
                recovered.append(
                    Action(
                        "cancel_order",
                        {
                            "order_id": order_id,
                            "client_id": item["client_id"],
                            "symbol": session.target_symbol,
                            "exchange": session.target_exchange,
                            "safety": True,
                        },
                    )
                )
        return recovered

    def _retry_recovery_orphan_cancels(self) -> None:
        """Retry orphan OPEN cancels and surface an unconfirmed cancel as RISK_HOLD."""
        now = time.monotonic()
        for key, order_ids in list(self._recovery_cancel_orders.items()):
            session = self._session_map.get(key)
            if session is None:
                continue
            started = self._recovery_cancel_started_at.get(key, now)
            if now - started >= float(session.config.effective["cancel_timeout_seconds"]):
                self._consume(
                    OrderActionErrorEvent(
                        order_id=next(iter(order_ids), "orphan-cancel-timeout"),
                        symbol=key[0],
                        exchange=key[1],
                        error_id=1,
                        error_msg="遗留开仓单撤单未确认",
                    ),
                    session,
                )
                continue
            last_sent = self._recovery_cancel_last_sent.setdefault(key, {})
            for order_id in sorted(order_ids):
                if now - last_sent.get(order_id, float("-inf")) < 1:
                    continue
                action = Action(
                    "cancel_order",
                    {
                        "order_id": order_id,
                        "client_id": None,
                        "symbol": key[0],
                        "exchange": key[1],
                        "safety": True,
                    },
                )
                last_sent[order_id] = now
                self._dispatch(action)

    def _dispatch(self, action: Action) -> None:
        if self.main_engine is None:
            raise CtpAdapterError("CTP 适配器尚未启动")
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
        from vnpy.trader.object import CancelRequest, OrderRequest

        payload = action.payload
        if action.kind == "submit_order":
            side = Direction.LONG if payload["side"] == "BUY" else Direction.SHORT
            offset = {
                "OPEN": Offset.OPEN,
                "CLOSE": Offset.CLOSE,
                "CLOSETODAY": Offset.CLOSETODAY,
            }[payload["offset"]]
            order_type = OrderType.FAK if payload["order_type"] == "FAK" else OrderType.LIMIT
            request = OrderRequest(
                symbol=payload["symbol"],
                exchange=Exchange(payload["exchange"]),
                direction=side,
                type=order_type,
                volume=payload["volume"],
                price=payload["price"],
                offset=offset,
                reference=payload["client_id"],
            )
            send_error_reported = False
            try:
                vt_order_id = self.main_engine.send_order(request, GATEWAY_NAME)
            except Exception as exc:
                vt_order_id = ""
                send_error_reported = True
                session = self._session_map[(payload["symbol"], payload["exchange"])]
                self._consume(
                    OrderActionErrorEvent(
                        order_id=f"send-failed-{payload['client_id']}",
                        symbol=payload["symbol"],
                        exchange=payload["exchange"],
                        client_id=payload["client_id"],
                        error_id=1,
                        error_msg=str(exc),
                        action="insert",
                    ),
                    session,
                )
            client_id = payload["client_id"]
            if vt_order_id:
                self._client_to_order[vt_order_id.split(".", 1)[-1]] = client_id
            else:
                session = self._session_map[(payload["symbol"], payload["exchange"])]
                if not send_error_reported:
                    self._consume(
                        OrderActionErrorEvent(
                            order_id=f"send-failed-{client_id}",
                            symbol=payload["symbol"],
                            exchange=payload["exchange"],
                            client_id=client_id,
                            error_id=1,
                            error_msg="CTP 未返回委托号",
                            action="insert",
                        ),
                        session,
                    )
                self._consume(
                    OrderEvent(
                        order_id=f"rejected-{client_id}",
                        symbol=payload["symbol"],
                        exchange=payload["exchange"],
                        side=payload["side"],
                        status="REJECTED",
                        volume=payload["volume"],
                        price=payload["price"],
                        client_id=client_id,
                    ),
                    session,
                )
        elif action.kind == "cancel_order":
            order_id = payload["order_id"]
            if not order_id:
                return
            try:
                self.main_engine.cancel_order(
                    CancelRequest(
                        orderid=order_id,
                        symbol=payload["symbol"],
                        exchange=Exchange(payload["exchange"]),
                    ),
                    GATEWAY_NAME,
                )
            except Exception as exc:
                session = self._session_map[(payload["symbol"], payload["exchange"])]
                self._consume(
                    OrderActionErrorEvent(
                        order_id=order_id,
                        symbol=payload["symbol"],
                        exchange=payload["exchange"],
                        client_id=payload.get("client_id"),
                        error_id=1,
                        error_msg=str(exc),
                    ),
                    session,
                )
        elif action.kind == "query_order":
            key = (payload["symbol"], payload["exchange"])
            session = self._session_map[key]
            logical_id = str(payload["request_id"])
            gateway = self.main_engine.get_gateway(GATEWAY_NAME)
            request_id = gateway.query_order() if gateway is not None and hasattr(gateway, "query_order") else None
            if request_id is None:
                self._consume(
                    OrderQueryCompleteEvent(
                        request_id=logical_id,
                        symbol=key[0],
                        exchange=key[1],
                        error_id=1,
                        error_msg="CTP 委托查询请求未发送",
                    ),
                    session,
                )
            else:
                self._inflight_order_queries.add(request_id)
                self._quote_ack_order_pending[request_id] = (key, logical_id)
        elif action.kind == "query_position":
            key = (payload["symbol"], payload["exchange"])
            phase = payload["phase"]
            if phase == "startup":
                self._startup_position_pending[key] = payload["request_id"]
                gateway = self.main_engine.get_gateway(GATEWAY_NAME)
                if hasattr(gateway, "query_order"):
                    self._startup_order_pending[key] = payload["request_id"]
                    if not self._inflight_order_queries:
                        self._try_send_order_query()
                elif not self._inflight_position_queries:
                    self._try_send_position_query()
            elif phase == "recovery":
                self._recovery_position_pending[key] = payload["request_id"]
                gateway = self.main_engine.get_gateway(GATEWAY_NAME)
                if hasattr(gateway, "query_order"):
                    self._recovery_order_pending[key] = payload["request_id"]
                    if not self._inflight_order_queries:
                        self._try_send_order_query()
                elif not self._inflight_position_queries:
                    self._try_send_position_query()
            else:
                self._closing_position_pending[key] = payload["request_id"]
                if not self._inflight_position_queries:
                    self._try_send_position_query()

    @staticmethod
    def _send_backoff_seconds(consecutive_failures: int) -> float:
        """被拒后的下一次发送间隔：1s→2s→4s，之后固定 5s。"""
        if consecutive_failures <= 0:
            return 0.0
        index = min(consecutive_failures, len(QUERY_SEND_BACKOFF_SECONDS)) - 1
        return QUERY_SEND_BACKOFF_SECONDS[index]

    @staticmethod
    def _send_refused_message(base: str, refusal: str | None) -> str:
        if refusal:
            return f"{base}（{refusal}）"
        return base

    def _try_send_position_query(self) -> None:
        """Send one account-level query; a refused send backs off and retries on later timers."""
        now = self._clock()
        if now < self._position_next_send_at:
            return
        gateway = self.main_engine.get_gateway(GATEWAY_NAME) if self.main_engine is not None else None
        request_id = gateway.query_position() if gateway is not None else None
        if request_id is not None:
            self._inflight_position_queries.add(request_id)
            self._position_query_attempts = 0
            self._position_next_send_at = 0.0
            return
        refusal = getattr(gateway, "last_query_send_refusal", None)
        self._position_query_attempts += 1
        self._position_next_send_at = now + self._send_backoff_seconds(self._position_query_attempts)
        if self._position_query_attempts < POSITION_QUERY_MAX_ATTEMPTS:
            return
        pending = {
            **self._startup_position_pending,
            **self._closing_position_pending,
            **self._recovery_position_pending,
        }
        self._startup_position_pending = {}
        self._closing_position_pending = {}
        self._recovery_position_pending = {}
        self._position_query_attempts = 0
        for key, logical_id in pending.items():
            self._consume(
                PositionQueryCompleteEvent(
                    request_id=logical_id,
                    symbol=key[0],
                    exchange=key[1],
                    net_position=0,
                    error_id=1,
                    error_msg=self._send_refused_message("CTP 持仓查询请求未发送", refusal),
                ),
                self._session_map[key],
            )

    def _try_send_order_query(self) -> None:
        """Send one account-level order query; a refused send backs off and retries on later timers."""
        now = self._clock()
        if now < self._order_next_send_at:
            return
        gateway = self.main_engine.get_gateway(GATEWAY_NAME) if self.main_engine is not None else None
        request_id = gateway.query_order() if gateway is not None and hasattr(gateway, "query_order") else None
        if request_id is not None:
            self._inflight_order_queries.add(request_id)
            self._query_attempts = 0
            self._order_next_send_at = 0.0
            return
        refusal = getattr(gateway, "last_query_send_refusal", None)
        self._query_attempts += 1
        self._order_next_send_at = now + self._send_backoff_seconds(self._query_attempts)
        if self._query_attempts < POSITION_QUERY_MAX_ATTEMPTS:
            return
        pending = {**self._startup_order_pending, **self._recovery_order_pending}
        self._startup_order_pending = {}
        self._recovery_order_pending = {}
        self._query_attempts = 0
        for key, logical_id in pending.items():
            self._consume(
                OrderQueryCompleteEvent(
                    request_id=f"order-query-failed-{logical_id}",
                    symbol=key[0],
                    exchange=key[1],
                    error_id=1,
                    error_msg=self._send_refused_message("CTP 委托查询请求未发送", refusal),
                ),
                self._session_map[key],
            )

    def _try_send_trade_query(self) -> None:
        now = self._clock()
        if now < self._trade_next_send_at:
            return
        gateway = self.main_engine.get_gateway(GATEWAY_NAME) if self.main_engine is not None else None
        request_id = gateway.query_trade() if gateway is not None and hasattr(gateway, "query_trade") else None
        if request_id is not None:
            self._inflight_trade_queries.add(request_id)
            self._query_attempts = 0
            self._trade_next_send_at = 0.0
            return
        refusal = getattr(gateway, "last_query_send_refusal", None)
        self._query_attempts += 1
        self._trade_next_send_at = now + self._send_backoff_seconds(self._query_attempts)
        if self._query_attempts < POSITION_QUERY_MAX_ATTEMPTS:
            return
        pending = {**self._startup_trade_pending, **self._recovery_trade_pending}
        self._startup_trade_pending = {}
        self._recovery_trade_pending = {}
        self._query_attempts = 0
        for key, logical_id in pending.items():
            self._consume(
                TradeQueryCompleteEvent(
                    request_id=f"trade-query-failed-{logical_id}",
                    symbol=key[0],
                    exchange=key[1],
                    error_id=1,
                    error_msg=self._send_refused_message("CTP 成交查询请求未发送", refusal),
                ),
                self._session_map[key],
            )

    @staticmethod
    def _status_name(status: Any) -> str:
        return {
            "提交中": "SUBMITTING",
            "已报": "ACCEPTED",
            "未成交": "NOTTRADED",
            "部分成交": "PARTTRADED",
            "全部成交": "ALLTRADED",
            "已撤销": "CANCELLED",
            "拒单": "REJECTED",
            "0": "ALLTRADED",
            "1": "PARTTRADED",
            "3": "NOTTRADED",
            "5": "CANCELLED",
            "a": "UNKNOWN",
        }.get(getattr(status, "value", status), str(getattr(status, "name", status)))

    @staticmethod
    def _offset_name(offset: Any) -> str:
        return {
            "开": "OPEN",
            "开仓": "OPEN",
            "平": "CLOSE",
            "平仓": "CLOSE",
            "平今": "CLOSETODAY",
            "平昨": "CLOSEYESTERDAY",
            "OPEN": "OPEN",
            "CLOSE": "CLOSE",
            "CLOSETODAY": "CLOSETODAY",
            "CLOSEYESTERDAY": "CLOSEYESTERDAY",
        }.get(getattr(offset, "value", offset), "UNKNOWN")
