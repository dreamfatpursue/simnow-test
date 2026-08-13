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
    TickEvent,
    TradeEvent,
)


GATEWAY_NAME = "CTP"


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
        session: LiveGridSession,
        gateway_setting: dict[str, str],
        audit: AuditWriter,
        project_root: str | Path | None = None,
    ) -> None:
        self.session = session
        self.gateway_setting = gateway_setting
        self.audit = audit
        self.project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self.main_engine: Any | None = None
        self._client_to_order: dict[str, str] = {}
        self._query_map: dict[int, str] = {}
        self._lock = threading.RLock()

    def start(self) -> Any:
        verify_project_gateway(self.project_root)
        try:
            from vnpy.event import EventEngine
            from vnpy.trader.event import EVENT_CONTRACT, EVENT_ORDER, EVENT_TICK, EVENT_TRADE, EVENT_TIMER
            from vnpy_ctp import CtpGateway
            from vnpy_ctp.gateway import EVENT_POSITION_QUERY_COMPLETE
        except (ImportError, OSError) as exc:
            raise CtpAdapterError(f"项目内 CTP 网关或原生扩展不可加载: {exc}") from exc

        self.main_engine = __import__("vnpy.trader.engine", fromlist=["MainEngine"]).MainEngine(EventEngine())
        self.main_engine.add_gateway(CtpGateway, GATEWAY_NAME)
        engine = self.main_engine.event_engine
        engine.register(EVENT_CONTRACT, self._on_contract)
        engine.register(EVENT_TICK, self._on_tick)
        engine.register(EVENT_ORDER, self._on_order)
        engine.register(EVENT_TRADE, self._on_trade)
        engine.register(EVENT_POSITION_QUERY_COMPLETE, self._on_position_query_complete)
        engine.register(EVENT_TIMER, self._on_timer)
        self.main_engine.connect(self.gateway_setting, GATEWAY_NAME)
        return self.main_engine

    def interrupt(self) -> None:
        self._consume(InterruptEvent())

    def close(self) -> None:
        with self._lock:
            if self.main_engine is not None:
                self.main_engine.close()
                self.main_engine = None
            self.audit.close()

    def _on_contract(self, event: Any) -> None:
        contract = event.data
        self._consume(ContractEvent(contract.symbol, contract.exchange.value, contract.pricetick))
        if (
            self.main_engine is not None
            and contract.symbol == self.session.target_symbol
            and contract.exchange.value == self.session.target_exchange
        ):
            from vnpy.trader.constant import Exchange
            from vnpy.trader.object import SubscribeRequest

            self.main_engine.subscribe(
                SubscribeRequest(symbol=contract.symbol, exchange=Exchange(contract.exchange.value)),
                GATEWAY_NAME,
            )

    def _on_tick(self, event: Any) -> None:
        tick = event.data
        if tick.symbol != self.session.target_symbol or tick.exchange.value != self.session.target_exchange:
            return
        self._consume(
            TickEvent(
                symbol=tick.symbol,
                exchange=tick.exchange.value,
                last_price=tick.last_price,
                bid_price=tick.bid_price_1,
                ask_price=tick.ask_price_1,
                at=time.monotonic(),
            )
        )

    def _on_order(self, event: Any) -> None:
        order = event.data
        if order.symbol != self.session.target_symbol or order.exchange.value != self.session.target_exchange:
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
            )
        )

    def _on_trade(self, event: Any) -> None:
        trade = event.data
        if trade.symbol != self.session.target_symbol or trade.exchange.value != self.session.target_exchange:
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
            )
        )

    def _on_position_query_complete(self, event: Any) -> None:
        result = event.data
        request_id = self._query_map.pop(result.request_id, None)
        if request_id is None:
            return
        long_volume = 0
        short_volume = 0
        for position in result.positions:
            if position.symbol != self.session.target_symbol or position.exchange.value != self.session.target_exchange:
                continue
            if position.direction.value == "多":
                long_volume += int(position.volume)
            elif position.direction.value == "空":
                short_volume += int(position.volume)
        self._consume(
            PositionQueryCompleteEvent(
                request_id=request_id,
                symbol=self.session.target_symbol,
                exchange=self.session.target_exchange,
                net_position=long_volume - short_volume,
                error_id=result.error_id,
                error_msg=result.error_msg,
            )
        )

    def _on_timer(self, event: Any) -> None:
        self._consume(ClockEvent(time.monotonic()))

    def _consume(self, event: object) -> None:
        with self._lock:
            state_before = self.session.state.value
            actions = self.session.handle(event)
            self.audit.record(
                event,
                actions,
                self.session.state.value,
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
                    )
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
            gateway = self.main_engine.get_gateway(GATEWAY_NAME)
            request_id = gateway.query_position() if gateway is not None else None
            if request_id is not None:
                self._query_map[request_id] = payload["request_id"]
            else:
                self._consume(
                    PositionQueryCompleteEvent(
                        request_id=payload["request_id"],
                        symbol=payload["symbol"],
                        exchange=payload["exchange"],
                        net_position=0,
                        error_id=1,
                        error_msg="CTP 持仓查询请求未发送",
                    )
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
