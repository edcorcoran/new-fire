"""
The app's pages.

Every action here reads the SQLite cache rather than MusicBrainz. The one
exception is label search, which may go out to the web service for a query
nobody has run before; release syncing is always handed to the scheduler, so no
page ever blocks on a crawl. See docs/musicbrainz-cache-plan.md section 4.
"""

import os

from ombott import static_file
from py4web import URL, abort, action, redirect, request
from py4web.utils.url_signer import URLSigner

from .common import (
    T,
    auth,
    db,
    mb_cache,
    mb_fixtures,
    mb_source,
    flash,
    logger,
    session,
)
from .musicbrainz import (
    count_recent_releases,
    count_release_groups,
    count_releases,
    ensure_label_cached,
    get_recent_releases,
    get_releases,
    is_complete,
    search_labels,
)
from .settings import (
    APP_FOLDER,
    MB_CACHE_TTL_DAYS,
    MB_HIDE_LABEL_TYPES,
    MB_SEARCH_TTL_DAYS,
)
from .tasks import queue_label_sync

PAGE_SIZE = 24
FEED_SIZE = 24
HOME_FEED_SIZE = 10
SEARCH_SIZE = 25

# Signs the follow/unfollow URLs so the POSTs only work from pages this app
# rendered. SameSite=Lax on the session cookie already blocks the classic
# cross-site form, but a signature costs nothing and doesn't depend on the
# browser to enforce it.
url_signer = URLSigner(session)

# Filter values the pages accept, and the only ones they accept: anything else
# in the query string is ignored rather than passed to the reader. These are
# user-facing URL values, so they are spelled for humans, not for MusicBrainz.
FILTER_VALUES = {
    "type": {"album", "ep", "single", "other"},
    "reissue": {"hide", "only"},
    "status": {"released", "upcoming"},
    "service": {"any", "apple_music", "spotify", "bandcamp"},
}


@action("index")
@action.uses("index.html", db, session, mb_cache, auth, T)
def index():
    """
    The front door: search, and a taste of what the followed labels just put out.

    Deliberately cache-only, like /tracked. The home page is the one people load
    without meaning to, so it must never be the page that waits on MusicBrainz.
    """
    gids = tracked_gids()
    return dict(
        signed_in=bool(auth.user_id),
        following=len(gids),
        feed=get_recent_releases(mb_cache, gids, limit=HOME_FEED_SIZE),
    )


@action("label/<gid>")
@action.uses("label.html", db, session, mb_cache, auth, T, url_signer, *mb_fixtures)
def label_page(gid):
    """
    A label and its releases, served from the local cache.

    Nothing here waits on a release sync. A label that is cached renders
    immediately, even if its data is past its TTL — the refresh is handed to the
    scheduler and lands before the next visit. A label that is not cached yet
    renders a syncing notice while the background job fills it in.

    Only signed-in visitors trigger MusicBrainz work. Anonymous traffic is
    served purely from the cache — an uncached label 404s rather than costing a
    rate-limited request, so a crawler walking label URLs cannot starve the
    limiter or fill the sync queue.
    """
    label, state = ensure_label_cached(
        mb_cache,
        mb_source,
        gid,
        ttl_days=MB_CACHE_TTL_DAYS,
        request_sync=queue_label_sync,
        logger=logger,
        allow_fetch=bool(auth.user_id),
    )
    if label is None:
        abort(404)

    filters = read_filters()
    # Pagination counts albums, not editions: the page shows one row per
    # release group, so counting releases would promise pages that don't exist.
    # It also counts what the filters leave, so the last page is never empty.
    total = count_release_groups(mb_cache, gid, filters)
    unfiltered = count_release_groups(mb_cache, gid) if filters else total
    editions = count_releases(mb_cache, gid)
    page, total_pages = _paginate(total, PAGE_SIZE)

    # Releases may only be ordered by date once the whole label is cached; the
    # sources hand them over in an order unrelated to release date, so sorting a
    # partial cache newest-first surfaces the wrong end of the catalogue.
    complete = is_complete(state)
    releases = (
        get_releases(mb_cache, gid, PAGE_SIZE, (page - 1) * PAGE_SIZE, filters)
        if complete
        else []
    )

    return dict(
        label=label,
        releases=releases,
        page=page,
        total_pages=total_pages,
        total=total,
        unfiltered=unfiltered,
        editions=editions,
        filters=filters,
        filter_labels=None,
        gid=gid,
        complete=complete,
        signed_in=bool(auth.user_id),
        tracked=is_tracked(gid),
        sync_status=state.status if state else None,
        sync_error=state.error_message if state else None,
        url_signer=url_signer,
    )


