# Bug Report — CoWork API

30 bugs, grouped by how hard they were to find. Line numbers refer to the original code.

| Tier | Bugs |
|---|---|
| Hard | H1–H10 |
| Medium | M1–M11 |
| Easy | E1–E6 |
| Smaller things | D1–D3 |

**Before you test anything: `docker compose down -v`.** `create_all` only issues
`CREATE TABLE IF NOT EXISTS`, so the `UNIQUE` index from M1 never appears on an existing database.
Skip the `-v` and that fix is silently missing.

---

## Hard

### H1 — The service deadlocks and stops responding

`app/services/notifications.py:24-35` (Rule 16)

`notify_created` takes the email lock, then the audit lock. `notify_cancelled` takes them the other
way round. Each also sleeps ~0.1 s while holding the outer one, so the window is wide open.

One concurrent create + cancel is all it takes. Thread A holds email and waits for audit; thread B
holds audit and waits for email. Neither lets go. These are `def` endpoints, so they run on a
bounded threadpool — a few more create/cancel pairs and the pool is gone. **Every endpoint stops
responding, including `/health`.**

Running the concurrency probe against the original code doesn't produce a failure message; it just
hangs until the socket times out.

Both functions now take the locks in the same order. I also dropped the `time.sleep` calls: they
made the deadlock easy to hit, and with the ordering fixed they'd still hold a global lock for
0.22 s on every booking mutation, capping the whole service at ~4.5 mutations/second. That's a
separate change from the ordering fix, and I'm calling it out rather than hiding it.

### H2 — Two people can book the same room at the same time

`app/routers/bookings.py:100-117` (Rules 3, 4)

The overlap check, the quota check and the `INSERT` are three separate statements with nothing
holding them together. Two requests both see "no conflict" and both commit. `_pricing_warmup()` and
`_quota_audit()` sleep for 0.22 s in the middle of that window, which is not subtle.

> 10 parallel identical bookings → **10 created**. Should be 1.

A module-level `threading.Lock` now spans conflict check → quota check → reference code → commit.
The sleeps are gone and notifications happen after the lock is released. SQLite only allows one
writer at a time anyway, so this costs nothing real — it just moves the serialization point early
enough to cover the check.

### H3 — Cancelling twice at once refunds twice

`app/routers/bookings.py:195-216`, `app/services/refunds.py:14-27` (Rule 6)

`log_refund` wrote *and committed* the RefundLog before the booking's status became `cancelled`,
with a 0.12 s sleep in between. Two concurrent cancels both pass the `status == "cancelled"` check,
so both write a ledger row and both return 200.

> 10 parallel cancels of one booking → **10 × HTTP 200, 10 RefundLog rows**. Should be 1 and 1.

The cancel path now re-reads the status inside the lock, then writes the status change and the
ledger row in one transaction with a single commit. `log_refund` no longer commits — the caller
owns the transaction.

### H4 — The rate limiter doesn't limit anything under load

`app/services/ratelimit.py:18-26` (Rule 5)

Read the bucket, trim it, sleep 0.1 s, append, write it back. Concurrent requests read the same
list and overwrite each other's appends.

> 50 parallel requests from one user → **42 allowed**. The limit is 20.

The whole read-modify-write now runs under a lock. Note the timestamp is appended *before* the
limit is tested — that's deliberate, since Rule 5 says all requests count, successful or not.

### H5 — Duplicate reference codes

`app/services/reference.py:17-21` (Rule 7)

`current = counter` → sleep 0.12 s → `counter = current + 1`. A textbook lost update.

> 150 concurrent bookings → **136 distinct codes**. 14 collisions.

Guarded with a lock. I also seed the counter from the database on first use: it lives in memory, so
after a restart against a persisted volume it would start over at `CW-001000` and collide with rows
that already exist — which is now a hard error, thanks to M1. After `docker compose restart` the
next code issued is `CW-001249`, not `CW-001000`.

### H6 — A new booking never shows up in the usage report

`app/routers/bookings.py:121` (Rule 12)

`create_booking` invalidates the availability cache but not the report cache. Any report computed
before a booking was made is served forever afterwards. Fixed by invalidating both.

### H7 — A cancelled slot still shows as busy

`app/routers/bookings.py:217` (Rule 13)

The mirror image of H6: `cancel_booking` invalidates the report cache but not availability. Fixed
the same way. Both mutations now clear both caches, after the commit and inside the lock.

