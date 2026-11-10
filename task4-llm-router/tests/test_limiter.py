from __future__ import annotations

import multiprocessing as mp
import threading
import time
from pathlib import Path

import pytest

from llm_router.limiter import RateLimited, RequestTooLarge, SqliteTokenLimiter


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def lim(tmp_path: Path, clock: Clock) -> SqliteTokenLimiter:
    limiter = SqliteTokenLimiter(tmp_path / "rl.db", limit=50_000, window=60.0, clock=clock)
    yield limiter
    limiter.close()


def test_admits_up_to_limit_then_rejects(lim: SqliteTokenLimiter) -> None:
    lim.reserve("k", 30_000)
    lim.reserve("k", 20_000)
    assert lim.usage("k") == 50_000
    with pytest.raises(RateLimited):
        lim.reserve("k", 1)


def test_single_request_over_budget_is_too_large_not_rate_limited(lim: SqliteTokenLimiter) -> None:
    with pytest.raises(RequestTooLarge):
        lim.reserve("k", 50_001)


def test_window_slides_and_old_rows_are_evicted(lim: SqliteTokenLimiter, clock: Clock) -> None:
    lim.reserve("k", 30_000)                # t=0
    clock.t += 30
    lim.reserve("k", 20_000)                # t=30
    with pytest.raises(RateLimited):
        lim.reserve("k", 10_000)
    clock.t += 30.001                       # t=60.001: first row has aged out
    lim.reserve("k", 10_000)
    assert lim.usage("k") == 30_000
    rows = lim._db.execute("SELECT COUNT(*) FROM token_events").fetchone()[0]
    assert rows == 2, "expired row must be physically deleted, not just ignored"


def test_row_exactly_window_old_is_expired(lim: SqliteTokenLimiter, clock: Clock) -> None:
    lim.reserve("k", 50_000)
    clock.t += 60.0
    lim.reserve("k", 50_000)


def test_retry_after_is_when_enough_tokens_expire(lim: SqliteTokenLimiter, clock: Clock) -> None:
    lim.reserve("k", 20_000)                # t=0
    clock.t += 10
    lim.reserve("k", 20_000)                # t=10
    clock.t += 5
    lim.reserve("k", 10_000)                # t=15  -> 50k used
    clock.t += 5                            # t=20
    with pytest.raises(RateLimited) as e1:
        lim.reserve("k", 15_000)            # needs the t=0 row gone -> at t=60
    assert e1.value.retry_after == pytest.approx(40.0)
    with pytest.raises(RateLimited) as e2:
        lim.reserve("k", 35_000)            # needs t=0 and t=10 rows gone -> at t=70
    assert e2.value.retry_after == pytest.approx(50.0)
    clock.t += 40
    lim.reserve("k", 15_000)                # exactly as promised


def test_tenants_are_isolated(lim: SqliteTokenLimiter) -> None:
    lim.reserve("tenant-a", 50_000)
    lim.reserve("tenant-b", 50_000)
    with pytest.raises(RateLimited):
        lim.reserve("tenant-a", 1)


def test_commit_corrects_estimate_and_release_refunds(lim: SqliteTokenLimiter) -> None:
    r1 = lim.reserve("k", 40_000)
    lim.commit(r1, 1_200)                   # actual usage was far below the estimate
    assert lim.usage("k") == 1_200
    r2 = lim.reserve("k", 48_000)
    lim.release(r2)
    assert lim.usage("k") == 1_200


def test_rejected_attempts_do_not_consume_budget(lim: SqliteTokenLimiter) -> None:
    lim.reserve("k", 50_000)
    for _ in range(10):
        with pytest.raises(RateLimited):
            lim.reserve("k", 100)
    assert lim.usage("k") == 50_000


def test_state_is_on_disk_and_survives_restart(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "persist.db"
    a = SqliteTokenLimiter(path, clock=clock)
    a.reserve("k", 45_000)
    a.close()
    assert path.stat().st_size > 0
    b = SqliteTokenLimiter(path, clock=clock)
    with pytest.raises(RateLimited):
        b.reserve("k", 10_000)
    b.close()


def test_raw_api_keys_never_written_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "secret.db"
    lim = SqliteTokenLimiter(path)
    lim.reserve("sk-live-SUPERSECRET-key", 10)
    lim.close()
    blob = b"".join(p.read_bytes() for p in tmp_path.iterdir())
    assert b"SUPERSECRET" not in blob


def test_concurrent_admission_is_exact_across_connections(tmp_path: Path) -> None:
    """60 threads over 6 independent connections race for a 50k budget at
    1,500 tokens each: exactly 33 may get in (33 * 1500 = 49,500)."""
    path = tmp_path / "race.db"
    limiters = [SqliteTokenLimiter(path) for _ in range(6)]
    admitted, rejected = [], []
    barrier = threading.Barrier(60)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            limiters[i % 6].reserve("k", 1_500)
            admitted.append(i)
        except RateLimited:
            rejected.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(60)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 33 and len(rejected) == 27
    assert limiters[0].usage("k") == 49_500
    for limiter in limiters:
        limiter.close()


def test_competing_reservation_inside_check_window_is_serialized(tmp_path: Path) -> None:
    """Deterministic interleaving in the exact danger window: A has summed
    its usage (saw 0) and is about to INSERT. SQLite's trace callback fires
    as A's INSERT starts; it launches B's competing reservation on a separate
    connection and gives it 0.2s. Without a write-locked transaction, B also
    sees 0 used, and both 600-token requests get into a 1,000 budget."""
    path = tmp_path / "interleave.db"
    b = SqliteTokenLimiter(path, limit=1_000)
    a = SqliteTokenLimiter(path, limit=1_000)
    b_result: list[str] = []
    b_thread: list[threading.Thread] = []

    def run_b() -> None:
        try:
            b.reserve("k", 600)
            b_result.append("admitted")
        except RateLimited:
            b_result.append("rejected")

    def on_statement(sql: str) -> None:
        if sql.lstrip().upper().startswith("INSERT") and not b_thread:
            t = threading.Thread(target=run_b)
            b_thread.append(t)
            t.start()
            time.sleep(0.2)  # give B every chance to run inside A's window

    a._db.set_trace_callback(on_statement)
    try:
        a.reserve("k", 600)
        a_result = "admitted"
    except RateLimited:
        a_result = "rejected"
    b_thread[0].join(timeout=15)
    # Either may win the lock; what matters is that exactly one gets in.
    assert sorted([a_result, *b_result]) == ["admitted", "rejected"]
    assert a.usage("k") == 600
    a.close()
    b.close()


def _proc_worker(path: str, n: int, out: mp.Queue) -> None:
    lim = SqliteTokenLimiter(path)
    ok = 0
    for _ in range(n):
        try:
            lim.reserve("k", 1_000)
            ok += 1
        except RateLimited:
            pass
    out.put(ok)


def test_concurrent_admission_is_exact_across_processes(tmp_path: Path) -> None:
    """4 OS processes x 30 attempts of 1,000 tokens: exactly 50 admitted."""
    path = str(tmp_path / "procs.db")
    SqliteTokenLimiter(path).close()  # create schema first
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_proc_worker, args=(path, 30, q)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    assert sum(q.get(timeout=5) for _ in procs) == 50
