"""
This module sets up core fixtures and utilities for a py4web application, including:

- Logging: Configures a custom logger using application settings.
- Database: Connects to the database using settings from the configuration.
- MusicBrainz: Opens the cache and builds the configured source; see build_mb_runtime.
- Caching: Instantiates a cache object for use throughout the app.
- Translation: Sets up the translation object for internationalization.
- Session Management: Selects and configures the session backend (cookies or database) based on settings.
- Authentication: Initializes the Auth object, configures its parameters, and defines authentication tables and actions.
- Email: Configures the email sender for authentication-related emails if SMTP settings are provided.
- Scheduler: Defines (but does not start) the background scheduler; see tasks.py.
- Decorators: Provides convenience action factories for authenticated and unauthenticated routes.
"""

from py4web import DAL, Cache, Field, Session, Translator
from py4web.server_adapters.logging_utils import make_logger
from py4web.utils.auth import Auth
from py4web.utils.factories import ActionFactory
from py4web.utils.mailer import Mailer

from . import settings
from .musicbrainz import apply_sqlite_pragmas, connect_cache
from .musicbrainz.factory import POSTGRES, WEBSERVICE, build_source, build_user_agent
from .scheduling import ResilientScheduler

# #######################################################
# implement custom loggers form settings.LOGGERS
# #######################################################
logger = make_logger("py4web:" + settings.APP_NAME, settings.LOGGERS)

# #######################################################
# connect to db
# #######################################################
db = DAL(
    settings.DB_URI,
    folder=settings.DB_FOLDER,
    pool_size=settings.DB_POOL_SIZE,
    migrate=settings.DB_MIGRATE,
    fake_migrate=settings.DB_FAKE_MIGRATE,
    # WAL matters here as much as it does for the cache: the scheduler polls
    # task_run continuously while web threads enqueue and forked children report
    # status. Without it that contention throws "database is locked", which
    # kills pydal's scheduler thread outright and stops background syncing.
    after_connection=(
        apply_sqlite_pragmas if settings.DB_URI.startswith("sqlite") else None
    ),
)

# #######################################################
# MusicBrainz cache: local SQLite copy of the fragment of
# MusicBrainz this app uses, plus the source that fills it
# #######################################################

def build_mb_runtime(migrate=False, pool_size=None):
    """
    Open a cache connection and its matching source.

    Called once at import for the web process, and again inside each scheduler
    task. The scheduler runs tasks in a forked child, and pydal keeps its
    connections in thread-local storage which the fork copies wholesale — so a
    child reusing the parent's handles would share sockets with it.

    Building a new DAL is *not* enough to avoid that: pydal pools connections
    globally per URI, so a fresh DAL in a child can be handed a connection the
    parent opened. Two processes then talk over one socket and queries start
    returning nonsense — a cursor with no description, in the failure that
    prompted this. Forked callers therefore pass pool_size=0, which bypasses the
    pool on connect and really closes on exit.

    Opening a genuinely new Postgres connection in a forked child has its own
    hazard on macOS; MB_DB_DRIVER_ARGS deals with that one.

    Returns (cache, source, mirror); mirror is None unless Postgres is the
    configured source.
    """
    mirror_pool = pool_size if pool_size is not None else settings.MB_DB_POOL_SIZE
    cache_pool = pool_size if pool_size is not None else settings.MBCACHE_DB_POOL_SIZE
    # The mirror is only connected when it is the configured source. A
    # deployment running against the web service has no Postgres alongside it,
    # and connecting unconditionally would make the app unstartable there.
    mirror = None
    if settings.MB_SOURCE == POSTGRES:
        mirror = DAL(
            settings.MB_DB_URI,
            folder=settings.DB_FOLDER,
            pool_size=mirror_pool,
            migrate=False,  # CRITICAL: never let DAL alter the MusicBrainz schema
            fake_migrate=False,
            # See MB_DB_DRIVER_ARGS: without gssencmode=disable, opening this
            # connection inside a forked scheduler task segfaults on macOS.
            driver_args=dict(settings.MB_DB_DRIVER_ARGS or {}),
        )

    cache = connect_cache(
        settings.MBCACHE_DB_URI,
        folder=settings.DB_FOLDER,
        migrate=migrate,
        pool_size=cache_pool,
    )

    source = build_source(
        settings.MB_SOURCE,
        mb_dal=mirror,
        user_agent=(
            build_user_agent(
                settings.MB_USER_AGENT_NAME,
                settings.MB_USER_AGENT_VERSION,
                settings.MB_USER_AGENT_CONTACT,
            )
            if settings.MB_SOURCE == WEBSERVICE
            else None
        ),
        rate_limit_db=settings.MB_RATE_LIMIT_DB,
        min_interval=settings.MB_RATE_LIMIT_INTERVAL,
        logger=logger,
    )
    return cache, source, mirror


