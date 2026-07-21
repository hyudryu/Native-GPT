"""Current-time Strands tool. Tool-owned files belong beside this module."""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from strands import tool


@tool
def current_time(timezone: str = "UTC") -> str:
    """Return the current ISO-8601 time for an IANA timezone such as America/Los_Angeles."""

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return f"Unknown timezone: {timezone}"
    return datetime.now(zone).isoformat()


TOOL = current_time
