"""
Keeping the cache tidy.

The cache accumulates rows nothing can reach. A release whose last label link is
pruned — because MusicBrainz stopped listing it under that label, or because an
older writer left it unlinked — stays in mb_release forever, invisible to every
page and counted by nothing. Search results expire but their rows do not. None
of it is harmful, and all of it is dead weight in a file whose whole point is
being small enough to ship prewarmed.

Everything here is derived data. The cache is rebuildable by design (see the
plan, section 4.2), so the worst case for an over-eager delete is a re-fetch —
but a delete that races a sync would make a *complete* label look incomplete, so
every rule below is written to only touch rows no sync could still be filling
in. That is what the grace period is for.
"""

import datetime


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def cleanup_cache(cache_db, search_ttl_days=7, grace_days=1, vacuum=True, logger=None):
    """
    Drop unreachable rows, then reclaim the space.

    Returns a dict of what went, so the scheduler run records it.

    `grace_days` keeps anything fetched recently, however unreachable it looks.
    A sync commits a page's releases and their label links in one transaction,
    so a half-written page is never visible to this process — but the margin
    costs nothing and means a future writer that separates the two cannot be
    silently undone by a cleanup that happens to run between them.
    """
    cutoff = utcnow() - datetime.timedelta(days=grace_days)
    removed = {
        "releases": _delete_unlinked_releases(cache_db, cutoff),
        "urls": _delete_dangling_urls(cache_db),
        "links": _delete_dangling_links(cache_db),
        "searches": _delete_expired_searches(cache_db, search_ttl_days),
    }
    cache_db.commit()

    if logger and any(removed.values()):
        logger.info("cache cleanup removed %s", removed)

    if vacuum:
        removed["reclaimed_bytes"] = _vacuum(cache_db, logger)
    return removed


def _delete_unlinked_releases(cache_db, cutoff):
    """
    Releases no label points at any more.

    mb_release_label is the only route a page has to a release, so a release
    with no link row cannot be rendered, counted, or paginated to. These arrive
    two ways: prune_label_links dropping the last label that listed it, and an
    earlier writer that inserted releases before linking them.
    """
    return _delete_gids(
        cache_db,
        cache_db.mb_release.gid,
        """
        SELECT r.gid FROM mb_release r
        WHERE r.fetched_at < ?
          AND NOT EXISTS (
            SELECT 1 FROM mb_release_label rl WHERE rl.release_gid = r.gid
          )
        """,
        (cutoff.strftime("%Y-%m-%d %H:%M:%S"),),
    )


def _delete_dangling_urls(cache_db):
    """Streaming links whose release is gone."""
    return _delete_gids(
        cache_db,
        cache_db.mb_release_url.release_gid,
        """
        SELECT DISTINCT u.release_gid FROM mb_release_url u
        WHERE NOT EXISTS (SELECT 1 FROM mb_release r WHERE r.gid = u.release_gid)
        """,
        (),
    )


def _delete_dangling_links(cache_db):
    """
    Label links whose release is gone.

    Should not happen — releases are only deleted here, after their links — but
    a link pointing at nothing inflates count_releases, which the sync compares
    against the source's count to decide whether a label is up to date. A stale
    row here would mean a full re-crawl every night.
    """
    return _delete_gids(
        cache_db,
        cache_db.mb_release_label.release_gid,
        """
        SELECT DISTINCT rl.release_gid FROM mb_release_label rl
        WHERE NOT EXISTS (SELECT 1 FROM mb_release r WHERE r.gid = rl.release_gid)
        """,
        (),
    )


def _delete_gids(cache_db, field, sql, params, chunk=500):
    """
    Delete rows whose gid the query names, and report how many.

    Selecting first and deleting by key rather than issuing one correlated
    DELETE buys two things: an exact count for the run's output, and NOT EXISTS
    semantics instead of NOT IN, which returns nothing at all the moment the
    subquery contains a single NULL.
    """
    gids = [row[0] for row in cache_db.executesql(sql, params) if row[0] is not None]
    for start in range(0, len(gids), chunk):
        cache_db(field.belongs(gids[start : start + chunk])).delete()
    return len(gids)


def _delete_expired_searches(cache_db, ttl_days):
    """
    Cached searches past their TTL.

    They are already ignored on read — _cached_search checks fetched_at — so
    this only reclaims the space, and dropping one costs at most a single
    request the next time anyone runs that query.
    """
    if ttl_days is None:
        return 0
    table = cache_db.mb_search_cache
    cutoff = utcnow() - datetime.timedelta(days=ttl_days)
    return cache_db(table.fetched_at < cutoff).delete()


def _vacuum(cache_db, logger=None):
    """
    Rebuild the file so deleted pages are returned to the filesystem.

    Deleting rows only marks pages free for SQLite to reuse; the file never
    shrinks on its own, and a prewarmed cache is meant to be shipped. VACUUM
    cannot run inside a transaction and needs the write lock, so a busy moment
    makes it fail — which is not worth failing the run over, since the next
    sweep will try again.
    """
    before = _page_bytes(cache_db)
    try:
        cache_db.executesql("VACUUM")
        cache_db.commit()
    except Exception as error:  # noqa: BLE001 - purely an optimisation
        if logger:
            logger.info("cache vacuum skipped: %s", error)
        return 0
    return max(0, before - _page_bytes(cache_db))


def _page_bytes(cache_db):
    rows = cache_db.executesql("PRAGMA page_count")
    pages = rows[0][0] if rows else 0
    rows = cache_db.executesql("PRAGMA page_size")
    size = rows[0][0] if rows else 0
    return pages * size
