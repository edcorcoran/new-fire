#!/bin/sh
# The web tier: py4web on a loopback port, behind DreamHost's Proxy Server.
# Started from cron under flock, which is both the supervisor and the guarantee
# that only one exists. See DEPLOY.md.
set -eu

CHECKOUT="${NEWFIRE_CHECKOUT:-$HOME/newfire}"
VENV="${NEWFIRE_VENV:-$HOME/newfire-venv}"
PORT="${NEWFIRE_PORT:-8123}"

# --app_names is not optional: apps/_default is a symlink to apps/newfire, and
#   without naming one py4web imports the app twice and starts two schedulers.
# --host keeps the socket on loopback, so the only way in is the proxy.
# --dashboard_mode none and --yes because cron has no terminal to prompt at.
exec "$VENV/bin/py4web" run "$CHECKOUT/apps" \
    --app_names _default \
    --host 127.0.0.1 \
    --port "$PORT" \
    --dashboard_mode none \
    --yes
