"""Public completion event for CTP position queries."""

from dataclasses import dataclass
from typing import Any


EVENT_POSITION_QUERY_COMPLETE = "ePositionQueryComplete"
EVENT_CTP_CONNECTION = "eCtpConnection"
EVENT_CTP_ORDER_ACTION_ERROR = "eCtpOrderActionError"
EVENT_CTP_ORDER_QUERY_COMPLETE = "eCtpOrderQueryComplete"
EVENT_CTP_TRADE_QUERY_COMPLETE = "eCtpTradeQueryComplete"


@dataclass(frozen=True)
class PositionQueryComplete:
    """Summary emitted once for every completed position query."""

    request_id: int
    positions: tuple[Any, ...]
    error_id: int = 0
    error_msg: str = ""


@dataclass(frozen=True)
class CtpConnection:
    kind: str
    connected: bool
    reason: str = ""


@dataclass(frozen=True)
class CtpOrderActionError:
    orderid: str
    symbol: str
    exchange: str
    error_id: int = 0
    error_msg: str = ""
    action: str = "cancel"


@dataclass(frozen=True)
class CtpOrderQueryComplete:
    request_id: int
    orders: tuple[Any, ...]
    error_id: int = 0
    error_msg: str = ""


@dataclass(frozen=True)
class CtpTradeQueryComplete:
    request_id: int
    trades: tuple[Any, ...]
    error_id: int = 0
    error_msg: str = ""
