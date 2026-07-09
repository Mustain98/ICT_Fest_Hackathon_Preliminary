"""Helpers for parsing input datetimes and rendering UTC responses."""
from datetime import datetime, timezone

from .errors import AppError


def parse_input_datetime(value: str) -> datetime:
    """Parse an ISO 8601 datetime into a naive UTC datetime for storage.

    Inputs that carry a UTC offset are normalized to UTC; naive inputs are
    treated as UTC as-is.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise AppError(400, "INVALID_BOOKING_WINDOW", "Invalid datetime")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


def iso_utc(dt: datetime) -> str:
    """Render a stored (naive UTC) datetime with an explicit UTC designator."""
    return dt.replace(tzinfo=timezone.utc).isoformat()
