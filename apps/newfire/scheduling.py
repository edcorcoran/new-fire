"""
A scheduler that survives its own accidents.

pydal's Scheduler is fine at running tasks and poor at recovering from anything
going wrong around them. Three failure modes bit this app, all of which stop
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

This subclass fixes 1 and 2 and leaves 3 to whoever wires it up.
"""

import datetime
import time

from pydal.tools.scheduler import Scheduler

# How long a run must sit unclaimed before another worker may take it back.
# pydal marks a run "assigned" and only gives it a pid once the child is forked,
# so a reclaim with no grace period could steal a run mid-handoff. Nothing
# legitimately spends a minute in that gap.
ORPHAN_GRACE = datetime.timedelta(seconds=60)


def _utcnow():
    """Naive UTC, matching what pydal's scheduler stores in task_run."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class ResilientScheduler(Scheduler):
    """A Scheduler whose thread cannot be killed by a database hiccup."""

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
        self.reclaim_orphaned_runs()
        return super().step()

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

        orphaned = db(unstarted & (stranded_queued | stranded_assigned))
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