@action("search")
@action.uses("search.html", db, session, mb_cache, auth, T, *mb_fixtures)
def search():
    """
    Find labels by name.

    Hits MusicBrainz only when the query hasn't been seen recently. That request
    is made synchronously, unlike release syncing, because a search that could
    only return labels somebody had already visited would be useless for finding
    anything new — and it is one request, not thirteen.

    Anonymous searches are answered from the cache alone, so only signed-in
    visitors can spend rate-limited requests on novel queries. The page says so
    when it applies.
    """
    query = (request.params.get("q") or "").strip()
    signed_in = bool(auth.user_id)
    labels, from_cache = (
        search_labels(
            mb_cache,
            mb_source,
            query,
            limit=SEARCH_SIZE,
            ttl_days=MB_SEARCH_TTL_DAYS,
            logger=logger,
            exclude_types=MB_HIDE_LABEL_TYPES,
            allow_remote=signed_in,
        )
        if query
        else ([], True)
    )

    tracked_gids = set()
    if auth.user_id and labels:
        tracked_gids = {
            row.label_gid
            for row in db(
                (db.tracked_label.created_by == auth.user_id)
                & (db.tracked_label.label_gid.belongs([l.gid for l in labels]))
            ).select(db.tracked_label.label_gid)
        }

    return dict(
        query=query,
        results=[
            dict(
                gid=label.gid,
                name=label.name,
                disambiguation=label.disambiguation,
                label_type=label.label_type,
                area_name=label.area_name,
                releases=count_releases(mb_cache, label.gid),
                tracked=label.gid in tracked_gids,
            )
            for label in labels
        ],
        from_cache=from_cache,
        signed_in=signed_in,
    )


def read_filters(valid_labels=None):
    """
    Collect the display filters from the query string, ignoring anything else.

    Filters live in the URL rather than the session so a filtered view can be
    bookmarked and shared, and so the back button does what it looks like it
    does. Unknown keys and unknown values are dropped rather than rejected: a
    stale link with a filter that no longer exists should still show records.
    """
    filters = {}
    for key, allowed in FILTER_VALUES.items():
        value = (request.params.get(key) or "").strip().lower()
        if value in allowed:
            filters[key] = value
    label = (request.params.get("label") or "").strip()
    if label and (valid_labels is None or label in valid_labels):
        filters["label"] = label
    return filters


