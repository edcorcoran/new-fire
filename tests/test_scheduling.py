"""
Tests for the scheduler's guard against running one run twice.

These exist because of a bug that cost two months of wasted work without ever
failing: a task declared with a 24-hour period ran about 490 times a day. A run
claimed by `next_run()` but whose child had not yet recorded its pid was
indistinguishable from one an abandoned worker had left behind, so the recovery
machinery handed it out again, a second child ran the same work, and -- because
pydal gives every completed periodic run a successor -- each double execution
left two standing copies where there had been one. From 2026-09-03 the count
went 1, 2, 4, 7, 11, 18, 26, 43, 66, 95, 139, levelling off near 490 only
because that was as many sweeps a day as the box could finish.

Nothing here forks. The race is driven by hand, which is the point: the
sequence that caused it is three database statements, and it is reproducible in
milliseconds once written down.
"""

import datetime
import logging
import os
import signal
import socket
import sys

import pytest
from pydal import DAL

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "newfire"),
)

import scheduling  # noqa: E402

TASK = "sweep"
PERIOD = 86400


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    """A real ResilientScheduler over an in-memory database.

    Two things about pydal's constructor have to be managed to use it in a
    test at all:

    `self.worker = socket.gethostbyname(socket.gethostname())` -- the worker's
    identity is a DNS lookup of the machine's own name. On a Mac whose
    hostname is "Mac-Studio.local" that spends 35 seconds and then raises
    gaierror, so the scheduler cannot be built here without pinning it. (It is
    also why a worker's name is an IP address, and why the docstring in
    scheduling.py has a story about runs stranded under a 169.254 address: the
    name is not stable, which is the real reason the orphan repair cannot
    match on it.)

    It also sets SIGCHLD to SIG_IGN when built on the main thread, which is
    global to the process; it is put back afterwards so these tests cannot
    change how the rest of the suite reaps children.
    """
    monkeypatch.setattr(socket, "gethostbyname", lambda _name: "10.0.0.1")
    previous = signal.getsignal(signal.SIGCHLD)
    db = DAL("sqlite:memory", folder=str(tmp_path))
    built = scheduling.ResilientScheduler(
        db,
        folder=str(tmp_path / "output"),
        logger=logging.getLogger("test-scheduler"),
    )
    built.register_task(TASK, lambda **_: None)
    yield built
    signal.signal(signal.SIGCHLD, previous)


def enqueue(scheduler, **kwargs):
    kwargs.setdefault("period", PERIOD)
    return scheduler.enqueue_run(TASK, description="a sweep", inputs={}, **kwargs)


def row(scheduler, run_id):
    return scheduler.db.task_run[run_id]


def queued_count(scheduler, status="queued"):
    table = scheduler.db.task_run
    return scheduler.db((table.name == TASK) & (table.status == status)).count()


def requeue_assigned_as_pydal_does(scheduler):
    """pydal's step() does exactly this at the top of every pass.

    It re-queues this worker's own assigned runs without clearing `worker` --
    a sound recovery for a worker that died with runs assigned, and the first
    half of the trouble when it runs while a child is starting.
    """
    table = scheduler.db.task_run
    scheduler.db((table.worker == scheduler.worker) & (table.status == "assigned")).update(
        status="queued"
    )
    scheduler.db.commit()


# ----------------------------------------------------------- the double start


def test_a_queued_run_is_handed_out_and_remembered(scheduler):
    run_id = enqueue(scheduler)
    run = scheduler.next_run()
    assert run is not None and run.id == run_id
    assert run_id in scheduler._handed_off
    assert row(scheduler, run_id).status == "assigned"


def test_the_same_run_is_never_handed_out_twice(scheduler):
    """The regression, driven through the exact sequence that caused it."""
    run_id = enqueue(scheduler)

    first = scheduler.next_run()
    assert first is not None and first.id == run_id

    # The child has been forked but has not recorded its pid yet.
    assert row(scheduler, run_id).pid is None

    # pydal's next pass re-queues it, then the orphan repair would free it.
    requeue_assigned_as_pydal_does(scheduler)
    scheduler.reclaim_orphaned_runs()

    # The repair must leave it alone: this worker is executing it.
    assert row(scheduler, run_id).worker == scheduler.worker

    # And even offered again, it must not start a second time.
    assert scheduler.next_run() is None


