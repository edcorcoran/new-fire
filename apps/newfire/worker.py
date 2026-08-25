"""
The background worker process.

Everything in tasks.py runs here rather than inside a web process. Passenger --
the application server this is deployed behind, see DEPLOY.md -- decides for
itself how many web processes exist and reaps them when traffic is quiet, which
is the opposite of what a scheduler wants: several loops competing at lunchtime
and none at all at 4am, when the nightly sweep is due. So web processes only
enqueue work, and one of these drains the queue.

Started from cron under `flock`, which is what guarantees the "one of these"
and lets the same crontab line double as a watchdog: while the worker is alive
the lock is held and each minute's attempt exits immediately, and whenever it is
not -- crash, reboot, or the recycle below -- the next minute starts a new one.

Run it with py4web's own loader so the app is imported exactly as the web
process imports it:

    py4web call apps newfire.worker.run_worker

Importing this module is what starts the scheduler, by way of the package
__init__ reaching tasks.py. run_worker only supervises what that already began.
"""

import signal
import threading

from .common import logger, scheduler
from . import settings

# How long a worker lives before standing down for a fresh one. Nothing is known
# to leak; this is insurance against the unknown kind, and against a loop wedged
# in a state its own error handling did not anticipate. Cron replaces it within
# the minute, so the cost of being wrong about the interval is close to zero.
DEFAULT_MAX_LIFETIME = 6 * 60 * 60


def run_worker(max_lifetime=DEFAULT_MAX_LIFETIME):
    """
    Keep the scheduler loop alive, then shut it down cleanly.

    Returns rather than raising on a misconfiguration, because cron's idea of
    reporting a failure is an email nobody reads. The log line is the signal.
    """
    if not settings.USE_SCHEDULER:
        logger.error("worker started but USE_SCHEDULER is off; nothing to run")
        return

    if not settings.RUN_SCHEDULER:
        # The web process's environment leaking into cron's would strand the
        # queue: enqueued work, and nothing anywhere executing it.
        logger.error(
            "worker started but RUN_SCHEDULER is off -- NEWFIRE_RUN_SCHEDULER is "
            "set to 0 in this environment, which is the web process's setting"
        )
        return

    stopping = threading.Event()

    def request_stop(signum, _frame):
        # Only ever sets the flag. Stopping the scheduler joins its thread, and
        # doing that from a signal handler risks deadlocking against whatever
        # the loop happened to hold when the signal arrived.
        logger.info("worker received signal %s; finishing current step", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    logger.info("worker up (max lifetime %ss)", max_lifetime)

    # wait() returns True on a signal and False on timeout; either way the next
    # step is the same orderly shutdown, so the result is not worth branching on.
    if not stopping.wait(timeout=max_lifetime):
        logger.info("worker reached its max lifetime; standing down for a fresh one")

    # Takes up to the scheduler's sleep_time -- ten seconds by default -- since
    # stopping joins the loop thread and the loop only rechecks the flag after
    # its sleep. A worker therefore overlaps the next cron attempt by a few
    # seconds while shutting down, which is precisely what the lock is for.
    scheduler.stop()
    logger.info("worker down")
