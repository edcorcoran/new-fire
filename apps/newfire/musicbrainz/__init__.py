"""
MusicBrainz access layer.

Two halves that meet at a normalized dict:

  sources.py  - where records come from (Postgres mirror now, web service later)
  cache.py    - the local SQLite copy, plus writer.py which fills it

Nothing outside this package should talk to MusicBrainz directly.

See docs/musicbrainz-cache-plan.md.
"""

from .cache import (
    SYNC_COMPLETE,
    SYNC_ERROR,
    SYNC_NEVER,
    SYNC_PARTIAL,
    apply_sqlite_pragmas,
    connect_cache,
    define_cache_tables,
)
from .normalize import (
    classify_url,
    flatten_artist_credit,
    format_catalog_numbers,
    format_partial_date,
    make_label,
    make_release,
    make_url,
)
from .factory import POSTGRES, WEBSERVICE, build_source, build_user_agent
from .ratelimit import RateLimiter
from .reader import (
    count_recent_releases,
    count_release_groups,
    count_releases,
    get_label,
    get_recent_releases,
    get_releases,
    is_complete,
)
from .service import (
    ensure_label_cached,
    ensure_label_record,
    fill_label_cache,
    is_stale,
    normalize_query,
    search_labels,
)
from .sources import MBSource, PostgresMirrorSource
from .webservice import MusicBrainzError, WebServiceSource
from .writer import (
    count_cached_releases,
    get_sync_state,
    sync_label,
    sync_label_incremental,
    upsert_label,
    upsert_releases,
)

__all__ = [
    "MBSource",
    "MusicBrainzError",
    "POSTGRES",
    "PostgresMirrorSource",
    "RateLimiter",
    "SYNC_COMPLETE",
    "SYNC_ERROR",
    "SYNC_NEVER",
    "SYNC_PARTIAL",
    "WEBSERVICE",
    "WebServiceSource",
    "build_source",
    "build_user_agent",
    "apply_sqlite_pragmas",
    "classify_url",
    "connect_cache",
    "count_cached_releases",
    "count_recent_releases",
    "count_release_groups",
    "count_releases",
    "define_cache_tables",
    "ensure_label_cached",
    "ensure_label_record",
    "fill_label_cache",
    "flatten_artist_credit",
    "format_catalog_numbers",
    "format_partial_date",
    "get_label",
    "get_recent_releases",
    "get_releases",
    "get_sync_state",
    "is_complete",
    "is_stale",
    "make_label",
    "make_release",
    "make_url",
    "normalize_query",
    "search_labels",
    "sync_label",
    "sync_label_incremental",
    "upsert_label",
    "upsert_releases",
]
