#!/bin/sh
# The web tier: deploy/serve.py behind DreamHost's Proxy Server.
# Started from cron under flock, which is both the supervisor and the guarantee
# that only one exists. See DEPLOY.md.
set -eu

CHECKOUT="${NEWFIRE_CHECKOUT:-$HOME/newfire}"
VENV="${NEWFIRE_VENV:-$HOME/newfire-venv}"

# 0.0.0.0 rather than loopback because DreamHost's proxy connects to the
# server's public IP -- a backend on 127.0.0.1 gets "Connection refused" and the
# domain answers 503. That leaves the port open to the internet, so serve.py
# refuses anything arriving without the proxy's headers. Overridable, but the
# two settings belong together: do not bind publicly with the guard off.
export NEWFIRE_HOST="${NEWFIRE_HOST:-0.0.0.0}"
export NEWFIRE_PORT="${NEWFIRE_PORT:-8123}"
export NEWFIRE_REQUIRE_PROXY="${NEWFIRE_REQUIRE_PROXY:-1}"
export NEWFIRE_CHECKOUT="$CHECKOUT"

# serve.py rather than `py4web run`: same app, same scheduler (it starts when
# the app is imported), plus the two wrappers a public port needs. It passes
# app_names="_default" itself, which is what stops py4web importing the app
# twice through the symlink and running two schedulers.
exec "$VENV/bin/python" "$CHECKOUT/deploy/serve.py"
