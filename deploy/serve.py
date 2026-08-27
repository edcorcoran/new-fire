"""
The web tier. deploy/serve.sh runs this, and cron runs serve.sh.

Two WSGI wrappers sit between Rocket3 and py4web, and both exist because of one
fact about this host: DreamHost's proxy connects to the server's public IP, not
to loopback, so the port this binds is reachable from the whole internet.

  require_proxy   refuses anything that did not arrive through the proxy, which
                  is what keeps a plain-HTTP copy of the site off the public
                  port even though the port itself is open. Optionally also
                  pins the connection's source address -- see PROXY_IPS.
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
# none of them. ProxyAddHeaders defaults to On, which is what makes this work.
# X_FORWARDED_PROTO is listed for completeness rather than because Apache sends
# it -- ProxyAddHeaders does not -- and require_proxy reads the request as it
# arrived, before trust_proxy sets that header itself.
PROXY_HEADERS = (
    "HTTP_X_FORWARDED_FOR",
    "HTTP_X_FORWARDED_HOST",
    "HTTP_X_FORWARDED_SERVER",
    "HTTP_X_FORWARDED_PROTO",
)

# The addresses the proxy dials in from, comma-separated. Empty by default, and
# empty means the header check above stands alone -- this is a second gate on
# require_proxy, never a replacement for it. Unlike the headers, a source
# address cannot be forged on a request that completed a TCP handshake, so
# setting this turns a guard anyone can talk their way past into one that also
# has to be reached from the right machine.
#
# Off by default on purpose: the failure mode of a wrong value is every request
# to the site answering 403, which is a worse day than the weakness it closes.
# DEPLOY.md, "Pinning the proxy's address", says how to read the right value off
# the running server before trusting it.
PROXY_IPS = frozenset(
    ip.strip()
    for ip in os.environ.get("NEWFIRE_PROXY_IPS", "").split(",")
    if ip.strip()
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

    Setting NEWFIRE_PROXY_IPS closes most of that gap. REMOTE_ADDR is the far
    end of the TCP connection, filled in by Rocket3 from the accepted socket
    rather than read out of the request, so a client cannot claim someone
    else's -- an attacker who spoofed it would never see the handshake finish,
    let alone send a request. Requiring it to be the proxy's address *as well
    as* the headers means forging the headers is no longer enough; you would
    have to be on the machine the proxy runs on. It is additional and opt-in,
    so with the variable unset this behaves exactly as it always has.
    """

    def wrapped(environ, start_response):
        forwarded = any(header in environ for header in PROXY_HEADERS)
        # An empty allowlist waves everything through here, which is what keeps
        # the address check strictly additional.
        from_allowed_address = not PROXY_IPS or environ.get("REMOTE_ADDR") in PROXY_IPS
        if not forwarded or not from_allowed_address:
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


def trust_proxy(app, force=REQUIRE_PROXY):
    """
    Tell the app it is being reached over HTTPS.

    py4web sets the Secure flag on the session cookie from the request scheme,
    and ombott reads HTTP_X_FORWARDED_PROTO before falling back to the WSGI
    scheme. Behind TLS termination the app is spoken to in plain HTTP, so this
    header is the only thing between the session cookie and being issued
    without Secure -- and a cookie that loses Secure is offered up on the first
    plaintext request to the domain.

    This was a setdefault, on the reasoning that a proxy which sends the header
    should be believed. What that misses is which way the failure falls. A
    proxy sending "X-Forwarded-Proto: http" would take the Secure flag off,
    silently, with no error and nothing in the log -- so the deferential
    reading of the header is also the dangerous one.

    Deferring would only be right if a plaintext request could arrive here at
    all, and on this host none can:

      - Apache adds X-Forwarded-For, -Host and -Server (ProxyAddHeaders sets
        those three) and *not* -Proto, so nothing upstream is currently
        expressing an opinion for us to defer to.
      - The panel's vhost answers port 80 with a 301 to https before it proxies
        anything. Verified against the live site: `curl -sI http://newfire.music/`
        is `301 Moved Permanently` from Apache, on `/` and on deeper paths, so
        a request that reached the proxy in the clear is redirected rather than
        forwarded, and never becomes a request to this process.
      - require_proxy has already refused anything that did not come through
        the proxy.

    So every request that gets here arrived at the proxy over TLS, and "https"
    is a fact about the request rather than a guess about the network. The
    header is overwritten accordingly.

    It is scoped to the guard, though, because the guard is what establishes
    all of the above. With NEWFIRE_REQUIRE_PROXY=0 this process may be talking
    to anyone, none of that reasoning holds, and this falls back to the old
    setdefault -- which is also what keeps development on loopback behaving as
    it did.
    """

    def wrapped(environ, start_response):
        if force:
            environ["HTTP_X_FORWARDED_PROTO"] = "https"
        else:
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
    if REQUIRE_PROXY and PROXY_IPS:
        # Printed so a typo in NEWFIRE_PROXY_IPS is one line in the log rather
        # than a site that 403s everything for no stated reason.
        guard += " from " + ", ".join(sorted(PROXY_IPS))
    print(f"newfire serving on http://{HOST}:{PORT} ({guard})", flush=True)
    Rocket3((HOST, PORT), "wsgi", dict(wsgi_app=served)).start()
