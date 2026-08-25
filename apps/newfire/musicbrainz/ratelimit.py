"""
A rate limiter that works across processes.

MusicBrainz asks for an average of one request per second per client, and
enforces it by blocking IPs and User-Agents that ignore it. Staying under that
limit is harder than it looks here, because more than one process wants to call
the API:

  - the web process, filling the cache when a page misses
  - pydal's scheduler, which runs each task in a *forked* process

A module-level token bucket guarded by a threading.Lock would give every one of
those processes its own private allowance, silently multiplying the real request
rate by the number of processes. So the limiter's state lives in SQLite, where
all processes can see it.

The mechanism is slot reservation rather than "wait until the coast is clear":
inside one IMMEDIATE transaction a caller reads the next free slot, claims it,
and pushes the marker forward. It then sleeps until its own slot outside the
transaction, so the lock is held for microseconds and concurrent callers get
consecutive slots instead of colliding on the same one.

Note the X-RateLimit-* headers the API returns are MusicBrainz's global load
shedding counter, not a per-client allowance -- they drop as other people
worldwide make requests, so they cannot be used to pace this client. Hence
self-pacing here.

See docs/musicbrainz-cache-plan.md section 4.5.
"""

import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mb_rate_limit (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_slot_at REAL NOT NULL
)
"""


class RateLimiter:
    """
    Paces outbound requests to a shared minimum interval.

    min_interval is the floor between two requests from this application as a
    whole, not per process.
    """

    def __init__(self, db_path, min_interval=1.0, busy_timeout=10.0):
        self.db_path = db_path
        self.min_interval = min_interval
        self.busy_timeout = busy_timeout
        self._local = threading.local()
        self._prepare()

    def _connect(self):
        """
        A connection belonging to this thread and this process.

        sqlite3 connections cannot cross threads, and a limiter built once at
        import time is then used from every web worker thread — so the
        connection is thread-local rather than an instance attribute. It is also
        keyed on pid, because pydal's scheduler forks: a child inheriting its
        parent's sqlite handle would share file descriptors and corrupt state.

        The connection is deliberately separate from the cache DAL, so a slot
        can be claimed while a sync holds the cache connection mid-batch.
        isolation_level=None hands transaction control to us, so BEGIN IMMEDIATE
        means what it says.
        """
        pid = os.getpid()
        conn = getattr(self._local, "conn", None)

        if conn is not None and getattr(self._local, "pid", None) != pid:
            # Inherited across a fork: abandon it rather than share it.
            conn = None

        if conn is None:
            directory = os.path.dirname(self.db_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            conn = sqlite3.connect(
                self.db_path, timeout=self.busy_timeout, isolation_level=None
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
            self._local.conn = conn
            self._local.pid = pid

        return conn

    def _prepare(self):
        conn = self._connect()
        conn.execute(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO mb_rate_limit (id, next_slot_at) VALUES (1, 0)"
        )

    def acquire(self):
        """
        Reserve the next request slot and block until it arrives.

        Returns how long the caller was made to wait, which is useful for
        logging how much of a sync is spent purely on pacing.
        """
        slot = self._claim_slot()
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)
        return max(0.0, delay)

    def _claim_slot(self):
        """Atomically take the next slot and advance the marker past it."""
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT next_slot_at FROM mb_rate_limit WHERE id = 1"
            ).fetchone()
            slot = max(time.time(), row[0] if row else 0.0)
            conn.execute(
                "UPDATE mb_rate_limit SET next_slot_at = ? WHERE id = 1",
                (slot + self.min_interval,),
            )
            conn.execute("COMMIT")
            return slot
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def penalize(self, seconds):
        """
        Push every process's next slot back after being throttled.

        A 503 means MusicBrainz is refusing this client, so the whole
        application should slow down -- not just whichever process happened to
        receive the rejection.
        """
        if seconds <= 0:
            return
        conn = self._connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT next_slot_at FROM mb_rate_limit WHERE id = 1"
            ).fetchone()
            resume_at = max(time.time(), row[0] if row else 0.0) + seconds
            conn.execute(
                "UPDATE mb_rate_limit SET next_slot_at = ? WHERE id = 1",
                (resume_at,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self):
        """Close this thread's connection, if it has one."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
