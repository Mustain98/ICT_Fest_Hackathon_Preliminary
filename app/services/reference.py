"""Human-facing booking reference codes.

Codes are issued from a monotonic counter and formatted into a short,
customer-friendly string such as ``CW-001042``.

The counter lives in memory, so on startup it is seeded from the highest code
already stored: the database outlives the process, and re-issuing ``CW-001000``
against a populated table would violate the uniqueness of ``reference_code``.
"""
import threading

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Booking

_lock = threading.Lock()
_counter = {"value": 1000}
_seeded = False


def _seed_from_db(db: Session) -> None:
    """Advance the counter past the highest code already persisted."""
    highest = db.query(func.max(Booking.reference_code)).scalar()
    if highest:
        try:
            _counter["value"] = int(highest.split("-")[1]) + 1
        except (IndexError, ValueError):
            pass


def next_reference_code(db: Session) -> str:
    global _seeded
    with _lock:
        if not _seeded:
            _seed_from_db(db)
            _seeded = True
        current = _counter["value"]
        _counter["value"] = current + 1
    return f"CW-{current:06d}"
