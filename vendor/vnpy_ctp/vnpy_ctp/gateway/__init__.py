from .position_query import (
    EVENT_CTP_CONNECTION,
    EVENT_CTP_ORDER_ACTION_ERROR,
    EVENT_CTP_ORDER_QUERY_COMPLETE,
    EVENT_CTP_TRADE_QUERY_COMPLETE,
    EVENT_POSITION_QUERY_COMPLETE,
    CtpConnection,
    CtpOrderActionError,
    CtpOrderQueryComplete,
    CtpTradeQueryComplete,
    PositionQueryComplete,
)


__all__ = [
    "CtpGateway",
    "EVENT_POSITION_QUERY_COMPLETE",
    "PositionQueryComplete",
    "EVENT_CTP_CONNECTION",
    "EVENT_CTP_ORDER_ACTION_ERROR",
    "EVENT_CTP_ORDER_QUERY_COMPLETE",
    "EVENT_CTP_TRADE_QUERY_COMPLETE",
    "CtpConnection",
    "CtpOrderActionError",
    "CtpOrderQueryComplete",
    "CtpTradeQueryComplete",
]


def __getattr__(name: str):
    if name == "CtpGateway":
        from .ctp_gateway import CtpGateway

        return CtpGateway
    raise AttributeError(name)
