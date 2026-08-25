"""
Contingency web tier -- not what the deployment runs by default.

deploy/serve.sh runs plain `py4web run`, which is the standard way to serve this
and needs none of the below. This file exists for one failure mode: py4web sets
the Secure flag on the session cookie from the request scheme, and behind a
TLS-terminating proxy the app itself sees plain HTTP. ombott reads
HTTP_X_FORWARDED_PROTO before falling back to the WSGI scheme, so if the proxy
sends that header everything is already correct.

If it does not, the cookie loses Secure with no error and nothing to notice.
Swap serve.sh to run this instead; it forces the header. See DEPLOY.md, "Check
the session cookie", for how to tell which case you are in.
"""

import os
import sys

HOME = os.path.expanduser("~")
CHECKOUT = os.environ.get("NEWFIRE_CHECKOUT", os.path.join(HOME, "newfire"))

# Loopback only. The proxy is the sole way in, so there is no reason for this
# port to be reachable from anywhere else, and binding it publicly would expose
# an unencrypted copy of the site alongside the real one.
HOST = os.environ.get("NEWFIRE_HOST", "127.0.0.1")
PORT = int(os.environ.get("NEWFIRE_PORT", "8123"))

sys.path.insert(0, CHECKOUT)

from py4web.core import wsgi  # noqa: E402
from rocket3 import Rocket3  # noqa: E402


def trust_proxy(app):
    """
    Tell the app it is being reached over HTTPS.

    py4web sets the Secure flag on the session cookie from the request scheme,
    and ombott reads HTTP_X_FORWARDED_PROTO before falling back to the WSGI
    scheme -- so behind a TLS-terminating proxy that forwards the header, this
    is already correct. It is a setdefault rather than an assignment for exactly
    that reason: a proxy that says so is believed.

    The default covers a proxy that stays silent, which would otherwise drop the
    Secure flag with no error and no visible symptom. Assuming HTTPS is safe
    here only because HOST is loopback: nothing can reach this server except
    through the proxy, and the proxy is where the certificate lives.
    """

    def wrapped(environ, start_response):
        environ.setdefault("HTTP_X_FORWARDED_PROTO", "https")
        return app(environ, start_response)

    return wrapped


application = wsgi(
    apps_folder=os.path.join(CHECKOUT, "apps"),
    # One app, mounted at "/" -- and naming it stops py4web importing the same
    # code twice through the _default symlink. See DEPLOY.md, "One app, once".
    app_names="_default",
    dashboard_mode="none",
    yes=True,
)

if __name__ == "__main__":
    print(f"newfire serving on http://{HOST}:{PORT}", flush=True)
    Rocket3((HOST, PORT), "wsgi", dict(wsgi_app=trust_proxy(application))).start()
