"""
Ties the cache and its sources together for the request path.

The rule this module implements: **a page request never waits on a full label
sync.** Fetching a label's releases costs 13 requests and about half a minute
over the web service, which is fine for a background job and unacceptable for
someone loading a page.

So a request does at most one cheap thing — fetch the label record itself, a
single request — and hands the expensive part to the scheduler. A label that is
already cached is served immediately even when stale, and refreshed behind the
reader's back.

See docs/musicbrainz-cache-plan.md sections 4.3 and 4.4.
"""

import datetime

from .cache import SYNC_COMPLETE
from .reader import get_label, is_complete
from .writer import get_sync_state, sync_label, upsert_label, utcnow

# Label types that are companies rather than imprints, and so are never the
# answer to "whose new records do I want to see". Measured against the mirror,
# these are also the types least likely to carry releases at all: 72% of
# Publishers, 79% of Rights Societies and 58% of Holdings have none. The ones
# that do carry releases are worse, not better — a search for "universal"
# otherwise offers Universal Music Group, a holding company with 3,966 releases
# nobody follows, above the imprints that actually put records out.
#
# Deliberately excludes Distributor (16% empty) and Broadcaster (20%), which
# name real catalogues someone might follow.
NON_RELEASING_LABEL_TYPES = frozenset(
    {"Holding", "Publisher", "Rights Society", "Creative Agency", "Manufacturer"}
)


def is_stale(state, ttl_days, now=None):
    """
    True when a fully-synced label is old enough to be worth refreshing.

    A label that has never completed a sync is always stale.
    """
    if not state or not state.last_full_sync_at:
        return True
    age = (now or utcnow()) - state.last_full_sync_at
    return age > datetime.timedelta(days=ttl_days)


def ensure_label_record(cache_db, source, label_gid, allow_fetch=True):
    """
    Make sure the label itself is cached, without touching its releases.

    This is the one piece of remote work a page request may do: a single
    request, needed because there is nothing to render — not even a heading or
    a "syncing" notice — without knowing the label exists and what it is called.

    allow_fetch=False makes this cache-only: an uncached label returns None
    rather than costing a rate-limited request. That is how anonymous traffic is
    kept from spending the MusicBrainz budget — a crawler hitting random label
    URLs would otherwise stall the shared limiter one request at a time.

    Returns the cached label row, or None if no such label exists (or, with
    allow_fetch=False, is not cached).
    """
    label = get_label(cache_db, label_gid)
    if label is not None:
        return label

    if not allow_fetch:
        return None

    fetched = source.get_label(label_gid)
    if fetched is None:
        return None

    upsert_label(cache_db, fetched)
    cache_db.commit()
    return get_label(cache_db, label_gid)


def ensure_label_cached(
    cache_db,
    source,
    label_gid,
    ttl_days=7,
    request_sync=None,
    force=False,
    logger=None,
    allow_fetch=True,
):
    """
    Prepare a label for display, arranging a release sync if one is due.

    `request_sync` is a callable taking the label gid; supply one to hand the
    work to the scheduler. Without it the sync runs inline, which is what the
    seeding CLI wants and what happens when the scheduler is switched off.

    allow_fetch=False serves strictly from the cache: no label fetch, no sync
    queued. Pages pass this for anonymous visitors, so only signed-in traffic
    can create MusicBrainz work; whatever is already cached still renders, and
    staleness is covered by the nightly sweep.

    Returns (label, sync_state). A None label means no such label exists (or is
    not cached, when fetching is disallowed) and the caller should 404 —
    distinct from a label that exists but has no releases cached yet, where the
    state says so.
    """
    label = ensure_label_record(cache_db, source, label_gid, allow_fetch=allow_fetch)
    if label is None:
        return None, get_sync_state(cache_db, label_gid)

    state = get_sync_state(cache_db, label_gid)
    if allow_fetch and (force or not is_complete(state) or is_stale(state, ttl_days)):
        if request_sync is not None:
            # Stale-while-revalidate: whatever is already cached gets served
            # now, and the refresh lands before the next visit.
            request_sync(label_gid)
        else:
            state = fill_label_cache(cache_db, source, label_gid, logger=logger)

    return label, state


def normalize_query(query):
    """Collapse a search string so trivially different spellings share a cache entry."""
    return " ".join((query or "").split()).lower()


