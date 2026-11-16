#!/usr/bin/env python3
"""
Tell someone when the site stops answering.

In November 2026 DreamHost upgraded this VPS from Ubuntu 22.04 to 26.04, which
replaced Python 3.10 with 3.14. The virtualenv's interpreter symlink followed,
the new interpreter could not see the old site-packages, and serve.py died on
`No module named 'py4web'` at every start. Cron dutifully restarted it once a
minute for seven and a half days. The domain answered 503 the whole time and
nothing said so -- the outage was found when a friend mentioned it. See
"When DreamHost upgrades the OS" in DEPLOY.md.

Cron is a supervisor with no opinion about whether its child stays up, which is
why that could happen. This closes the gap: ask the site the same question a
visitor asks, and mail a human when the answer stops being 200.

**It must not depend on the thing it is watching.** The failure above broke
every import in the virtualenv, so a watchdog living in that virtualenv would
have been just as dead as the app, and the mail nobody got would have been
nobody's surprise. So: the system interpreter, the standard library, and
nothing else. No py4web, no pydal, no virtualenv, no third-party package. The
app's mail settings are read out of settings_private.py as data rather than
imported through the app, for the same reason -- and because the recipient
address does not belong in a public repository.

    # on the VPS, by hand
    /usr/bin/python3 ~/newfire/deploy/watchdog.py            # check once, quietly
    /usr/bin/python3 ~/newfire/deploy/watchdog.py --status    # what it thinks now
    /usr/bin/python3 ~/newfire/deploy/watchdog.py --test-mail # prove the mail path

    # in cron, every five minutes
    */5 * * * * /usr/bin/python3 /home/newfire_admin/newfire/deploy/watchdog.py

Exit status is 0 when the site answered, 1 when it did not, 2 when this script
is misconfigured and could not look. Cron mails you the output of a failing
command, which is the one mail you do not want from a thing that detects
outages -- so a confirmed outage still exits 1, but prints nothing. The log at
~/newfire-logs/watchdog.log is the record.
"""

import argparse
import datetime
import glob
import importlib.util
import json
import os
import re
import smtplib
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

# What a visitor sees, which is the only definition of "up" that matters. A
# 200 is not quite enough on its own: py4web answers 200 with an error page of
# its own making for some failures, and DreamHost's proxy has its own idea of a
# friendly placeholder. So the body must also contain the marker -- the site's
# title, which is in the layout every page extends.
DEFAULT_URL = "https://newfire.music/"
DEFAULT_MARKER = "<title>new fire</title>"

# Seconds to wait for the whole exchange. Generous: this is the first request
# after a restart sometimes, and py4web's first response can be slow while the
# app imports. Too mean a timeout invents outages.
DEFAULT_TIMEOUT = 25

# A single failure is not an outage. Cron restarts serve.py every minute and a
# check landing in that window would otherwise mail about nothing. Ask twice,
# with a pause long enough for a legitimate restart to finish.
RETRIES = 2
RETRY_PAUSE = 30

# How long before a site that is still down says so again. The point is not to
# nag but to not be forgotten: one mail in a week is how a week of downtime
# happens. Overridable with WATCHDOG_REMINDER_HOURS.
DEFAULT_REMINDER_HOURS = 24

# Identify the checker, not the person running it, since this goes to a server
# log someone else reads.
USER_AGENT = "newfire-watchdog/1 (+https://newfire.music/)"

