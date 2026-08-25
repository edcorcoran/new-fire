"""
App-level settings: databases, the MusicBrainz cache and its sources, auth,
sessions, email and the scheduler.

Anything machine-specific or secret belongs in settings_private.py, which is
gitignored and overrides whatever it defines; see the import at the bottom.
"""

import os

from py4web.core import required_folder

# mode (default or development)
MODE = os.environ.get("PY4WEB_MODE")

# db settings
APP_FOLDER = os.path.dirname(__file__)
APP_NAME = os.path.split(APP_FOLDER)[-1]

# DB_FOLDER:    Sets the place where migration files will be created
#               and is the store location for SQLite databases
DB_FOLDER = required_folder(APP_FOLDER, "databases")
DB_URI = "sqlite://storage.db"
# No pooling. The scheduler forks a child per task, and pydal pools connections
# globally per URI: with a pool, the child's reconnect() closes the inherited
# connection into the pool and immediately takes the same one back, so parent
# and child end up sharing one SQLite handle. Pooling buys almost nothing for
# SQLite anyway, since connecting to a local file is cheap.
DB_POOL_SIZE = 0
DB_MIGRATE = True
DB_FAKE_MIGRATE = False

# MusicBrainz cache: a local SQLite copy of just the labels and releases this
# app actually touches. Separate file from storage.db so it can be rebuilt,
# deleted or shipped prewarmed without disturbing user data.
MBCACHE_DB_URI = "sqlite://mbcache.db"
MBCACHE_DB_POOL_SIZE = 1
MBCACHE_MIGRATE = True

# The local MusicBrainz Postgres mirror, read only when MB_SOURCE is "postgres".
# Both are placeholders: a mirror is a machine-specific thing, so a real one
# belongs in settings_private.py. They are declared here rather than only there
# because common.py reads them whenever MB_SOURCE is "postgres", and a settings
# module that references names it does not define fails a fresh checkout with an
# AttributeError instead of a connection error that says what is wrong.
MB_DB_URI = "postgres://musicbrainz:musicbrainz@host:5432/musicbrainz_db"
MB_DB_POOL_SIZE = 3

# Extra psycopg2 arguments for the mirror connection.
#
# gssencmode=disable is load-bearing, not tidying. The scheduler runs each task
# in a forked child, and libpq probes for GSSAPI credentials when opening a
# connection — a path that goes through Kerberos into CoreFoundation, which is
# not fork-safe on macOS. Connecting to Postgres from a forked child segfaults
# without this: measured 3/3 crashes with SIGSEGV by default, 3/3 clean with it.
# Harmless on Linux, and this app authenticates with a password, never Kerberos.
MB_DB_DRIVER_ARGS = {"gssencmode": "disable"}

# Which source fills the cache: "webservice" (authoritative, ~1 req/sec) or
# "postgres" (a local mirror, no rate limit).
#
# The web service is the default because it is the one that works anywhere. A
# mirror is a machine you have to have and MB_DB_URI above cannot be guessed,
# so choosing it is a local decision and belongs in settings_private.py next to
# the URI it needs. Getting this wrong is also self-announcing rather than
# quiet: an unconfigured checkout stops with "MB_USER_AGENT_CONTACT must be set
# to a real contact address", which is the next thing to do anyway.
MB_SOURCE = "webservice"

# Contact address embedded in the MusicBrainz User-Agent. The web service blocks
# generic user agents, so this must be real before MB_SOURCE = "webservice".
MB_USER_AGENT_CONTACT = None
MB_USER_AGENT_VERSION = "0.1"

# Application name in the User-Agent, deliberately not APP_NAME. APP_NAME is the
# directory the app was loaded from, which is "_default" whenever it is mounted
# at "/" through the symlink -- and "_default/0.1" identifies nothing, which is
# the one thing MusicBrainz asks a User-Agent to do. This is the name of the
# application, not of the folder it happens to live in.
MB_USER_AGENT_NAME = "newfire"

# Minimum seconds between web service requests, shared across every process
# (web workers and forked scheduler tasks alike). MusicBrainz asks for an
# average of one per second and enforces it by blocking clients that ignore it,
# so raising this is safe and lowering it is not.
MB_RATE_LIMIT_INTERVAL = 1.0

# Where the shared rate-limiter marker lives. Its own file rather than a table
# in the cache, so claiming a request slot never waits on a cache write.
MB_RATE_LIMIT_DB = os.path.join(DB_FOLDER, "mbratelimit.db")

