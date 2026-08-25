"""
The SQLite cache that stands in front of MusicBrainz.

This is a separate database file from the app's storage.db: it is disposable and
rebuildable, it can be seeded offline from the Postgres mirror and shipped to
production as a prewarmed file, and it should never share a migration history
with user data.

Everything is keyed on MBIDs. MusicBrainz's internal integer ids are assigned per
data dump, are not stable across mirror rebuilds, and are not exposed by the web
service at all, so they are deliberately absent here.

See docs/musicbrainz-cache-plan.md section 4.2.
"""

from py4web import DAL, Field

# Sync status values for mb_sync_state.status.
#
# The distinction that matters is COMPLETE vs everything else: the browse
# endpoint returns releases in an undefined order, so a partially synced label
# cannot be sorted by date without showing the wrong records at the top. Only a
# COMPLETE label may be paginated by date.
SYNC_NEVER = "never"
SYNC_PARTIAL = "partial"
SYNC_COMPLETE = "complete"
SYNC_ERROR = "error"
SYNC_STATUSES = (SYNC_NEVER, SYNC_PARTIAL, SYNC_COMPLETE, SYNC_ERROR)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_label_gid ON mb_label (gid)",
    "CREATE INDEX IF NOT EXISTS ix_label_name ON mb_label (name)",
    "CREATE INDEX IF NOT EXISTS ix_release_gid ON mb_release (gid)",
    "CREATE INDEX IF NOT EXISTS ix_release_date ON mb_release (date)",
    "CREATE INDEX IF NOT EXISTS ix_release_rg ON mb_release (release_group_gid)",
    "CREATE INDEX IF NOT EXISTS ix_rl_label ON mb_release_label (label_gid)",
    "CREATE INDEX IF NOT EXISTS ix_rl_release ON mb_release_label (release_gid)",
    # COALESCE because SQLite treats NULLs as distinct in unique indexes, which
    # would let a release with no catalog number be linked to the same label
    # again on every resync.
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_rl_pair ON mb_release_label "
    "(release_gid, label_gid, COALESCE(catalog_number, ''))",
    "CREATE INDEX IF NOT EXISTS ix_ru_release ON mb_release_url (release_gid)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_ru_url "
    "ON mb_release_url (release_gid, url)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sync_label "
    "ON mb_sync_state (label_gid)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_search_query "
    "ON mb_search_cache (entity_type, query_norm)",
)


def apply_sqlite_pragmas(adapter):
    """
    Put a SQLite database in WAL mode so writers never block readers.

    pydal calls this with the adapter (not the raw DBAPI connection) after each
    connect. Used for the cache and for the app database, which the scheduler
    polls continuously while web threads and forked task children write to it —
    without WAL and a busy timeout that contention raises "database is locked",
    and pydal's scheduler loop has no guard around it, so the thread dies and
    background syncing stops until the app restarts.
    """
    adapter.execute("PRAGMA journal_mode=WAL")
    adapter.execute("PRAGMA busy_timeout=5000")
    adapter.execute("PRAGMA synchronous=NORMAL")


def connect_cache(uri, folder, migrate=True, pool_size=1):
    """Open the cache database and define its tables."""
    cache_db = DAL(
        uri,
        folder=folder,
        pool_size=pool_size,
        migrate=migrate,
        after_connection=apply_sqlite_pragmas if uri.startswith("sqlite") else None,
    )
    define_cache_tables(cache_db, migrate=migrate)
    return cache_db


def define_cache_tables(cache_db, migrate=True):
    """
    Define the cache schema on an existing DAL.

    Split out from connect_cache so tests can point the schema at a throwaway
    in-memory database.
    """
    cache_db.define_table(
        "mb_label",
        Field("gid", unique=True),
        Field("name"),
        Field("sort_name"),
        Field("disambiguation"),
        Field("label_type"),
        Field("area_name"),
        Field("label_code", "integer"),
        Field("fetched_at", "datetime"),
        migrate=migrate,
        redefine=True,
    )

    cache_db.define_table(
        "mb_release",
        Field("gid", unique=True),
        Field("title"),
        Field("artist_credit"),
        Field("artist_gid"),
        # Partial date as text: 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Lexicographic
        # ordering on this is chronologically correct, which is why it is not
        # split into three integer columns like the MusicBrainz schema does.
        Field("date"),
        # MusicBrainz's "the album, regardless of edition". The CD, the LP and
        # the digital release of one record are three releases sharing one
        # release group, which is what lets the pages collapse them into a
        # single row. NULL for rows cached before this column existed, and the
        # readers fall back to the release's own gid so an unmigrated row is
        # simply its own group rather than being merged with everything else
        # that has no group.
        Field("release_group_gid"),
        # 'Album', 'Single', 'EP', ... — the group's primary type.
        Field("release_group_type"),
        # When the *work* first came out anywhere, on any label — as opposed to
        # `date`, which is when this particular edition did. The two diverge on
        # a reissue, and the gap is the only thing that distinguishes Light in
        # the Attic putting a 1983 record back in print from a label pressing
        # its own album again. Partial date, same format as `date`.
        Field("release_group_first_date"),
        Field("country"),
        Field("status"),
        Field("disambiguation"),
        # Tri-state: True/False when known, NULL when never looked up.
        Field("has_front_cover", "boolean"),
        Field("fetched_at", "datetime"),
        migrate=migrate,
        redefine=True,
    )

    cache_db.define_table(
        "mb_release_label",
        Field("release_gid"),
        Field("label_gid"),
        Field("catalog_number"),
        migrate=migrate,
        redefine=True,
    )

    cache_db.define_table(
        "mb_release_url",
        Field("release_gid"),
        Field("service"),
        Field("url"),
        Field("rel_type"),
        migrate=migrate,
        redefine=True,
    )

    cache_db.define_table(
        "mb_sync_state",
        Field("label_gid", unique=True),
        Field("status", default=SYNC_NEVER),
        Field("release_count_remote", "integer"),
        Field("release_count_local", "integer", default=0),
        Field("last_full_sync_at", "datetime"),
        Field("last_checked_at", "datetime"),
        Field("source"),
        Field("error_message"),
        Field("error_count", "integer", default=0),
        migrate=migrate,
        redefine=True,
    )

    # Remembers what a search returned, so repeating a query is free.
    #
    # Stores result MBIDs rather than the labels themselves; those live in
    # mb_label. A friend group searching for the same handful of labels turns
    # almost every search into a cache hit within days.
    cache_db.define_table(
        "mb_search_cache",
        Field("query_norm"),
        Field("entity_type", default="label"),
        Field("result_gids", "json", default=[]),
        Field("fetched_at", "datetime"),
        migrate=migrate,
        redefine=True,
    )

    cache_db.commit()

    if migrate:
        create_indexes(cache_db)

    return cache_db


def create_indexes(cache_db):
    """
    Create the indexes pydal doesn't express declaratively.

    The unique index on (release_gid, label_gid) is what makes the writer's
    upserts idempotent when a sync is interrupted and rerun.
    """
    for statement in _INDEXES:
        cache_db.executesql(statement)
    cache_db.commit()