CHECKOUT = os.environ.get(
    "NEWFIRE_CHECKOUT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
VENV = os.environ.get("NEWFIRE_VENV", os.path.expanduser("~/newfire-venv"))
LOG_DIR = os.environ.get("NEWFIRE_LOG_DIR", os.path.expanduser("~/newfire-logs"))
SERVER_LOG = os.path.join(LOG_DIR, "newfire.log")
STATE_PATH = os.path.join(LOG_DIR, "watchdog.state")
WATCHDOG_LOG = os.path.join(LOG_DIR, "watchdog.log")
PRIVATE_SETTINGS = os.path.join(CHECKOUT, "apps", "newfire", "settings_private.py")

# The app's own port, behind the proxy. Probed only to tell two outages apart:
# the app being dead and the proxy in front of it being dead look identical
# from outside and want different fixes.
LOCAL_PORT = int(os.environ.get("NEWFIRE_PORT", "8123"))


def utcnow():
    """Naive UTC. Matches what the rest of this app stores, and sorts."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- settings


class ConfigError(Exception):
    """Something this script needs is missing or malformed."""


def load_private_settings(path=PRIVATE_SETTINGS):
    """Read settings_private.py as data, without importing the app.

    That file is a flat list of assignments with no imports, so loading it
    directly is safe -- unlike settings.py, which ends in a relative
    `from .settings_private import *` and needs a parent package to resolve
    (scripts/send_test_email.py documents that trap at length). Nothing here
    needs the public defaults: every value this script reads is one that only
    exists once someone has configured mail.
    """
    if not os.path.exists(path):
        raise ConfigError(
            "no %s, so there are no mail settings to send an alert with" % path
        )
    spec = importlib.util.spec_from_file_location("newfire_private", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 -- report whatever it was
        raise ConfigError("could not read %s: %s" % (path, error))
    return module


class Config:
    """Where to look, and who to tell."""

    def __init__(self, settings):
        self.url = getattr(settings, "WATCHDOG_URL", DEFAULT_URL)
        self.marker = getattr(settings, "WATCHDOG_MARKER", DEFAULT_MARKER)
        self.reminder = datetime.timedelta(
            hours=float(
                getattr(settings, "WATCHDOG_REMINDER_HOURS", DEFAULT_REMINDER_HOURS)
            )
        )
        # The recipient lives in settings_private.py because this repository is
        # public and a personal address in it would be scraped within the week.
        self.recipient = getattr(settings, "WATCHDOG_RECIPIENT", None)
        if not self.recipient:
            raise ConfigError(
                "WATCHDOG_RECIPIENT is not set in settings_private.py, so there "
                "is nobody to mail. Add: WATCHDOG_RECIPIENT = 'you@example.com'"
            )

        # Mail goes out through the same mailbox the app's password resets use.
        # Reusing it rather than adding a second credential: one mailbox to
        # keep working, and it is already proved by scripts/send_test_email.py.
        self.smtp_server = getattr(settings, "SMTP_SERVER", None)
        self.smtp_sender = getattr(settings, "SMTP_SENDER", None)
        self.smtp_login = getattr(settings, "SMTP_LOGIN", None)
        self.smtp_ssl = bool(getattr(settings, "SMTP_SSL", True))
        self.smtp_timeout = int(getattr(settings, "SMTP_TIMEOUT", 30))
        for name in ("smtp_server", "smtp_sender", "smtp_login"):
            if not getattr(self, name):
                raise ConfigError(
                    "%s is not set in settings_private.py, so no alert could be "
                    "sent. See the SMTP section of DEPLOY.md."
                    % name.upper()
                )
        if ":" not in self.smtp_login:
            raise ConfigError(
                'SMTP_LOGIN must be "username:password" and has no colon in it'
            )


# ---------------------------------------------------------------- the check


class Result:
    """What one look at the site found."""

    def __init__(self, ok, summary):
        self.ok = ok
        self.summary = summary

    def __repr__(self):
        return "Result(ok=%r, summary=%r)" % (self.ok, self.summary)


def look_once(url, marker, timeout=DEFAULT_TIMEOUT):
    """One request. Returns a Result, never raises.

    Every failure mode is reported as text rather than an exception class,
    because this string goes into a mail someone reads half-awake: "HTTP 503"
    and "certificate expired" are both answers to "what is wrong".
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(65536).decode("utf-8", "replace")
            code = response.getcode()
            if code != 200:
                return Result(False, "HTTP %s" % code)
            if marker and marker not in body:
                return Result(
                    False,
                    "HTTP 200 but the page did not contain %r -- something is "
                    "answering, but it is not this app" % marker,
                )
            return Result(True, "HTTP 200")
    except urllib.error.HTTPError as error:
        return Result(False, "HTTP %s" % error.code)
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return Result(False, "TLS certificate did not verify: %s" % reason)
        if isinstance(reason, socket.timeout):
            return Result(False, "no answer within %ss" % timeout)
        if isinstance(reason, socket.gaierror):
            return Result(False, "the hostname did not resolve: %s" % reason)
        return Result(False, "could not connect: %s" % reason)
    except socket.timeout:
        return Result(False, "no answer within %ss" % timeout)
    except Exception as error:  # noqa: BLE001 -- a checker must not crash
        return Result(False, "%s: %s" % (type(error).__name__, error))


def look(url, marker, timeout=DEFAULT_TIMEOUT, retries=RETRIES, pause=RETRY_PAUSE):
    """Ask until one answer is good, or the attempts run out.

    The last failure is the one reported: by then the pause has covered a
    restart, so whatever is still wrong is the real complaint.
    """
    result = look_once(url, marker, timeout)
    attempt = 1
    while not result.ok and attempt < retries:
        time.sleep(pause)
        result = look_once(url, marker, timeout)
        attempt += 1
    return result


# ---------------------------------------------------------------- diagnosis


def venv_mismatch(venv=VENV):
    """Name the OS-upgrade failure, or return None.

    This is the one diagnosis worth automating, because it is the one that cost
    a week. The virtualenv keeps its packages in lib/python<major>.<minor>/,
    and bin/python is a symlink to the system interpreter. An OS upgrade moves
    the interpreter to a new minor version, which then looks in a directory
    that does not exist, and every import fails -- while everything about the
    virtualenv still looks present and correct.

    Compared by where the packages actually are rather than against
    pyvenv.cfg's `version`, because pyvenv.cfg is only a record of what built
    the environment: after a rebuild it agrees with the interpreter even when
    the packages were left behind.
    """
    pythons = sorted(glob.glob(os.path.join(venv, "lib", "python3.*", "site-packages")))
    if not pythons:
        return "%s has no lib/python3.*/site-packages at all" % venv
    have = {re.search(r"python(3\.\d+)", path).group(1) for path in pythons}

    binary = os.path.join(venv, "bin", "python")
    try:
        completed = subprocess.run(
            [binary, "-c", "import sys; print('3.%d' % sys.version_info[1])"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return "%s would not run (%s)" % (binary, error)
    if completed.returncode != 0:
        return "%s exited %s: %s" % (
            binary,
            completed.returncode,
            (completed.stderr or "").strip()[:200],
        )
    running = completed.stdout.strip()
    if running not in have:
        return (
            "%s is Python %s, but the packages are in %s. The interpreter cannot "
            "see them, so every import fails. This is the OS-upgrade failure: "
            'see "When DreamHost upgrades the OS" in DEPLOY.md.'
            % (binary, running, ", ".join("python" + v for v in sorted(have)))
        )
    return None


def local_port_answers(port=LOCAL_PORT):
    """Whether anything is listening on the app's own port.

    serve.py answers 403 to a request without the proxy's headers, so a refused
    connection means the app is down while a 403 means the app is up and the
    proxy in front of it is the problem. Raw socket rather than HTTP: all this
    needs to know is whether someone accepts.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return True
    except OSError:
        return False


def processes():
    """The serve.py processes, as pgrep would show them."""
    try:
        completed = subprocess.run(
            ["pgrep", "-af", "deploy/serve.py"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return "could not run pgrep (%s)" % error
    out = (completed.stdout or "").strip()
    return out or "nothing matching deploy/serve.py is running"


def log_tail(path=SERVER_LOG, lines=25):
    """The end of the server log, where the traceback is."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            window = min(size, 20000)
            handle.seek(size - window)
            text = handle.read().decode("utf-8", "replace")
        return "\n".join(text.splitlines()[-lines:]) or "(empty)"
    except OSError as error:
        return "could not read %s (%s)" % (path, error)


def diagnose():
    """Everything worth knowing about why it is down, in one block.

    Gathered only when the site has already failed twice, so the cost does not
    matter and completeness does. The aim is a mail that can be acted on from a
    phone without first having to ssh in and go looking.
    """
    parts = []
    mismatch = venv_mismatch()
    if mismatch:
        parts.append("LIKELY CAUSE\n  %s" % mismatch)
    parts.append(
        "APP PORT\n  127.0.0.1:%s %s"
        % (
            LOCAL_PORT,
            (
                "accepted a connection, so the app is up and the problem is "
                "between DreamHost's proxy and it"
                if local_port_answers()
                else "refused the connection, so the app itself is not running"
            ),
        )
    )
    parts.append("PROCESSES\n  %s" % processes().replace("\n", "\n  "))
    parts.append("SERVER LOG (tail)\n  %s" % log_tail().replace("\n", "\n  "))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- state


def load_state(path=STATE_PATH):
    """What the last run concluded.

    A missing or corrupt file reads as "up", so the first run after installing
    this -- or after someone deletes the file -- mails if the site is down and
    stays quiet if it is not. Corrupt rather than crash: the state file is a
    convenience, and refusing to check the site because a cache is unreadable
    would be the wrong way round.
    """
    try:
        with open(path) as handle:
            state = json.load(handle)
        if not isinstance(state, dict) or state.get("status") not in ("up", "down"):
            raise ValueError("unrecognised contents")
        return state
    except (OSError, ValueError):
        return {"status": "up", "since": None, "last_alert": None}


def save_state(state, path=STATE_PATH):
    """Write the state file, atomically enough for a once-every-5-minutes job."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".new"
    with open(temporary, "w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def parse_time(value):
    """An ISO timestamp from the state file, or None."""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


def describe_duration(delta):
    """A timedelta as "3d 4h", for a subject line."""
    if delta is None:
        return "an unknown time"
    seconds = int(max(delta.total_seconds(), 0))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    for value, unit in ((days, "d"), (hours, "h"), (minutes, "m")):
        if value:
            parts = ["%s%s" % (value, unit)]
            break
    else:
        return "under a minute"
    if unit == "d" and hours:
        parts.append("%sh" % hours)
    elif unit == "h" and minutes:
        parts.append("%sm" % minutes)
    return " ".join(parts)


def decide(state, ok, now, reminder):
    """What to do about this result, given what the last run concluded.

    Separated from everything that touches the network, a mailbox or the clock
    so that the part with the policy in it can be tested directly -- which is
    where the bugs in a thing like this live: mailing on every check, mailing
    never, or forgetting to say when it came back.

    Returns (action, next_state) where action is one of "none", "down",
    "still_down" or "recovered".
    """
    since = parse_time(state.get("since"))
    last_alert = parse_time(state.get("last_alert"))

    if ok:
        if state.get("status") == "down":
            return "recovered", {"status": "up", "since": None, "last_alert": None}
        return "none", {"status": "up", "since": None, "last_alert": None}

    if state.get("status") != "down":
        # Newly down: the outage began at the first failed check, near enough.
        return "down", {
            "status": "down",
            "since": now.isoformat(),
            "last_alert": now.isoformat(),
        }

    # Still down. Say so again only when the reminder interval has passed,
    # keeping the original start time: how long it has been is the useful part.
    if last_alert is None or now - last_alert >= reminder:
        return "still_down", {
            "status": "down",
            "since": (since or now).isoformat(),
            "last_alert": now.isoformat(),
        }
    return "none", {
        "status": "down",
        "since": (since or now).isoformat(),
        "last_alert": last_alert.isoformat(),
    }


# ---------------------------------------------------------------- mail


def send(config, subject, body):
    """Send one message, verifying the server's certificate.

    The mailbox password goes out over this connection, so the context is a
    verifying one -- the same reasoning, and the same correction of smtplib's
    defaults, as install_certificate_verification in apps/newfire/mailer.py.
    That module cannot be reused here because importing it pulls in py4web,
    which is exactly what this script cannot afford to depend on.
    """
    username, password = config.smtp_login.split(":", 1)
    host, _, port = config.smtp_server.partition(":")
    port = int(port or (465 if config.smtp_ssl else 587))

    message = EmailMessage()
    message["From"] = config.smtp_sender
    message["To"] = config.recipient
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    if config.smtp_ssl:
        server = smtplib.SMTP_SSL(
            host, port, timeout=config.smtp_timeout, context=context
        )
    else:
        server = smtplib.SMTP(host, port, timeout=config.smtp_timeout)
    try:
        if not config.smtp_ssl:
            server.starttls(context=context)
        server.login(username, password)
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 -- the message is already sent
            pass


def compose(action, config, result, state, now):
    """The subject and body for one alert."""
    host = config.url.split("//", 1)[-1].rstrip("/")
    since = parse_time(state.get("since"))
    down_for = describe_duration(now - since) if since else None

    if action == "recovered":
        subject = "%s is back up (was down %s)" % (host, down_for)
        body = (
            "%s is answering again.\n\n"
            "It was down for %s, from %s UTC.\n\n"
            "Nothing here fixed it -- this only watches. If you did not restart "
            "anything, cron did it for you, which means it will probably happen "
            "again: worth reading the log.\n\n"
            "  %s\n"
            % (config.url, down_for, state.get("since"), SERVER_LOG)
        )
        return subject, body

    if action == "still_down":
        subject = "%s STILL down (%s) -- %s" % (host, down_for, result.summary)
        opening = "%s has been down for %s and is still down.\n" % (config.url, down_for)
    else:
        subject = "%s is DOWN -- %s" % (host, result.summary)
        opening = "%s stopped answering.\n" % config.url

    body = (
        "%s\n"
        "WHAT FAILED\n  %s\n  (checked %s times, %ss apart)\n\n"
        "%s\n\n"
        "WHAT TO DO\n"
        "  ssh in and read DEPLOY.md -- \"Deploying an update\" for the usual\n"
        "  checks, \"When DreamHost upgrades the OS\" if the likely cause above\n"
        "  names a Python version. Cron restarts the server every minute, so a\n"
        "  site that is still down after a minute will not fix itself.\n\n"
        "-- %s, %s UTC\n"
        % (
            opening,
            result.summary,
            RETRIES,
            RETRY_PAUSE,
            diagnose(),
            os.path.basename(__file__),
            now.replace(microsecond=0).isoformat(),
        )
    )
    return subject, body


# ---------------------------------------------------------------- plumbing


def note(message, path=WATCHDOG_LOG):
    """Append one line to the watchdog's own log.

    Only state changes and errors are written. A line per check would be a
    hundred thousand lines a year of "still fine", and nobody reads that -- the
    state file answers "what does it think right now" instead.
    """
    stamp = utcnow().replace(microsecond=0).isoformat()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as handle:
            handle.write("%s %s\n" % (stamp, message))
    except OSError:
        # A watchdog that cannot write its log should still send its mail.
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check that the site is answering, and mail someone when it is not."
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print what the last run concluded, check nothing, send nothing",
    )
    parser.add_argument(
        "--test-mail",
        action="store_true",
        help="send one message to WATCHDOG_RECIPIENT and exit, to prove the path",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="ask once instead of retrying; for looking by hand",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="say what happened on stdout (cron runs without this, on purpose)",
    )
    args = parser.parse_args(argv)

    if args.status:
        state = load_state()
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    try:
        config = Config(load_private_settings())
    except ConfigError as error:
        # Worth being loud about: a misconfigured watchdog is indistinguishable
        # from a working one until the day it matters.
        print("watchdog is not configured: %s" % error, file=sys.stderr)
        note("CONFIG ERROR %s" % error)
        return 2

    if args.test_mail:
        now = utcnow()
        subject = "%s watchdog test" % config.url.split("//", 1)[-1].rstrip("/")
        body = (
            "This is the watchdog on the VPS, proving it can reach you.\n\n"
            "A real alert looks like this, with a diagnosis of the failure in "
            "place of this paragraph:\n\n%s\n\n-- %s UTC\n"
            % (diagnose(), now.replace(microsecond=0).isoformat())
        )
        try:
            send(config, subject, body)
        except Exception as error:  # noqa: BLE001 -- the point is to report it
            print("FAILED: %s: %s" % (type(error).__name__, error), file=sys.stderr)
            print(
                "scripts/send_test_email.py explains the common causes in detail.",
                file=sys.stderr,
            )
            return 1
        print("Sent to %s. Check the inbox, and its spam folder." % config.recipient)
        return 0

    state = load_state()
    result = look(
        config.url,
        config.marker,
        retries=1 if args.once else RETRIES,
    )
    now = utcnow()
    action, next_state = decide(state, result.ok, now, config.reminder)

    if action != "none":
        subject, body = compose(action, config, result, next_state, now)
        try:
            send(config, subject, body)
            note("%s %s -- mailed %s" % (action.upper(), result.summary, config.recipient))
        except Exception as error:  # noqa: BLE001
            # The state still advances. Mailing again in five minutes would be
            # a loop, and the next reminder is the retry.
            note(
                "%s %s -- MAIL FAILED %s: %s"
                % (action.upper(), result.summary, type(error).__name__, error)
            )
            if args.verbose:
                print("alert not sent: %s: %s" % (type(error).__name__, error))

    save_state(next_state)

    if args.verbose:
        print("%s (%s)" % ("up" if result.ok else "DOWN", result.summary))
        if action != "none":
            print("action: %s" % action)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