def test_the_guard_survives_many_passes_while_a_child_starts(scheduler):
    """A slow child must not be overtaken however long the loop spins.

    The loop does not sleep after starting a run, so these passes happen back
    to back -- which is why the window was hit so often in production.
    """
    run_id = enqueue(scheduler)
    assert scheduler.next_run() is not None

    for _ in range(25):
        scheduler._forget_settled_handoffs()
        requeue_assigned_as_pydal_does(scheduler)
        scheduler.reclaim_orphaned_runs()
        assert scheduler.next_run() is None

    assert run_id in scheduler._handed_off


def test_a_run_that_reached_running_keeps_its_protection(scheduler):
    """However long the task takes, its row is not up for grabs."""
    run_id = enqueue(scheduler)
    scheduler.next_run()
    row(scheduler, run_id).update_record(status="running", pid=4242)
    scheduler.db.commit()

    scheduler._forget_settled_handoffs()
    assert run_id in scheduler._handed_off


def test_a_finished_run_is_forgotten(scheduler):
    run_id = enqueue(scheduler)
    scheduler.next_run()
    row(scheduler, run_id).update_record(status="completed", pid=4242)
    scheduler.db.commit()

    scheduler._forget_settled_handoffs()
    assert scheduler._handed_off == {}


def test_a_fork_that_never_landed_is_released_after_the_grace(scheduler):
    """The one case that must still be recovered: no child ever arrived.

    Without this the guard would be a new way to lose work -- the mirror of the
    bug it fixes.
    """
    run_id = enqueue(scheduler)
    scheduler.next_run()
    requeue_assigned_as_pydal_does(scheduler)

    # Pretend the handoff happened longer ago than the grace period.
    scheduler._handed_off[run_id] = scheduling._utcnow() - (
        scheduling.HANDOFF_GRACE + datetime.timedelta(seconds=1)
    )
    scheduler._forget_settled_handoffs()
    assert run_id not in scheduler._handed_off

    scheduler.reclaim_orphaned_runs()
    again = scheduler.next_run()
    assert again is not None and again.id == run_id


def test_a_run_stranded_by_another_worker_is_still_reclaimed(scheduler):
    """Failure mode 2 must survive the fix for failure mode 4."""
    run_id = enqueue(scheduler)
    row(scheduler, run_id).update_record(status="queued", worker="10.0.0.9")
    scheduler.db.commit()

    scheduler.reclaim_orphaned_runs()
    assert row(scheduler, run_id).worker is None

    taken = scheduler.next_run()
    assert taken is not None and taken.id == run_id


# ----------------------------------------------- exactly one standing run


def test_a_standing_run_is_created_when_there_is_none(scheduler):
    deleted = scheduler.ensure_one_standing_run(
        TASK, description="a sweep", inputs={}, period=PERIOD
    )
    assert deleted == 0
    assert queued_count(scheduler) == 1


def test_surplus_standing_copies_are_collapsed_to_one(scheduler):
    """What production needed: 490 copies down to 1, without a manual script."""
    # Whole seconds: SQLite keeps a datetime to the second, so a value with
    # microseconds on it would not compare equal coming back out.
    earliest = scheduling._utcnow().replace(microsecond=0)
    for offset in range(5):
        enqueue(
            scheduler,
            scheduled_for=earliest + datetime.timedelta(hours=offset),
        )
    assert queued_count(scheduler) == 5

    deleted = scheduler.ensure_one_standing_run(
        TASK, description="a sweep", inputs={}, period=PERIOD
    )
    assert deleted == 4
    assert queued_count(scheduler) == 1

    # The survivor is the one due soonest, so the next sweep is no later than
    # it would have been.
    survivor = scheduler.db(scheduler.db.task_run.name == TASK).select().first()
    assert survivor.scheduled_for == earliest


def test_collapsing_leaves_an_in_flight_copy_alone(scheduler):
    """Deleting the row a child is executing would orphan that child."""
    running_id = enqueue(scheduler)
    row(scheduler, running_id).update_record(status="running", pid=4242)
    scheduler.db.commit()
    for _ in range(3):
        enqueue(scheduler)

    deleted = scheduler.ensure_one_standing_run(
        TASK, description="a sweep", inputs={}, period=PERIOD
    )
    assert deleted == 3
    assert row(scheduler, running_id).status == "running"
    # No queued copy remains: the running one's successor will be the only one.
    assert queued_count(scheduler) == 0


def test_ensuring_one_standing_run_is_idempotent(scheduler):
    for _ in range(3):
        scheduler.ensure_one_standing_run(
            TASK, description="a sweep", inputs={}, period=PERIOD
        )
    assert queued_count(scheduler) == 1
