"""
Links this app found for itself, and the cache projection that shows them.

MusicBrainz is missing a great many Apple Music links for records it otherwise
describes well: of the Album and EP releases since 2024 in a 302-label cache,
623 carried no Apple Music relationship at all. Searching Apple's catalogue by
artist and title recovers a good share of them, and those matches are worth
showing even before MusicBrainz itself carries the link.

Where they live is the whole design here. They cannot live in the cache: the
cache is disposable and rebuildable by contract, and worse, writer's
_replace_release_urls deletes a release's URL set wholesale before re-inserting
what the source reported, so anything written there is gone at the next sync of
that label. So the record is a table in storage.db alongside tracked_label --
the database that is actually backed up -- and what lands in the cache is only
a *projection* of it, re-applied after each sync.

That indirection buys something concrete: the projected rows are ordinary
mb_release_url rows, so the reader's collapsing, the "Apple Music" service
filter (which is SQL against mb_release_url) and the templates all work with no
knowledge of this module at all. And because identity in that table is
(release_gid, url), a link MusicBrainz later publishes for itself collapses with
the one found here rather than doubling the row.
"""

import datetime

from py4web import Field

from .normalize import classify_url
from .writer import insert_or_ignore

# SQLite's default host-parameter ceiling is 999 on older builds, and a
# projection covers every discovered link at once, so `belongs` queries are
# chunked rather than trusting the set to stay small.
_CHUNK = 400


def define_discovered_link_table(db, migrate=True):
    """
    Define discovered_link on an app database.

    Lives here rather than in models.py so the import script can open
    storage.db directly and get the same schema, the way cache.py owns the
    cache's tables for both the app and scripts/seed_cache.py.

    Deliberately not auth.signature: most rows arrive from a batch import that
    has no signed-in user, and `source` answers the question a signature would
    be asked here -- which run produced this link, so a bad matching pass can be
    withdrawn by source rather than one row at a time.
    """
    db.define_table(
        "discovered_link",
        Field("release_gid"),
        Field("service"),
        Field("url"),
        # The MusicBrainz relationship type this link would be filed under, so
        # the projected row ranks against real ones in normalize.dedupe_urls.
        Field("rel_type"),
        # How the link was arrived at, e.g. "itunes-search-2026-09".
        Field("source"),
        Field("found_on", "datetime"),
        migrate=migrate,
        redefine=True,
    )
    return db.discovered_link


def create_discovered_indexes(db):
    """
    One link per service per release.

    A release has at most one Apple Music page worth linking, and a re-import
    should correct a row rather than accumulate alternatives beside it.
    """
    db.executesql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_discovered_link "
        "ON discovered_link (release_gid, service)"
    )


def define_link_lookup_table(db, migrate=True):
    """
    Define link_lookup: which releases have already been searched for.

    Without this the weekly sweep re-asks about every permanent miss forever,
    and permanent misses are the majority -- most releases with no Apple Music
    link genuinely have no Apple Music album page. Remembering the miss is what
    keeps an unattended job's cost proportional to *new* releases rather than to
    the whole back catalogue.

    Kept in storage.db beside discovered_link rather than in the cache, for the
    same reason: it is the record of an expensive external call, and losing it
    to a cache rebuild would mean making every one of those calls again.
    """
    db.define_table(
        "link_lookup",
        Field("release_gid"),
        Field("service"),
        Field("checked_on", "datetime"),
        # Whether that search found anything. A hit is worth keeping as well as
        # a miss: it dates the answer, so a re-check can be scheduled without
        # having to join back to discovered_link to find out what happened.
        Field("found", "boolean", default=False),
        migrate=migrate,
        redefine=True,
    )
    return db.link_lookup


def create_link_lookup_indexes(db):
    """One answer per release per service, replaced rather than appended to."""
    db.executesql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_link_lookup "
        "ON link_lookup (release_gid, service)"
    )


def recently_checked(db, service, within_days, now=None):
    """
    Releases searched recently enough not to search again.

    A miss is not necessarily permanent -- Apple's catalogue gains records, and
    a release announced before it is available will start matching later -- so
    this expires rather than being forever. `within_days` is that window.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now - datetime.timedelta(days=within_days)
    table = db.link_lookup
    rows = db(
        (table.service == service) & (table.checked_on > cutoff)
    ).select(table.release_gid)
    return {row.release_gid for row in rows}


def record_lookups(db, service, checked, now=None):
    """
    Note that these releases were searched, and what came of it.

    `checked` is an iterable of (release_gid, found) pairs, as
    applemusic.find_links returns.
    """
    checked = list(checked or [])
    if not checked:
        return 0

    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    table = db.link_lookup
    gids = [gid for gid, _found in checked]

    existing = {}
    for batch in _chunked(gids):
        for row in db(
            (table.service == service) & (table.release_gid.belongs(batch))
        ).select(table.id, table.release_gid):
            existing[row.release_gid] = row.id

    for gid, found in checked:
        row_id = existing.get(gid)
        if row_id is None:
            table.insert(
                release_gid=gid, service=service, checked_on=now, found=bool(found)
            )
        else:
            db(table.id == row_id).update(checked_on=now, found=bool(found))
    return len(checked)


def _chunked(values, size=_CHUNK):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start : start + size]


def read_discovered_links(db):
    """Every discovered link, as the plain dicts apply_discovered_links takes."""
    rows = db(db.discovered_link.id > 0).select()
    return [
        dict(
            release_gid=row.release_gid,
            service=row.service,
            url=row.url,
            rel_type=row.rel_type,
        )
        for row in rows
    ]


def apply_discovered_links(cache_db, links):
    """
    Project discovered links into the cache's URL table.

    Idempotent, and meant to be run after every sync of any label: a sync has
    just rebuilt the URL sets of the releases it touched, so this is what puts
    the found links back. Running it for every link rather than only the synced
    label's is deliberate -- it costs one pass over a small table and makes any
    label's sync repair the whole projection, including releases whose own
    label has not been synced in a while.

    Links whose release is not in the cache are skipped rather than inserted:
    an mb_release_url row with no mb_release is exactly what maintenance's
    cleanup collects, so writing one would be work undone on a timer.

    Returns the number of rows actually added.
    """
    links = [
        link
        for link in (links or [])
        if link.get("release_gid") and link.get("url")
    ]
    if not links:
        return 0

    gids = sorted({link["release_gid"] for link in links})

    known = set()
    existing = set()
    releases = cache_db.mb_release
    urls = cache_db.mb_release_url
    for batch in _chunked(gids):
        known.update(
            row.gid
            for row in cache_db(releases.gid.belongs(batch)).select(releases.gid)
        )
        existing.update(
            (row.release_gid, row.url)
            for row in cache_db(urls.release_gid.belongs(batch)).select(
                urls.release_gid, urls.url
            )
        )

    rows = []
    for link in links:
        gid, url = link["release_gid"], link["url"]
        if gid not in known or (gid, url) in existing:
            continue
        service = link.get("service") or classify_url(url)
        if not service:
            # A URL the app has no way to render is not worth caching; it would
            # only widen the "any service" filter with a link nothing displays.
            continue
        rows.append((gid, service, url, link.get("rel_type")))
        # Guard against the same link appearing twice in one batch, which the
        # unique index would reject mid-executemany.
        existing.add((gid, url))

    if rows:
        insert_or_ignore(
            cache_db,
            "INSERT OR IGNORE INTO mb_release_url "
            "(release_gid, service, url, rel_type) VALUES (?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def project_discovered_links(cache_db, db):
    """Read the links from storage.db and apply them to the cache."""
    return apply_discovered_links(cache_db, read_discovered_links(db))
