"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.

The amount is computed once, by :func:`refund_amount_cents`, and both the API
response and the ledger row use that single value so they cannot disagree.
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog


def refund_percent_for(notice: timedelta) -> int:
    """Refund tier for the notice given before ``start_time``."""
    if notice >= timedelta(hours=48):
        return 100
    if notice >= timedelta(hours=24):
        return 50
    return 0


def refund_amount_cents(price_cents: int, percent: int) -> int:
    """Percentage of ``price_cents``, nearest cent, half-cents rounding up.

    Integer arithmetic on purpose: ``round`` is banker's rounding (500.5 -> 500)
    and ``int`` truncates, both of which understate a half-cent refund.
    """
    return (price_cents * percent + 50) // 100


def log_refund(db: Session, booking: Booking, amount_cents: int) -> RefundLog:
    """Add the ledger row. The caller owns the transaction and commits it."""
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    return entry
