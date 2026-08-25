"""
Builds the configured MusicBrainz source.

Kept apart from sources.py so the mirror implementation doesn't have to import
the web service one (and vice versa), and so callers pick a source with plain
configuration rather than by importing a specific class.
"""

from .ratelimit import RateLimiter
from .sources import PostgresMirrorSource
from .webservice import WebServiceSource

POSTGRES = "postgres"
WEBSERVICE = "webservice"


def build_user_agent(app_name, version, contact):
    """
    Assemble the User-Agent MusicBrainz requires.

    The contact address is not decorative: requests without a real one get
    blocked, so this refuses to build a placeholder.
    """
    if not contact:
        raise ValueError(
            "MB_USER_AGENT_CONTACT must be set to a real contact address "
            "before using the MusicBrainz web service"
        )
    return f"{app_name}/{version} ( {contact} )"


def build_source(
    kind,
    mb_dal=None,
    user_agent=None,
    rate_limit_db=None,
    min_interval=1.0,
    logger=None,
):
    """
    Return the MBSource named by `kind`.

    POSTGRES needs a DAL connected to a local mirror; WEBSERVICE needs a
    User-Agent and somewhere to keep the shared rate-limiter state.
    """
    if kind == POSTGRES:
        if mb_dal is None:
            raise ValueError("the postgres source needs a mirror connection")
        return PostgresMirrorSource(mb_dal)

    if kind == WEBSERVICE:
        if not rate_limit_db:
            raise ValueError("the web service source needs a rate limiter database")
        return WebServiceSource(
            user_agent=user_agent,
            limiter=RateLimiter(rate_limit_db, min_interval=min_interval),
            logger=logger,
        )

    raise ValueError(
        f"unknown MB_SOURCE {kind!r}; expected {POSTGRES!r} or {WEBSERVICE!r}"
    )
