"""
A scheduler that survives its own accidents.

pydal's Scheduler is fine at running tasks and poor at recovering from anything
going wrong around them. Four failure modes bit this app. The first three stop
background work silently — the app keeps serving pages, jobs simply stop
running, and nothing says so:

1. `loop()` has no exception guard around `step()`. One transient
   `database is locked` — the scheduler polls task_run continuously while web
   threads enqueue and forked children report status, so contention happens —
   propagates out of the thread and background processing is finished until the
   process restarts. The logging call in `loop()` itself queries the database
   too, so guarding only `step()` would not be enough.

2. A run that was assigned but never started is orphaned forever. The recovery
   in `step()` resets its status to "queued" but leaves `worker` set, while
   `next_run()` only ever considers rows with `worker IS NULL`. The row stays in
   the queue looking perfectly healthy and is never picked up again. Worse, that
   recovery only matches the worker's own name, so a worker killed between
   claiming a run and forking it strands the row under a name nothing will
   answer to — and since the name is an IP address, "nothing" includes the same
   machine after a DHCP change.

3. Tasks must be registered before the loop starts. `step()` marks any run whose
   name it does not recognise as "unknown" and drops it, so a scheduler polling
   before the app has registered its handlers quietly discards queued work. That
   ordering is enforced by the caller — see tasks.py — rather than here.

The fourth does the opposite, and is worse for being invisible: instead of
stopping, background work multiplies.

4. A run can be executed twice, and a *periodic* run that executes twice
   enqueues two successors — so one standing daily job quietly becomes two,
   then four. `step()` re-queues this worker's own "assigned" rows at the top of
   every pass, which is a sound recovery for a worker that died with runs
   assigned and a bug when applied continuously: between `next_run()` claiming a
   run and the forked child marking it "running", the row sits "assigned" with
   no pid, and the loop does not sleep after starting a run, so the very next
   pass flips it back to "queued". The repair in 2 below then clears its worker,
   `next_run()` offers it again, and a second child runs the same work.

   This is not theoretical. It ran in production from 2026-09-03: a task
   declared with a 24-hour period grew by about half again each day — 1, 2, 4,
   7, 11, 18, 26, 43, 66, 95, 139 standing copies — until it levelled off at
   ~490, where the only thing stopping it was how many sweeps a day the box
   could finish. The daily label check had become continuous, each copy making
   its own rate-limited MusicBrainz requests, and `task_run` reached 23,000 rows
   against 82 followed labels. Nothing failed; there was no error anywhere. The
   app simply did a day's work several hundred times a day.

This subclass fixes 1, 2 and 4, and leaves 3 to whoever wires it up.
"""

import datetime
import time

from pydal.tools.scheduler import Scheduler

# How long a run must sit unclaimed before another worker may take it back.
# pydal marks a run "assigned" and only gives it a pid once the child is forked,
# so a reclaim with no grace period could steal a run mid-handoff. Nothing
# legitimately spends a minute in that gap.
ORPHAN_GRACE = datetime.timedelta(seconds=60)

# How long a run this worker has handed to a child may go without that child
# reporting a pid before we conclude the fork never landed and let the run be
# picked up again. `safe_exec_child` records its pid as its first act, so this
# only ever elapses when the fork itself failed -- and until it does, the run is
# protected from being started a second time. See failure mode 4.
HANDOFF_GRACE = datetime.timedelta(seconds=60)

# Statuses in which a run is still this worker's responsibility. "queued" is in
# the list because that is precisely the state `step()` puts a run in while its
# child is starting, which is the whole trouble in failure mode 4.
IN_HAND_STATUSES = ("queued", "assigned", "running")