def _paginate(total, page_size):
    """Clamp the requested page to what exists. Returns (page, total_pages)."""
    total_pages = max(1, -(-total // page_size))  # ceiling division
    return min(max(1, _int_param("page", 1)), total_pages), total_pages


def _int_param(name, default):
    """Read a positive integer query parameter, ignoring junk."""
    try:
        return int(request.params.get(name, default))
    except (TypeError, ValueError):
        return default


def tracked_gids():
    """
    The labels the signed-in user follows, most recently followed first.

    Returns MBIDs rather than rows because the labels themselves live in the
    cache, which is a separate database and cannot be joined against this one.
    """
    if not auth.user_id:
        return []
    return [
        row.label_gid
        for row in db(db.tracked_label.created_by == auth.user_id).select(
            db.tracked_label.label_gid, orderby=~db.tracked_label.created_on
        )
    ]


def is_tracked(label_gid):
    """Whether the signed-in user follows this label."""
    if not auth.user_id:
        return False
    return bool(
        db(
            (db.tracked_label.created_by == auth.user_id)
            & (db.tracked_label.label_gid == label_gid)
        ).count()
    )


@action("track/<gid>", method="POST")
@action.uses(db, session, flash, auth.user, url_signer.verify())
def track_label(gid):
    """
    Follow a label.

    Inserting is guarded by a unique index rather than a check-then-insert, so a
    double-submitted form is a no-op instead of a duplicate row or an error.
    """
    try:
        db.tracked_label.insert(label_gid=gid)
        db.commit()
        flash.set(T("Following this label"), sanitize=True)
    except Exception:  # noqa: BLE001 - unique index means already following
        db.rollback()
        flash.set(T("Already following this label"), sanitize=True)
    redirect(URL("label", gid))


@action("untrack/<gid>", method="POST")
@action.uses(db, session, flash, auth.user, url_signer.verify())
def untrack_label(gid):
    """Stop following a label. The cached releases stay; they cost nothing."""
    db(
        (db.tracked_label.created_by == auth.user_id)
        & (db.tracked_label.label_gid == gid)
    ).delete()
    db.commit()
    flash.set(T("No longer following this label"), sanitize=True)
    redirect(URL("label", gid))


@action("tracked")
@action.uses("tracked.html", db, session, mb_cache, auth.user, T)
def tracked():
    """
    Followed labels, and the newest releases across them.

    Reads only the cache: whatever the nightly sweep last pulled in. Nothing
    here reaches out to MusicBrainz, so the page is fast regardless of how many
    labels are followed or how deep the reader pages into them.
    """
    gids = tracked_gids()

    # The cache is a separate database, so the label names are fetched
    # separately and matched up here rather than joined.
    labels = {row.gid: row for row in mb_cache(mb_cache.mb_label.gid.belongs(gids)).select()} if gids else {}

    followed = [
        dict(
            label_gid=gid,
            name=labels[gid].name if gid in labels else gid,
            disambiguation=labels[gid].disambiguation if gid in labels else "",
            releases=count_releases(mb_cache, gid),
        )
        for gid in gids
    ]

    filters = read_filters(valid_labels=set(gids))
    total = count_recent_releases(mb_cache, gids, filters)
    unfiltered = count_recent_releases(mb_cache, gids) if filters else total
    page, total_pages = _paginate(total, FEED_SIZE)

    return dict(
        followed=followed,
        feed=get_recent_releases(
            mb_cache,
            gids,
            limit=FEED_SIZE,
            offset=(page - 1) * FEED_SIZE,
            filters=filters,
        ),
        page=page,
        total_pages=total_pages,
        total=total,
        unfiltered=unfiltered,
        filters=filters,
        # The label filter is only meaningful where there is more than one.
        filter_labels=followed if len(followed) > 1 else None,
    )


# The icons again, at the site root.
#
# The <link> tags in layout.html are the whole story for a tab icon, but not
# for a bookmark: Safari fills its icon cache by probing /favicon.ico and
# /apple-touch-icon.png directly, and what it finds -- or fails to find -- there
# is what the bookmark, the Favorites bar and the Start Page tile show. Both
# were 404 because this app serves everything it owns out of static/.
#
# Serving the bytes rather than redirecting into static/: a redirect is one
# more thing for an icon fetch to get wrong, and these are two small files.
STATIC_FOLDER = os.path.join(APP_FOLDER, "static")


@action("favicon.ico")
def favicon():
    return static_file("favicon.ico", root=STATIC_FOLDER)


@action("apple-touch-icon.png")
def apple_touch_icon():
    return static_file("apple-touch-icon.png", root=STATIC_FOLDER)