# The web process owns one set; migrations run here and only here.
mb_cache, mb_source, mb = build_mb_runtime(migrate=settings.MBCACHE_MIGRATE)

# Extra fixtures the MusicBrainz pages need. The mirror connection is only one
# of them some of the time, so actions splat this rather than naming `mb`.
mb_fixtures = tuple(fixture for fixture in (mb,) if fixture is not None)

# #######################################################
# define global objects that may or may not be used by the actions
# #######################################################
cache = Cache(size=1000)
T = Translator(settings.T_FOLDER)

# #######################################################
# pick the session type that suits you best
# #######################################################
if settings.SESSION_TYPE == "cookies":
    session = Session(secret=settings.SESSION_SECRET_KEY)

elif settings.SESSION_TYPE == "database":
    from py4web.utils.dbstore import DBStore

    session = Session(secret=settings.SESSION_SECRET_KEY, storage=DBStore(db))

# #######################################################
# Instantiate the object and actions that handle auth
# #######################################################
auth = Auth(session, db, define_tables=False)
auth.use_username = True
auth.param.registration_requires_confirmation = settings.VERIFY_EMAIL
auth.param.registration_requires_approval = settings.REQUIRES_APPROVAL
auth.param.login_after_registration = settings.LOGIN_AFTER_REGISTRATION
auth.param.allowed_actions = settings.ALLOWED_ACTIONS
auth.param.login_expiration_time = 3600
auth.param.password_complexity = {"entropy": settings.PASSWORD_ENTROPY}
auth.param.block_previous_password_num = 3
auth.define_tables()
auth.fix_actions()
auth.logger = logger

flash = auth.flash

# #######################################################
# Configure email sender for auth
# #######################################################
if settings.SMTP_SERVER:
    auth.sender = Mailer(
        server=settings.SMTP_SERVER,
        sender=settings.SMTP_SENDER,
        login=settings.SMTP_LOGIN,
        tls=settings.SMTP_TLS,
        ssl=settings.SMTP_SSL,
    )

# #######################################################
# Define the scheduler
#
# Deliberately not started here. step() marks any run whose task name it does
# not recognise as "unknown" and discards it, so a loop polling before tasks.py
# has registered its handlers would quietly throw away queued work. tasks.py
# registers first and starts it afterwards.
# #######################################################
if settings.USE_SCHEDULER:
    scheduler = ResilientScheduler(
        db, logger=logger, max_concurrent_runs=settings.SCHEDULER_MAX_CONCURRENT_RUNS
    )
else:
    scheduler = None

# #######################################################
# Enable authentication
# #######################################################
auth.enable(uses=(session, T, db), env=dict(T=T))

# #######################################################
# Define convenience decorators
# They can be used instead of @action and @action.uses
# They should NEVER BE MIXED with @action and @action.uses
# If you need to provide extra fixtures for a specific controller
# add them like this: @authenticated(uses=[extra_fixture])
# #######################################################
unauthenticated = ActionFactory(db, session, T, flash, auth)
authenticated = ActionFactory(db, session, T, flash, auth.user)
