"""
The web tier. deploy/serve.sh runs this, and cron runs serve.sh.

Two WSGI wrappers sit between Rocket3 and py4web, and both exist because of one
fact about this host: DreamHost's proxy connects to the server's public IP, not
to loopback, so the port this binds is reachable from the whole internet.

  require_proxy   refuses anything that did not arrive through the proxy, which
                  is what keeps a plain-HTTP copy of the site off the public
                  port even though the port itself is open.
  trust_proxy     tells the app it was reached over HTTPS, so the session
                  cookie keeps its Secure flag behind TLS termination.

Plain `py4web run` serves the same app and is what development uses, but it has
neither wrapper -- do not put it in front of a public port. See DEPLOY.md,
"The shape".
"""

import os
import sys

HOME = os.path.expanduser("~")
CHECKOUT = os.environ.get("NEWFIRE_CHECKOUT", os.path.join(HOME, "newfire"))

# Loopback by default, which is right for development and wrong for the VPS:
# DreamHost's proxy connects to the public IP, so serve.sh overrides this with
# 0.0.0.0 there and require_proxy below carries the weight loopback used to.
HOST = os.environ.get("NEWFIRE_HOST", "127.0.0.1")
PORT = int(os.environ.get("NEWFIRE_PORT", "8123"))

# Set to "0" to serve the port unguarded. Only sane when HOST is loopback, and
# the one way back in if DreamHost ever stops sending the headers below.
REQUIRE_PROXY = os.environ.get("NEWFIRE_REQUIRE_PROXY", "1") == "1"

# What Apache's mod_proxy_http adds on the way through. Any one of them means
# the request came via the proxy; a request straight to the public port has
# none of them. ProxyAddHeaders defaults to On, which is what makes this work,
# and X_FORWARDED_PROTO is checked before trust_proxy invents one.
PROXY_HEADERS = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_X_FORWARDED_SERVER",
    "HTTP_X_FORWARDED_PROTO",
)

sys.path.insert(0, CHECKOUT)

from py4web.core import wsgi  # noqa: E402
from rocket3 import Rocket3  # noqa: E402


def require_proxy(app):
    """
    Refuse requests that did not arrive through the proxy.

    The port has to be open to the internet because DreamHost's proxy dials the
    public IP, so this is what stops `http://<ip>:8123/` from serving the entire
    site in the clear beside the real one: no proxy headers, no answer.

    It is a header check, and headers can be forged -- someone who knows the
    address and sends an X-Forwarded-For gets through. That is worth saying
    plainly, because the guarantee here is weaker than a firewall. What it does
    buy is everything that finds an open port without being told: scanners,
    crawlers, and anyone who reads the IP off a DNS record. It also keeps
    trust_proxy honest, since by the time that runs, HTTPS is no longer an
    assumption about the network but a fact about the request.
    """

    def wrapped(environ, start_response):
        if not any(header in environ for header in PROXY_HEADERS):
            body = b"This site is served over HTTPS at its domain.\n"
            start_response(
                "403 Forbidden",
                [
                    ("Content-Type", "text/plain; charset=utf-8"),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]
        return app(environ, start_response)

    return wrapped


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
    because require_proxy has already run: anything reaching this wrapper came
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
    served = trust_proxy(application)
    if REQUIRE_PROXY:
        # Outside trust_proxy, so the guard runs first and judges the request as
        # it arrived rather than as trust_proxy has since described it.
        served = require_proxy(served)

    guard = "proxy required" if REQUIRE_PROXY else "UNGUARDED"
    print(f"newfire serving on http://{HOST}:{PORT} ({guard})", flush=True)
    Rocket3((HOST, PORT), "wsgi", dict(wsgi_app=served)).start()