### H8 — A slow reader can poison a cache key forever

`app/cache.py` (Rules 12, 13)

Even with H6 and H7 fixed, invalidating on write isn't enough. A reader that misses the cache, reads
the database, and then gets descheduled can store its stale snapshot *after* a writer invalidated
the key:

```
reader:  miss -> read db (0 bookings) ......................... set(stale)
writer:                     commit -> invalidate (nothing cached yet, so a no-op)
```

The stale value lands after the invalidation and is served from then on. Nothing ever clears it.

Every key now carries a version number. `invalidate` bumps it; a reader captures the version before
it reads the database, and `set` only stores the result if the version hasn't moved. Reports are
versioned per org, availability per (room, date).

### H9 — Room stats drift away from the actual bookings

`app/services/stats.py` (Rule 14)

The stats were in-memory counters updated with read → sleep → write, so concurrent bursts lost
updates. They also reset on restart while the database persisted in the volume, and the double
cancel from H3 decremented them twice.

Rule 14 says the numbers must *always* match the bookings. A counter can't promise that, so
`stats.get()` now derives both from the bookings table with a `COUNT` and a `SUM`.

### H10 — A newly created room never appears in a cached report

`app/routers/rooms.py:42-57` (Rule 12)

Rule 12 wants every room in the org listed, *including rooms with zero bookings*. So creating a room
changes the report even though nothing is booked yet. `create_room` invalidated nothing.

> Cache a report showing 1 room, `POST /rooms`, request the same report → **still 1 room**.

I found this one late, by re-reading the rules rather than by testing — my own test suite had the
same blind spot as the code. Fixed by invalidating the report cache when a room is created.

---

## Medium

### M1 — `reference_code` isn't unique

`app/models.py:55` (Rule 7) — indexed, but not `UNIQUE`, so nothing stopped duplicate codes at the
storage layer. Added `unique=True`. Only takes effect on a fresh database (see the note at the top).

### M2 — UTC offsets are thrown away instead of converted

`app/timeutils.py:11-14` (Rule 1)

