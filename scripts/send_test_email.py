#!/usr/bin/env python
"""
Send one message through the app's own SMTP configuration and say what happened.

Why this exists: the only route that sends mail is a password reset, and testing
it means locking yourself out of an account to watch a form either work or throw
a 500. This builds the same Mailer common.py builds, from the same settings, and
sends to whatever address you name -- so the mailbox password, the port and the
TLS mode are all proved before a stranger's reset request depends on them.

    # on the VPS, after writing the SMTP settings into settings_private.py
    ~/newfire-venv/bin/python scripts/send_test_email.py you@example.com

    # locally, where SMTP_SERVER is "logging", this prints the message instead
    .venv/bin/python scripts/send_test_email.py you@example.com

Exit status is 0 only if the server accepted the message. Accepted is not the
same as delivered: mail from a new domain can be accepted here and filed as spam
there, so check the inbox too the first time.
"""

import argparse
import importlib.util
import os
import sys
import types

APP_FOLDER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps", "newfire"
)


def load_app_module(name):
    """Import one module out of the app without importing the app.

    `import newfire.settings` would run the package __init__, which imports
    controllers, opens both databases and builds the scheduler -- minutes of
    machinery, and a second writer against storage.db, to read five constants.
    But settings.py ends in `from .settings_private import *`, a relative import
    that needs a parent package to resolve, and one that fails *silently*: it is
    wrapped in try/except ImportError, so loading settings.py as a lone file
    would skip every private setting and test a configuration nobody runs.

    So: a stub package whose __path__ is the app folder. Relative imports inside
    it resolve against the real files, and nothing else is imported.
    """
    package = "newfire_standalone"
    if package not in sys.modules:
        stub = types.ModuleType(package)
        stub.__path__ = [APP_FOLDER]
        sys.modules[package] = stub
    full_name = "%s.%s" % (package, name)
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(
        full_name, os.path.join(APP_FOLDER, name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def describe(settings):
    """The configuration as the app sees it, with the password withheld."""
    login = settings.SMTP_LOGIN
    if login and ":" in login:
        username, password = login.split(":", 1)
        shown = "%s:%s" % (username, "*" * len(password))
    else:
        shown = login
    return [
        ("SMTP_SERVER", settings.SMTP_SERVER),
        ("SMTP_SENDER", settings.SMTP_SENDER),
        ("SMTP_LOGIN", shown),
        ("SMTP_SSL", settings.SMTP_SSL),
        ("SMTP_TLS", settings.SMTP_TLS),
        ("SMTP_TIMEOUT", settings.SMTP_TIMEOUT),
    ]


# Failures cluster into a few kinds, and the SMTP library's own wording does not
# always name the cause. Matched on the exception class name so importing
# smtplib is not needed to read the table.
HINTS = {
    "SMTPAuthenticationError": (
        "The server rejected the username or password. On a DreamHost mailbox "
        "the username is the full address, not the part before the @."
    ),
    "SMTPSenderRefused": (
        "The server refused the From address. Most hosts only let a mailbox "
        "send as itself, so SMTP_SENDER should match the SMTP_LOGIN username."
    ),
    "SMTPRecipientsRefused": "The server refused the recipient address.",
    "SMTPServerDisconnected": (
        "The server hung up. Usually the wrong TLS mode for the port: 465 is "
        "SMTP_SSL = True, 587 is SMTP_SSL = False with SMTP_TLS = True."
    ),
    "SMTPNotSupportedError": (
        "The server does not offer what was asked of it -- typically STARTTLS "
        "on a port that expects implicit SSL. See the note on 465 vs 587."
    ),
    "SSLError": (
        "TLS handshake failed. Speaking SSL to a STARTTLS port looks like this; "
        "so does 465 with SMTP_SSL = False."
    ),
    "timeout": (
        "No answer within SMTP_TIMEOUT. The port is blocked, or the host is "
        "wrong. Try: nc -vz <host> <port>"
    ),
    "TimeoutError": (
        "No answer within SMTP_TIMEOUT. The port is blocked, or the host is "
        "wrong. Try: nc -vz <host> <port>"
    ),
    "ConnectionRefusedError": "Nothing is listening on that host and port.",
    "gaierror": "The hostname does not resolve.",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipient", help="address to send the test message to")
    parser.add_argument(
        "--subject",
        default="new fire: SMTP test",
        help="subject line (default: %(default)s)",
    )
    args = parser.parse_args()

    settings = load_app_module("settings")
    mailer_module = load_app_module("mailer")

    for key, value in describe(settings):
        print("%-13s %s" % (key, value))
    print()

    try:
        mailer = mailer_module.build_mailer(settings)
    except RuntimeError as error:
        # The same check that would stop the app at import. Better to read it
        # here than to find out by watching every route return 404.
        print("Configuration incomplete: %s" % error, file=sys.stderr)
        return 2

    if mailer is None:
        print(
            "SMTP_SERVER is not set, so the app sends no mail at all and auth "
            "prints reset messages to stdout instead. Set SMTP_SERVER, "
            "SMTP_SENDER and SMTP_LOGIN in settings_private.py.",
            file=sys.stderr,
        )
        return 2

    body = (
        "This is a test message from new fire.\n\n"
        "If you are reading it in a mailbox, password reset mail works: the "
        "server accepted a message from %s and delivered it here.\n"
        % settings.SMTP_SENDER
    )

    print("Sending to %s ..." % args.recipient)
    try:
        mailer.send(to=args.recipient, subject=args.subject, body=body)
    except Exception as error:  # noqa: BLE001 -- the point is to report anything
        name = type(error).__name__
        print("FAILED: %s: %s" % (name, error), file=sys.stderr)
        hint = HINTS.get(name)
        if hint:
            print(hint, file=sys.stderr)
        return 1

    if mailer_module.is_logging_server(settings.SMTP_SERVER):
        # Nothing was sent; the message above is the whole result. Say so rather
        # than let a green "accepted" be read as mail having gone out.
        print(
            "\nLogged, not sent: SMTP_SERVER is %r. The message appears in the "
            "log above rather than in a mailbox." % settings.SMTP_SERVER
        )
        return 0

    print(
        "\nAccepted by the server. Check %s -- including its spam folder, which "
        "is where mail from a domain that has never sent any tends to land."
        % args.recipient
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
