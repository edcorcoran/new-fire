"""
Builds the Mailer that auth sends through, and decides what counts as a complete
SMTP configuration.

Its own module rather than a few lines in common.py so the configuration can be
checked without booting the app: importing the newfire package pulls in
controllers, models and the scheduler, which is a great deal of machinery to
stand up only to find out whether a password is right.
scripts/send_test_email.py loads this module by itself and gets the same Mailer
the running app would have built.
"""

import email.charset
import email.encoders
import email.header

from py4web.utils import mailer as py4web_mailer
from py4web.utils.mailer import Mailer

# Names py4web's own mailer module uses but, as of 1.20260805.0, no longer
# imports. They arrived through `from pydal._compat import *`, which that
# release dropped -- it had to, because pydal 3 removed _compat entirely -- and
# nothing replaced them. Their values here are the stdlib originals that
# _compat was itself re-exporting, so restoring them puts back exactly what was
# there rather than substituting anything.
COMPAT_NAMES = {
    "add_charset": email.charset.add_charset,
    "charset_QP": email.charset.QP,
    "Header": email.header.Header,
    "Encoders": email.encoders,
}


def restore_compat_names():
    """Repair py4web's mailer module in place; return the names restored.

    Worth doing rather than pinning around, because of where the breakage sits:
    add_charset is called on the opening line of Mailer.send(), so on an
    affected release *every* send raises NameError before a socket is opened --
    the "logging" pseudo-server included. Nothing catches it, so it surfaces as
    a 500 on the password reset form, and only once a real person has needed
    one. Meanwhile the release installs and the app runs perfectly.

    Guarded by hasattr, so this is a no-op on releases that still define them,
    and the whole function can be deleted once every machine that matters is
    past the fix.
    """
    restored = []
    for name, value in COMPAT_NAMES.items():
        if not hasattr(py4web_mailer, name):
            setattr(py4web_mailer, name, value)
            restored.append(name)
    return restored


PATCHED_COMPAT_NAMES = restore_compat_names()


def is_logging_server(server):
    """True for the pseudo-servers that write the message instead of sending it.

    py4web accepts both the bare word and "logging:/path/to/file". Neither opens
    a socket, so neither needs credentials -- which is the whole reason
    development can run with email switched on and no mailbox password on the
    machine.
    """
    return server == "logging" or server.startswith("logging:")


# Fixed width on purpose. A mask as long as what it hides ("*" * len(password))
# reports the password's length to anyone reading the log, which is a real help
# to someone guessing it and no help at all to the person fixing the setting.
MASK = "********"


def redact_login(login):
    """SMTP_LOGIN with the password removed, safe to put in an error or a log.

    The username half stays, because it is the half worth seeing: shortening it
    to the part before the @ is the most common way this setting is wrong, and
    the mailbox address is not a secret. Everything after the first colon is
    the password and goes behind the mask.

    A value with no colon is masked whole. That case looks like the least
    sensitive one and is the most: "username:password" with no colon in it is
    usually a bare password pasted into the wrong shape, so the part that would
    normally be safe to show is precisely the part that might be the secret.
    """
    if not login:
        return login
    if ":" not in login:
        return MASK
    username, _password = login.split(":", 1)
    return "%s:%s" % (username, MASK)


def build_mailer(settings, logger=None):
    """Return a Mailer for these settings, or None when email is switched off.

    Raises RuntimeError on a half-written configuration. Raising here means
    failing at import, which is loud in the way this app already fails on a
    missing MB_USER_AGENT_CONTACT: py4web logs the traceback and serves 404 for
    every path, so the problem is found immediately and by whoever caused it.
    The alternative is strictly worse. py4web's Mailer re-raises whatever the
    SMTP conversation threw, and auth does not catch it, so a Mailer built from
    half a configuration fails inside the password reset action instead -- a 500
    in front of the one person who by definition cannot log in to report it.
    """
    server = settings.SMTP_SERVER
    if not server:
        return None

    if not settings.SMTP_SENDER:
        raise RuntimeError(
            "SMTP_SENDER must be set when SMTP_SERVER is: it is the From address "
            "on password reset mail. Set it in settings_private.py."
        )

    login = settings.SMTP_LOGIN
    if not is_logging_server(server):
        if not login:
            raise RuntimeError(
                "SMTP_LOGIN must be set when SMTP_SERVER is %r. Set it in "
                "settings_private.py, or use SMTP_SERVER = 'logging' to write "
                "mail to the log instead of sending it." % server
            )
        if ":" not in login:
            raise RuntimeError(
                'SMTP_LOGIN must be "username:password"; %r has no colon. On '
                "most hosts the username is the full mailbox address."
                % redact_login(login)
            )

    mailer = Mailer(
        server=server,
        sender=settings.SMTP_SENDER,
        login=login,
        tls=settings.SMTP_TLS,
        ssl=settings.SMTP_SSL,
    )
    # Mailer hardcodes 5 seconds, which is under the round trip to a remote
    # mailbox having a slow day.
    mailer.settings.timeout = settings.SMTP_TIMEOUT
    if logger is not None:
        # Mailer otherwise logs through the root logger, so a failure -- and in
        # "logging" mode the entire message -- lands wherever logging's
        # last-resort handler points rather than where LOGGERS says.
        mailer.settings.logger = logger
        if PATCHED_COMPAT_NAMES:
            # Said once per process, so that the day this stops appearing is
            # the day restore_compat_names can go.
            logger.warning(
                "py4web's mailer is missing %s; restored from the stdlib. See "
                "restore_compat_names in mailer.py.",
                ", ".join(PATCHED_COMPAT_NAMES),
            )
    return mailer