# How long a fully-synced label stays fresh. Past this the page still serves
# from the cache immediately and refreshes it in the background, so staleness
# costs nobody a wait.
MB_CACHE_TTL_DAYS = 7

# Seconds before the scheduler kills a stuck label sync. Generous because a
# large label over the web service is genuinely slow: ~1,000 releases is 11
# requests at a rate-limited second apiece, plus retries.
MB_SYNC_TIMEOUT = 900

# How often followed labels are checked for new releases. Each check is one
# request per label, so this is cheap even hourly; nightly is plenty given
# MusicBrainz itself is edited by hand.
MB_REFRESH_PERIOD = 86400

# Most new releases a label can gain before the nightly sweep stops fetching
# them one by one and just re-reads the whole label instead.
MB_INCREMENTAL_MAX_NEW = 25

# How long a search result is trusted. Repeat searches inside this window cost
# nothing; past it MusicBrainz is asked again.
MB_SEARCH_TTL_DAYS = 7
# Label types hidden from search: companies rather than imprints. None means use
# the package default (Holding, Publisher, Rights Society, Creative Agency,
# Manufacturer); set a collection here to override, or an empty one to show
# everything MusicBrainz returns.
MB_HIDE_LABEL_TYPES = None
# Cache tidying: how often the cleanup sweep runs, and how long a row that looks
# unreachable is left alone anyway. The grace period is slack against a sync
# writing rows the sweep would otherwise judge orphaned.
MB_CLEANUP_PERIOD = 604800  # weekly
MB_CLEANUP_GRACE_DAYS = 1

# How often a followed label gets a full resync regardless of its release count.
# The count check cannot see a release being retitled, a streaming link being
# added, or one release replacing another, so every label is swept properly
# eventually.
MB_FULL_RESYNC_DAYS = 30

# send verification email on registration
VERIFY_EMAIL = MODE != "development"

# complexity of the password 0: no constraints, 50: safe!
PASSWORD_ENTROPY = 0 if MODE == "development" else 50

# account requires to be approved ?
REQUIRES_APPROVAL = False

# auto login after registration
# requires False VERIFY_EMAIL & REQUIRES_APPROVAL
LOGIN_AFTER_REGISTRATION = False

# ALLOWED_ACTIONS in API / default Forms:
# ["all"]
# ["login", "logout", "request_reset_password", "reset_password", \
#  "change_password", "change_email", "profile", "config", "register",
#  "verify_email", "unsubscribe"]
# Note: if you add "login", add also "logout"
ALLOWED_ACTIONS = ["all"]

# email settings
SMTP_SSL = False
SMTP_SERVER = None
SMTP_SENDER = "you@example.com"
SMTP_LOGIN = "username:password"
SMTP_TLS = False

# session settings: "cookies" (signed with the generated apps/.service secret)
# or "database" (server-side, stored in db)
SESSION_TYPE = "cookies"
SESSION_SECRET_KEY = None  # or replace with your own secret

# logger settings
LOGGERS = [
    "warning:stdout"
]  # syntax "severity:filename:format" filename can be stderr or stdout

# i18n settings
T_FOLDER = required_folder(APP_FOLDER, "translations")

# Scheduler settings. Enabled because label syncs run as background tasks;
# see tasks.py. One concurrent run keeps the MusicBrainz request rate
# predictable on top of the shared limiter.
USE_SCHEDULER = True
SCHEDULER_MAX_CONCURRENT_RUNS = 1

# Whether this process runs the scheduler loop, as distinct from USE_SCHEDULER,
# which only says the app has background work at all. Every process still builds
# a scheduler object and can enqueue through it; this decides which one actually
# executes the queue.
#
# They are separate because a deployment behind an application server does not
# get to choose how many web processes exist or how long they live. Passenger
# starts them on demand and reaps them when idle, so a scheduler started at
# import would mean several loops competing at busy times and none overnight --
# exactly inverted from what the nightly sweep needs. Instead the web processes
# only enqueue, and one long-lived worker started from cron does the work; see
# worker.py and DEPLOY.md.
#
# Defaults on, so `py4web run apps` in development behaves as it always has:
# one process that both serves pages and runs its own queue.
RUN_SCHEDULER = os.environ.get("NEWFIRE_RUN_SCHEDULER", "1") == "1"

# try import private settings
try:
    from .settings_private import *  # type: ignore[reportMissingImports]
except (ImportError, ModuleNotFoundError):
    pass