def _utcnow():
    """Naive UTC, matching what pydal's scheduler stores in task_run."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class ResilientScheduler(Scheduler):
    """A Scheduler whose thread cannot be killed by a database hiccup."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Runs this process has forked a child for: id -> when it was handed
        # over. The authority on "already being executed here", which the
        # task_run row cannot be, since the row is briefly indistinguishable
        # from an abandoned one. Bounded by max_concurrent_runs in practice,
        # and pruned every pass by _forget_settled_handoffs.
        self._handed_off = {}

    def loop(self):
        """
        Poll for work until asked to stop, treating every error as transient.

        Overriding the whole loop rather than just step() because pydal's version
        also queries the database to log its progress, which can fail for exactly
        the same reasons the work itself can.
        """
        self.logger.info("scheduler loop starting (worker %s)", self.worker)
        while self._looping:
            try:
                if not self.step():
                    time.sleep(self.sleep_time)
            except Exception:
                # Deliberately broad. Whatever went wrong, the correct response
                # is to abandon this pass and try again, never to take the
                # scheduler down with it.
                self.logger.exception("scheduler step failed; retrying")
                self._safe_rollback()
                time.sleep(self.sleep_time)
        self.logger.info("scheduler loop stopped (worker %s)", self.worker)

    def step(self):
        """Reclaim orphaned runs, then take one pydal step."""
        self._forget_settled_handoffs()
        self.reclaim_orphaned_runs()
        return super().step()

    def next_run(self):
        """
        Take the next run, unless this worker is already running it.

        `super().next_run()` decides what to run from the row alone, and a row
        whose child is still starting is indistinguishable from one some dead
        worker abandoned: both are available-looking. This process knows the
        difference, because it is the one that forked the child, so the memory
        in `_handed_off` is the authority rather than the row.

        Declining returns None, which costs this pass and nothing else — the
        run stays assigned to us, which is the truth, and the child that is
        already executing it will report its own completion. Handing it out
        again would run the same work twice, and for a periodic run that means
        two successors where there should be one. See failure mode 4.
        """
        run = super().next_run()
        if run is None:
            return None
        if run.id in self._handed_off:
            self.logger.warning(
                "run #%i %s came back around while its child was starting; "
                "not starting it twice",
                run.id,
                run.name,
            )
            return None
        self._handed_off[run.id] = _utcnow()
        return run

    def _forget_settled_handoffs(self):
        """
        Stop protecting runs that are no longer in this worker's hands.

        Three ways a remembered handoff settles: the row reached a terminal
        status (its child reported, one way or another), the row is gone, or no
        child ever recorded a pid and HANDOFF_GRACE has passed -- a fork that
        did not land. Only the last of those releases a run that nothing
        executed, which is why it waits for the grace period instead of
        guessing.

        A run that reached "running" keeps its protection for as long as it
        holds that status, however long the task takes; pydal's own step()
        notices a child whose pid has gone and marks the run "dead", which
        settles it here on the next pass.
        """
        if not self._handed_off:
            return
        table = self.db.task_run
        rows = self.db(table.id.belongs(list(self._handed_off))).select(
            table.id, table.status, table.pid
        )
        current = {row.id: row for row in rows}
        for run_id, handed_at in list(self._handed_off.items()):
            row = current.get(run_id)
            if row is None or row.status not in IN_HAND_STATUSES:
                del self._handed_off[run_id]
            elif row.pid is None and _utcnow() - handed_at >= HANDOFF_GRACE:
                self.logger.warning(
                    "run #%i was handed to a child %ss ago and no pid was ever "
                    "recorded; releasing it to be started again",
                    run_id,
                    int(HANDOFF_GRACE.total_seconds()),
                )
                del self._handed_off[run_id]

    def ensure_one_standing_run(self, name, **kwargs):
        """
        Make a periodic task have exactly one standing run, not merely at least
        one.

        The caller's intent -- see tasks.py -- is one standing run per periodic
        task. Checking "is there one?" and enqueueing if not leaves the
        invariant hoped-for rather than true: anything that adds a second copy
        (failure mode 4, or two processes racing the check) is permanent,
        because pydal gives every completed periodic run a successor and so
        every surplus copy renews itself forever.

        So enforce it on every start instead. Surplus *queued* copies are
        deleted; an assigned or running copy is never touched, since a child may
        be executing it and its completion will produce the one successor that
        should remain. Of several queued copies the earliest `scheduled_for`
        survives, so the next sweep happens no later than it would have.

        Returns the number of surplus runs deleted, so a caller can log a
        collapse that mattered.
        """
        table = self.db.task_run
        pending = self.db(
            (table.name == name) & (table.status.belongs(IN_HAND_STATUSES))
        ).select(orderby=table.scheduled_for | table.id)

        if not pending:
            self.enqueue_run(name, **kwargs)
            return 0

        # An in-flight copy is the keeper: it is about to produce a successor,
        # and deleting its row would orphan the child executing it.
        in_flight = [row for row in pending if row.status in ("assigned", "running")]
        queued = [row for row in pending if row.status == "queued"]
        surplus = queued if in_flight else queued[1:]
        if not surplus:
            return 0

        deleted = self.db(table.id.belongs([row.id for row in surplus])).delete()
        self.db.commit()
        self.logger.warning(
            "collapsed %i surplus standing run(s) of %s; one remains",
            deleted,
            name,
        )
        return deleted

    def reclaim_orphaned_runs(self):
        """
        Return abandoned runs to the queue properly.

        A row that has never been given a pid is not executing anywhere, so
        clearing its worker is safe no matter which worker claimed it. Matching
        on pid rather than on this worker's name matters because the name is an
        IP address, and a machine that picked up a self-assigned address will
        not recognise its own earlier runs after a restart — which is exactly
        what happened here: runs stranded under a self-assigned 169.254.x
        address were invisible to the same machine once it came back on its
        normal LAN address.

        Covers "assigned" as well as "queued". pydal claims a run by marking it
        assigned and only records a pid once it has forked the child, so a
        worker killed in that window leaves a row its own recovery will not
        touch either — `step()` only re-queues rows matching its own name, and
        `next_run()` only considers rows with no worker at all. Such a run is
        stranded for good, and because queue_label_sync treats "assigned" as
        outstanding, that label can never be queued again.
        """
        db = self.db
        table = db.task_run
        # No pid means no child was ever forked, so nothing is executing this
        # row and taking it back cannot interrupt work in progress.
        unstarted = (table.worker != None) & (table.pid == None)  # noqa: E711

        # A queued row belongs to nobody regardless of whose name is on it:
        # pydal's own recovery re-queues without clearing worker, and that is
        # precisely the state next_run() cannot see.
        stranded_queued = table.status == "queued"

        # An assigned row is only stranded if some *other* worker claimed it.
        # pydal re-queues its own assigned rows at the top of every step, so
        # ours self-heal — and reclaiming them here would race the gap between
        # assigning a run and forking its child, stealing work about to start.
        # queued_on is not refreshed on re-assignment, so it cannot stand in for
        # "assigned a while ago"; it only bounds how long a dead worker's row
        # lingers before another one may take it.
        stranded_assigned = (
            (table.status == "assigned")
            & (table.worker != self.worker)
            & (table.queued_on < _utcnow() - ORPHAN_GRACE)
        )

        query = unstarted & (stranded_queued | stranded_assigned)

        # Never reclaim a run this process has already forked a child for. The
        # "queued with a worker still on it" state above is exactly what step()
        # leaves behind while a child is starting, so without this the repair
        # becomes the cause: it frees the row, next_run() offers it again, and
        # the work runs twice. next_run() declines the duplicate as well --
        # belt and braces, since that is the path that multiplies periodic runs.
        if self._handed_off:
            query &= ~table.id.belongs(list(self._handed_off))

        orphaned = db(query)
        count = orphaned.count()
        if count:
            orphaned.update(worker=None, status="queued")
            db.commit()
            self.logger.warning("returned %i orphaned run(s) to the queue", count)

    def _safe_rollback(self):
        try:
            self.db.rollback()
        except Exception:
            # A rollback that fails means the connection is unusable; pydal opens
            # a fresh one on next use, so there is nothing useful to do here.
            self.logger.debug("rollback after scheduler error failed", exc_info=True)
