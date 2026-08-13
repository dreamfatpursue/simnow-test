"""Public completion event for CTP position queries."""

from dataclasses import dataclass
from typing import Any


EVENT_POSITION_QUERY_COMPLETE = "ePositionQueryComplete"


@dataclass(frozen=True)
class PositionQueryComplete:
    """Summary emitted once for every completed position query."""

    request_id: int
    positions: tuple[Any, ...]
    error_id: int = 0
    error_msg: str = ""