def search_labels(
    cache_db,
    source,
    query,
    limit=25,
    ttl_days=7,
    logger=None,
    exclude_types=None,
    allow_remote=True,
):
    """
    Find labels by name, from the cache and from MusicBrainz.

    The cache cannot answer this on its own. It only knows labels somebody has
    already looked at, and MusicBrainz search is a Lucene index that a handful of
    SQLite rows cannot stand in for — so a genuinely new query has to go out to
    the service. What the cache *can* do is remember the answer: repeat searches,
    and searches for labels already seen, cost nothing.

    Remote results are written into the cache as they arrive, so following a
    search result never needs to fetch the label again.

    allow_remote=False answers from the cache alone — stored searches and local
    name matches — so anonymous visitors cannot spend rate-limited requests on
    novel queries. Like a failed remote search, the cache-only answer is not
    stored, so it cannot mask the real results from the next signed-in search.

    Returns (labels, from_cache).
    """
    normalized = normalize_query(query)
    if not normalized:
        return [], True

    cached = _cached_search(cache_db, normalized, ttl_days)
    if cached is not None:
        return _resolve_labels(cache_db, cached, limit, exclude_types), True

    local = [row.gid for row in _local_matches(cache_db, normalized, limit)]

    if not allow_remote:
        return _resolve_labels(cache_db, local, limit, exclude_types), True

    remote_gids = []
    try:
        for label in source.search_labels(query, limit=limit) or []:
            upsert_label(cache_db, label)
            remote_gids.append(label["gid"])
        cache_db.commit()
    except Exception as error:  # noqa: BLE001 - degrade to local-only results
        cache_db.rollback()
        if logger:
            logger.warning("MusicBrainz label search failed for %r: %s", query, error)
        # Deliberately not cached: a failed lookup must not suppress the real
        # results for the next week.
        return _resolve_labels(cache_db, local, limit, exclude_types), False

    # Remote order carries MusicBrainz's relevance ranking, so it leads; local
    # matches it didn't return are appended rather than dropped.
    merged = remote_gids + [gid for gid in local if gid not in set(remote_gids)]
    _store_search(cache_db, normalized, merged)
    return _resolve_labels(cache_db, merged, limit, exclude_types), False


def _local_matches(cache_db, normalized, limit):
    labels = cache_db.mb_label
    return cache_db(labels.name.lower().contains(normalized)).select(
        labels.gid, orderby=labels.name, limitby=(0, limit)
    )


def _cached_search(cache_db, normalized, ttl_days):
    """Result gids for a query if they were fetched recently, else None."""
    row = (
        cache_db(
            (cache_db.mb_search_cache.query_norm == normalized)
            & (cache_db.mb_search_cache.entity_type == "label")
        )
        .select()
        .first()
    )
    if row is None or not row.fetched_at:
        return None
    if utcnow() - row.fetched_at > datetime.timedelta(days=ttl_days):
        return None
    return list(row.result_gids or [])


def _store_search(cache_db, normalized, gids):
    table = cache_db.mb_search_cache
    values = dict(result_gids=list(gids), fetched_at=utcnow())
    existing = (
        cache_db(
            (table.query_norm == normalized) & (table.entity_type == "label")
        )
        .select()
        .first()
    )
    if existing:
        cache_db(table.id == existing.id).update(**values)
    else:
        table.insert(query_norm=normalized, entity_type="label", **values)
    cache_db.commit()


def _resolve_labels(cache_db, gids, limit, exclude_types=None):
    """
    Load cached labels for a list of gids, preserving the given order.

    Filters before limiting rather than after, so hiding a company does not
    leave a short page — the caller asked for `limit` labels worth following,
    not the followable subset of the first `limit` rows.

    Filtering happens on read, not when the search is cached, so the exclusion
    list can be changed without invalidating every stored search.
    """
    if not gids:
        return []
    excluded = NON_RELEASING_LABEL_TYPES if exclude_types is None else exclude_types
    rows = {
        row.gid: row
        for row in cache_db(cache_db.mb_label.gid.belongs(gids)).select()
    }
    empty = _known_empty(cache_db, gids)
    keep = [
        rows[gid]
        for gid in gids
        if gid in rows
        and rows[gid].label_type not in excluded
        and gid not in empty
    ]
    return keep[:limit]


def _known_empty(cache_db, gids):
    """
    Labels a finished sync has proved carry no releases at all.

    Only a complete sync counts. An unvisited label also holds zero cached
    releases, and hiding those would hide every label nobody had opened yet —
    which is most of what a search is for. MusicBrainz's label search carries no
    release count, so for labels never synced this is simply unknowable without
    a request each; measured over 100 hits, about 5% are empty.
    """
    state = cache_db.mb_sync_state
    return {
        row.label_gid
        for row in cache_db(
            state.label_gid.belongs(gids)
            & (state.status == SYNC_COMPLETE)
            & (state.release_count_remote == 0)
        ).select(state.label_gid)
    }


def fill_label_cache(cache_db, source, label_gid, logger=None):
    """
    Pull a label and all its releases into the cache now, in this process.

    A failure here is caught rather than raised: a sync that dies partway leaves
    its progress recorded in the sync state, and the page is more useful showing
    the releases it does have than an error screen. sync_label records the
    failure before re-raising, so the state is already accurate.

    It is logged, though. A swallowed exception here looks exactly like "this
    label does not exist" from the outside, which is a miserable thing to debug.
    """
    try:
        return sync_label(source, cache_db, label_gid)
    except Exception as error:  # noqa: BLE001 - recorded in state, logged here
        if logger:
            logger.exception(
                "musicbrainz sync failed for label %s: %s", label_gid, error
            )
        return get_sync_state(cache_db, label_gid)
