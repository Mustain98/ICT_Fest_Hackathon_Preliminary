"""In-memory response caches for read-heavy reporting endpoints.

Usage reports and per-room availability are relatively expensive to compute and
are read far more often than the underlying data changes, so results are cached
and invalidated when the data they depend on is modified.

Invalidating on write is not sufficient on its own. A reader that misses the
cache, reads the database, and is then descheduled can store its now-stale
snapshot *after* a concurrent writer invalidated the key, poisoning it for every
later request:

    reader:  miss -> read db (0 bookings) ................. set(stale)
    writer:                    commit -> invalidate (no-op, nothing cached)

Each key therefore carries a version. A reader captures the version *before*
reading the database, and its value is only stored if the version has not moved
in the meantime.
"""
import threading

_lock = threading.Lock()

_report_cache: dict[tuple, dict] = {}
_availability_cache: dict[tuple, dict] = {}

# Versions are coarser than the caches: reports are versioned per org (any
# booking change in an org invalidates every date range for it), availability
# per (room, date).
_report_versions: dict[int, int] = {}
_availability_versions: dict[tuple, int] = {}


def report_version(org_id: int) -> int:
    """Capture before reading the database; pass back to :func:`set_report`."""
    with _lock:
        return _report_versions.get(org_id, 0)


def get_report(org_id: int, frm: str, to: str):
    with _lock:
        return _report_cache.get((org_id, frm, to))


def set_report(org_id: int, frm: str, to: str, value: dict, seen_version: int) -> None:
    with _lock:
        if _report_versions.get(org_id, 0) == seen_version:
            _report_cache[(org_id, frm, to)] = value


def invalidate_report(org_id: int) -> None:
    with _lock:
        _report_versions[org_id] = _report_versions.get(org_id, 0) + 1
        for key in [k for k in _report_cache if k[0] == org_id]:
            _report_cache.pop(key, None)


def availability_version(room_id: int, date: str) -> int:
    """Capture before reading the database; pass back to :func:`set_availability`."""
    with _lock:
        return _availability_versions.get((room_id, date), 0)


def get_availability(room_id: int, date: str):
    with _lock:
        return _availability_cache.get((room_id, date))


def set_availability(room_id: int, date: str, value: dict, seen_version: int) -> None:
    key = (room_id, date)
    with _lock:
        if _availability_versions.get(key, 0) == seen_version:
            _availability_cache[key] = value


def invalidate_availability(room_id: int, date: str) -> None:
    key = (room_id, date)
    with _lock:
        _availability_versions[key] = _availability_versions.get(key, 0) + 1
        _availability_cache.pop(key, None)
