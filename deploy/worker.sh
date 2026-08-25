#!/bin/sh
# The background worker, started from cron under flock. See DEPLOY.md.
#
# flock is what makes one crontab line both a supervisor and a singleton: while
# a worker holds the lock every subsequent minute's attempt exits immediately,
# and the first minute after one dies -- crash, reboot, or its own scheduled
# recycle -- starts a replacement.
set -eu

CHECKOUT="${NEWFIRE_CHECKOUT:-$HOME/newfire}"
VENV="${NEWFIRE_VENV:-$HOME/newfire-venv}"

# Unset rather than 0: this is the process that must run the loop, and the
# web tier's environment leaking in here would leave nothing draining the queue.
unset NEWFIRE_RUN_SCHEDULER

exec "$VENV/bin/py4web" call "$CHECKOUT/apps" _default.worker.run_worker
