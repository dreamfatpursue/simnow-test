from .position_query import EVENT_POSITION_QUERY_COMPLETE, PositionQueryComplete


__all__ = ["CtpGateway", "EVENT_POSITION_QUERY_COMPLETE", "PositionQueryComplete"]


def __getattr__(name: str):
    if name == "CtpGateway":
        from .ctp_gateway import CtpGateway

        return CtpGateway
    raise AttributeError(name)