`dt.replace(tzinfo=None)` strips the offset without moving the clock, so `10:00+05:00` was stored as
`10:00` UTC instead of `05:00` UTC. Every comparison downstream — overlap, quota, refund tier — was
off by the offset. Now `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

### M3 — Logout does nothing at all

`app/auth.py:86, 97` (Rule 8)

`revoke_access_token` adds the token's `jti` to the revoked set. `get_token_payload` then checks
whether `payload.get("sub")` is in that set. A `sub` is never added, so **no token was ever
rejected**. Had it matched, it would have been worse: revoking a `sub` kills every token that user
holds, not the one they presented.

One-word fix: check `jti`.

### M4 — Refresh tokens can be replayed forever

`app/routers/auth.py:81-93` (Rule 8)

`/auth/refresh` issued a new pair but never invalidated the token it was given.

Fixed with `spend_refresh_token()`, which checks and revokes the `jti` under a single lock. The lock
matters: without it, two concurrent refreshes with the same token both pass the check and both
succeed. Reuse now returns 401.

### M5 — Registering an existing username returns 201

`app/routers/auth.py:37-43` (Rule 15)

It returned 201 with the *existing* user's record — leaking that account's id and role to anyone who
guessed the username. Now `409 USERNAME_TAKEN`.

### M6 — Back-to-back bookings are rejected

`app/routers/bookings.py:50` (Rule 3)

`b.start_time <= end and start <= b.end_time`. Non-strict, so a booking starting exactly when
another ends counted as an overlap. The rule is `existing.start < new.end AND new.start <
existing.end`.

Strict `<` on both sides. I also moved the check into SQL — the original pulled every confirmed
booking for the room into Python, and that now runs inside the booking lock, where a slow scan
blocks every other booking request.

### M7 — No minimum duration, and no check that `end > start`

`app/routers/bookings.py:89-94` (Rule 2)

Only `duration > 8` was rejected. `end == start` gives `0.0`, which passes the whole-hour test. And
`end < start` gives `-2.0`, which passes the whole-hour test *and* the `> 8` test — happily creating
a booking with a **negative price**.

Now: reject `end <= start` first, then require `1 <= hours <= 8`. Duration comes from integer
arithmetic on the timedelta rather than float division, so `1 h + 1 µs` is rejected too.

### M8 — Any member can read any colleague's booking

`app/routers/bookings.py:156-163` (Rule 10)

`get_booking` only scoped by org. Pass someone else's booking id and you get their start time,
price and refunds. `cancel_booking` already had the right guard; `get_booking` just didn't.

Added the same two-line check. It returns `404 BOOKING_NOT_FOUND` rather than 403, so the endpoint
doesn't confirm the booking exists.

### M9 — An admin can export another org's bookings

`app/services/export.py:48-52` (Rule 9)

With `include_all=true` **and** a `room_id`, the code called `fetch_bookings_raw`, which filters by
`room_id` and nothing else. No org check.

> `GET /admin/export?include_all=true&room_id=<a room in org B>` as an org A admin → **200, with
> org B's rows.**

Every path now goes through `_fetch_scoped`, which joins `Room` and filters on `org_id`. I deleted
`fetch_bookings_raw` outright — an unscoped query sitting unused is a trap for the next person. An
unknown or cross-org `room_id` returns `404 ROOM_NOT_FOUND`.

### M10 — Refund tiers are wrong three different ways

`app/routers/bookings.py:200-206` (Rule 6)

1. `notice_hours > 48`, so exactly 48 hours' notice fell through to the 50% branch.
2. `int(seconds // 3600)` truncates to whole hours before comparing.
3. The final `else` returns **50** where the rule says **0**. Cancelling ten minutes before your
   booking refunded half the price.

Now the tiers compare `timedelta`s directly: `>= 48h` → 100, `>= 24h` → 50, otherwise 0. Negative
notice lands in the 0% branch, as it should.

### M11 — The refund amount is computed twice, and both are wrong

`app/routers/bookings.py:208`, `app/services/refunds.py:15-17` (Rule 6)

The response used `round(price * pct / 100)`. Python's `round` is banker's rounding, so 50% of 1001
gives `round(500.5)` → **500**. The ledger recomputed it independently via float dollars and `int()`
truncation → also 500. The spec says 501, and it says the response must equal the stored amount —
two independent float paths are free to disagree.

One helper now: `(price * pct + 50) // 100`, integer math. Called once, persisted and returned.

---

## Easy

| # | Where | What | Fix |
|---|---|---|---|
| E1 | `auth.py:50` | `timedelta(minutes=15 * 60)` — access tokens lived 54 000 s, not 900 | `minutes=ACCESS_TOKEN_EXPIRE_MINUTES` |
| E2 | `bookings.py:86` | `start <= now - timedelta(seconds=300)` gave a 5-minute grace window; Rule 2 says none | `if start <= now` |
| E3 | `bookings.py:137` | Sorted by `start_time` descending; Rule 11 says ascending | `.asc()` |
| E4 | `bookings.py:138` | `.offset(page * limit)` skipped the entire first page | `.offset((page - 1) * limit)` |
| E5 | `bookings.py:139` | `.limit(10)` hardcoded, ignoring `limit` | `.limit(limit)` |
| E6 | `bookings.py:166` | Overwrote `start_time` with `created_at` in the detail response | deleted the line |

---

## Smaller things

**D1 — 500 on a malformed datetime.** `app/timeutils.py:11`. `fromisoformat` raised `ValueError`,
and since `schemas.py` types the fields as plain `str`, pydantic never validated them.
`{"start_time": "tomorrow"}` returned HTTP 500 with a non-JSON body. Now `400
INVALID_BOOKING_WINDOW`.

**D2 — 500 when two people register the same new org at once.** `app/routers/auth.py:26-30`. Both
see `org is None`, both insert, one hits the unique constraint. The loser now rolls back, re-selects
the org, and joins it as a member.

**D3 — Cache keys weren't normalized.** `app/routers/admin.py:25`, `app/routers/rooms.py:69`. Both
looked the cache up using the raw query string, so `?date=2026-1-1` and `?date=2026-01-01` got
separate entries and only one was ever invalidated. Dates are parsed and normalized before the key
is built. The response still echoes back exactly what the caller sent.

---

## Requirements addition


`requirements.txt` gained `httpx` and `pytest`. The README documents `pip install -r
requirements.txt && pytest`, and that couldn't work — `TestClient` imports `httpx`, which wasn't
pinned anywhere. Neither package runs in production; the image's `CMD` is `uvicorn`.

## Running it

```bash
docker compose down -v      # required, or the UNIQUE index from M1 won't exist
docker compose up --build
docker compose exec api pytest -q
```
