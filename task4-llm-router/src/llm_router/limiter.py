"""Token-aware sliding-window rate limiter backed by an on-disk SQLite file.

Model: a *sliding log*. Every admitted request is one row
`(tenant, ts, tokens)`. A tenant's usage is the sum of `tokens` over rows
with `ts > now - window`. That is exact; no fixed-bucket edge bursts where
2x the limit fits across a minute boundary.

Lifecycle of one request:

    reserve(tenant, estimate)  -> row inserted with the *estimated* cost
    commit(id, actual)         -> row corrected to the provider's real usage
    release(id)                -> row deleted (request failed / was cancelled;
                                  no tokens were consumed)

Reserving up front is what makes concurrent admission correct: N requests
racing for the last 5k tokens can't all see "45k used" and all get in.

Atomicity: each check-and-reserve runs inside `BEGIN IMMEDIATE`, which takes
SQLite's write lock *before* reading the sum. That serializes admission
across threads and across processes sharing the file (several gateway
workers), not only within one Python process. WAL mode keeps readers off the
writer's path, and `busy_timeout` makes contenders wait instead of erroring.

Eviction: rows at or older than the window are deleted inside the same
transaction, for all tenants (index on `ts`), so idle tenants don't leave
garbage behind and the table stays at roughly one minute of traffic.

Tenant API keys are stored as SHA-256 digests, never in plaintext on disk.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_events (
    id      INTEGER PRIMARY KEY,
    tenant  TEXT    NOT NULL,
    ts      REAL    NOT NULL,
    tokens  INTEGER NOT NULL CHECK (tokens >= 0)
);
CREATE INDEX IF NOT EXISTS ix_token_events_tenant_ts ON token_events (tenant, ts);
CREATE INDEX IF NOT EXISTS ix_token_events_ts ON token_events (ts);
"""


class RateLimited(Exception):
    def __init__(self, retry_after: float, used: int, limit: int) -> None:
        super().__init__(f"rate limited; retry after {retry_after:.2f}s")
        self.retry_after = retry_after
        self.used = used
        self.limit = limit


class RequestTooLarge(Exception):
    """A single request costs more than the whole per-window budget: it can
    never be admitted, so retrying is pointless (unlike RateLimited)."""


@dataclass(frozen=True)
class Reservation:
    id: int
    tenant: str
    tokens: int


def tenant_id(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


class SqliteTokenLimiter:
    def __init__(
        self,
        path: str | Path,
        limit: int = 50_000,
        window: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window = window
        self._clock = clock
        # One connection per limiter, used from worker threads (the router
        # calls in via asyncio.to_thread); the lock keeps it single-user.
        # Cross-process exclusion is SQLite's job (BEGIN IMMEDIATE).
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=10.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _evict(self, now: float) -> None:
        self._db.execute("DELETE FROM token_events WHERE ts <= ?", (now - self.window,))

    def reserve(self, api_key: str, tokens: int) -> Reservation:
        if tokens < 0:
            raise ValueError("tokens must be >= 0")
        if tokens > self.limit:
            raise RequestTooLarge(f"request needs {tokens} tokens; limit is {self.limit} per {self.window:g}s")
        tenant = tenant_id(api_key)
        with self._lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                now = self._clock()
                self._evict(now)
                used = db.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM token_events WHERE tenant = ?", (tenant,)
                ).fetchone()[0]
                if used + tokens > self.limit:
                    retry_after = self._retry_after(tenant, used, tokens, now)
                    db.execute("COMMIT")  # keep the eviction
                    raise RateLimited(retry_after, used, self.limit)
                cur = db.execute(
                    "INSERT INTO token_events (tenant, ts, tokens) VALUES (?, ?, ?)", (tenant, now, tokens)
                )
                db.execute("COMMIT")
                return Reservation(id=cur.lastrowid, tenant=tenant, tokens=tokens)
            except RateLimited:
                raise
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def _retry_after(self, tenant: str, used: int, tokens: int, now: float) -> float:
        """Earliest moment enough of the oldest rows age out for `tokens` to fit."""
        need_to_free = used + tokens - self.limit
        freed = 0
        for ts, row_tokens in self._db.execute(
            "SELECT ts, tokens FROM token_events WHERE tenant = ? ORDER BY ts", (tenant,)
        ):
            freed += row_tokens
            if freed >= need_to_free:
                return max(0.0, ts + self.window - now)
        return self.window  # unreachable while tokens <= limit; safe default

    def commit(self, reservation: Reservation, actual_tokens: int) -> None:
        """Replace the estimate with real usage (may be higher or lower)."""
        with self._lock:
            self._db.execute(
                "UPDATE token_events SET tokens = ? WHERE id = ?", (max(0, int(actual_tokens)), reservation.id)
            )

    def release(self, reservation: Reservation) -> None:
        with self._lock:
            self._db.execute("DELETE FROM token_events WHERE id = ?", (reservation.id,))

    def usage(self, api_key: str) -> int:
        with self._lock:
            now = self._clock()
            return self._db.execute(
                "SELECT COALESCE(SUM(tokens), 0) FROM token_events WHERE tenant = ? AND ts > ?",
                (tenant_id(api_key), now - self.window),
            ).fetchone()[0]
