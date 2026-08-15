"""Thin adapter between vn.py CTP events and the deterministic session."""

from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path
from typing import Any

from .audit import AuditWriter
from .session import (
    Action,
    ClockEvent,
    ContractEvent,
    InterruptEvent,
    LiveGridSession,
    OrderEvent,
    PositionQueryCompleteEvent,
    SessionState,
    TickEvent,
    TradeEvent,
)


GATEWAY_NAME = "CTP"

# 零仓门槛尚未评估完的会话状态：只要还有会话处在其一，任何会话都不得开始首次报价。
_PREGATE_STATES = {SessionState.WAITING_FOR_CONTRACT, SessionState.WAITING_FOR_ZERO_POSITION}

# CTP 同一时刻只允许一个在途查询；目标合约回报可能在合约查询响应流的中间到达，
# 此时新查询发送会被拒，需按定时器每秒重试直到流结束（SimNow 可能持续数十秒）。
POSITION_QUERY_MAX_ATTEMPTS = 60


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
        raise CtpAdapterError("CTP 网关缺少持仓查询完成契约")
    return loaded.parent


class CtpLiveGridAdapter:
    """Translate live CTP callbacks and session actions without strategy logic."""

    def __init__(
        self,
        sessions: list[LiveGridSession],
        gateway_setting: dict[str, str],
        audits: list[AuditWriter],
        project_root: str | Path | None = None,
    ) -> None:
        if not sessions or len(sessions) != len(audits):
            raise CtpAdapterError("适配器需要一一对应的会话与审计写入器列表")
        self.sessions = list(sessions)
        self.gateway_setting = gateway_setting
        self.audits = list(audits)
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
        self._startup_position_pending: dict[tuple[str, str], str] = {}
        self._closing_position_pending: dict[tuple[str, str], str] = {}
        self._position_query_attempts = 0
        self._lock = threading.RLock()

    def start(self) -> Any:
        verify_project_gateway(self.project_root)
        try:
            from vnpy.event import EventEngine
            from vnpy.trader.event import (
                EVENT_CONTRACT,
                EVENT_LOG,
                EVENT_ORDER,
                EVENT_TICK,
                EVENT_TRADE,
                EVENT_TIMER,
            )
            from vnpy_ctp import CtpGateway
            from vnpy_ctp.gateway import EVENT_POSITION_QUERY_COMPLETE
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
        engine.register(EVENT_POSITION_QUERY_COMPLETE, self._on_position_query_complete)
        engine.register(EVENT_TIMER, self._on_timer)
        self.main_engine.connect(self.gateway_setting, GATEWAY_NAME)
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
            self._consume(
                TickEvent(
                    symbol=tick.symbol,
                    exchange=tick.exchange.value,
                    last_price=tick.last_price,
                    bid_price=tick.bid_price_1,
                    ask_price=tick.ask_price_1,
                    at=time.monotonic(),
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
                ),
                session,
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
        self._startup_position_pending = {}
        self._closing_position_pending = {}
        self._position_query_attempts = 0
        # 任一待查合约非零仓即整体拒绝：先送达真实净仓事件（非零会话自然失败），
        # 再向全部会话广播中断，零仓与未评估会话都会无委托地终结。
        rejected = bool(result.error_id) or any(nets[key] != 0 for key in startup)
        for key, logical_id in startup.items():
            self._consume(
                self._position_event(key, logical_id, nets[key], result.error_id, result.error_msg),
                self._session_map[key],
            )
        if rejected:
            for session in self.sessions:
                self._consume(InterruptEvent(), session)
        for key, logical_id in closing.items():
            self._consume(
                self._position_event(key, logical_id, nets[key], result.error_id, result.error_msg),
                self._session_map[key],
            )

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
            if (self._startup_position_pending or self._closing_position_pending) and not self._inflight_position_queries:
                self._try_send_position_query()
            # 零仓启动门槛未在全部会话上评估完之前，禁止任何会话靠时钟进入首次报价。
            gate_closed = any(
                session.state in _PREGATE_STATES for session in self.sessions
            )
            for session in self.sessions:
                if gate_closed and session.state == SessionState.WAITING_FOR_STABLE_QUOTE:
                    continue
                self._consume(ClockEvent(time.monotonic()), session)

    def _consume(self, event: object, session: LiveGridSession) -> None:
        with self._lock:
            audit = self._audit_map[(session.target_symbol, session.target_exchange)]
            state_before = session.state.value
            actions = session.handle(event)
            audit.record(
                event,
                actions,
                session.state.value,
                time.monotonic(),
                state_before=state_before,
            )
            for action in actions:
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
            vt_order_id = self.main_engine.send_order(request, GATEWAY_NAME)
            client_id = payload["client_id"]
            if vt_order_id:
                self._client_to_order[vt_order_id.split(".", 1)[-1]] = client_id
            else:
                session = self._session_map[(payload["symbol"], payload["exchange"])]
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
            self.main_engine.cancel_order(
                CancelRequest(
                    orderid=order_id,
                    symbol=payload["symbol"],
                    exchange=Exchange(payload["exchange"]),
                ),
                GATEWAY_NAME,
            )
        elif action.kind == "query_position":
            key = (payload["symbol"], payload["exchange"])
            pending = (
                self._startup_position_pending
                if payload["phase"] == "startup"
                else self._closing_position_pending
            )
            pending[key] = payload["request_id"]
            if not self._inflight_position_queries:
                self._try_send_position_query()

    def _try_send_position_query(self) -> None:
        """Send one account-level query; a refused send is retried on later timer ticks."""
        gateway = self.main_engine.get_gateway(GATEWAY_NAME) if self.main_engine is not None else None
        request_id = gateway.query_position() if gateway is not None else None
        if request_id is not None:
            self._inflight_position_queries.add(request_id)
            self._position_query_attempts = 0
            return
        self._position_query_attempts += 1
        if self._position_query_attempts < POSITION_QUERY_MAX_ATTEMPTS:
            return
        pending = {**self._startup_position_pending, **self._closing_position_pending}
        self._startup_position_pending = {}
        self._closing_position_pending = {}
        self._position_query_attempts = 0
        for key, logical_id in pending.items():
            self._consume(
                PositionQueryCompleteEvent(
                    request_id=logical_id,
                    symbol=key[0],
                    exchange=key[1],
                    net_position=0,
                    error_id=1,
                    error_msg="CTP 持仓查询请求未发送",
                ),
                self._session_map[key],
            )

    @staticmethod
    def _status_name(status: Any) -> str:
        return {
            "提交中": "SUBMITTING",
            "未成交": "NOTTRADED",
            "部分成交": "PARTTRADED",
            "全部成交": "ALLTRADED",
            "已撤销": "CANCELLED",
            "拒单": "REJECTED",
        }.get(getattr(status, "value", status), str(getattr(status, "name", status)))
