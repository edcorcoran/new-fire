"""
Background tasks.

The only task so far syncs one label's releases into the cache. It exists
because that work is far too slow for a page request: a label the size of Drag
City is 13 rate-limited requests, roughly half a minute, and Ninja Tune is
twice that.
"""

import threading

from .common import build_mb_runtime, db, logger, scheduler, settings
from .musicbrainz.maintenance import cleanup_cache
from .musicbrainz.reader import count_releases
from .musicbrainz.service import is_stale
from .musicbrainz.writer import get_sync_state, sync_label, sync_label_incremental

SYNC_LABEL_TASK = "mb_sync_label"
REFRESH_TRACKED_TASK = "mb_refresh_tracked"
CLEANUP_TASK = "mb_cleanup_cache"

# Runs the scheduler considers still outstanding. A label with one of these
# already queued must not be queued again.
PENDING_STATUSES = ("queued", "assigned", "running")


def sync_label_task(label_gid=None, **_):
    """
    Fetch a label's releases into the cache.

    Runs in a process the scheduler forked, so it opens its own unpooled
    connections rather than reusing — or being handed, via pydal's per-URI
    connection pool — the ones the web process built. See build_mb_runtime.
    """
    if not label_gid:
        return {"ok": False, "error": "no label_gid given"}

    cache, source, mirror = build_mb_runtime(pool_size=0)
    try:
        state = sync_label(source, cache, label_gid)
        return {
            "ok": state.status == "complete",
            "label_gid": label_gid,
            "status": state.status,
            "releases": state.release_count_local,
        }
    finally:
        cache.close()
        if mirror is not None:
            mirror.close()


# Checking the queue and adding to it must not interleave. Every enqueue comes
# from a web request thread in this one process, so a lock is enough; without it
# a burst of visitors to the same uncached label all read an empty queue before
# any of them writes to it, and each queues its own redundant sync.
_enqueue_lock = threading.Lock()


def queue_label_sync(label_gid):
    """
    Ask for a label to be synced in the background.

    Does nothing when a sync for the same label is already outstanding, so a
    burst of visitors to an uncached label produces one job rather than one per
    request. Returns True when a run was actually enqueued.
    """
    if not scheduler:
        return False

    with _enqueue_lock:
        pending = db(
            (db.task_run.name == SYNC_LABEL_TASK)
            & (db.task_run.status.belongs(PENDING_STATUSES))
        ).select(db.task_run.inputs)
        # inputs is a JSON column; matching in Python avoids depending on
        # SQLite's JSON support, and the pending queue is only ever small.
        if any((row.inputs or {}).get("label_gid") == label_gid for row in pending):
            return False

        scheduler.enqueue_run(
            SYNC_LABEL_TASK,
            description=f"sync MusicBrainz label {label_gid}",
            inputs={"label_gid": label_gid},
            timeout=settings.MB_SYNC_TIMEOUT,
        )
    logger.info("queued MusicBrainz sync for label %s", label_gid)
    return True


def refresh_tracked_task(**_):
    """
    Check every followed label for new releases, and resync the ones that moved.

    The count check is what makes this affordable. Asking a label's release count
    is one request; if it matches what the cache holds, nothing was added or
    removed and the label is skipped entirely. Fifty followed labels of which a
    couple changed costs roughly fifty requests — about a minute — instead of the
    ~650 a nightly full re-crawl would need.

    Its blind spot is a count-neutral edit: a release retitled, a Bandcamp link
    added, or one added while another was removed. MB_FULL_RESYNC_DAYS covers
    that by forcing a full resync of each label periodically regardless.
    """
    tracked = sorted(
        {row.label_gid for row in db(db.tracked_label).select(db.tracked_label.label_gid)}
    )
    if not tracked:
        return {"tracked": 0}

    cache, source, mirror = build_mb_runtime(pool_size=0)
    checked = changed = queued = incrementals = failed = 0
    try:
        for label_gid in tracked:
            checked += 1
            try:
                remote = source.count_releases_by_label(label_gid)
                local = count_releases(cache, label_gid)
                state = get_sync_state(cache, label_gid)
                stale = is_stale(state, settings.MB_FULL_RESYNC_DAYS)

                if remote == local and not stale:
                    continue

                changed += 1

                # Try the cheap path first: ask only for releases dated since
                # around the last sync and fill those in. It returns None when it
                # can't account for the whole difference, and then the label is
                # paged in full.
                incremental = None
                if remote > local and not stale:
                    incremental = sync_label_incremental(
                        source,
                        cache,
                        label_gid,
                        since_year=_recent_year(state),
                        max_new=settings.MB_INCREMENTAL_MAX_NEW,
                        logger=logger,
                    )

                if incremental is not None:
                    incrementals += 1
                    logger.info(
                        "tracked label %s: caught up incrementally (%s releases)",
                        label_gid,
                        incremental.release_count_local,
                    )
                    continue

                if queue_label_sync(label_gid):
                    queued += 1
                logger.info(
                    "tracked label %s: remote=%s local=%s stale=%s -> full resync",
                    label_gid,
                    remote,
                    local,
                    stale,
                )
            except Exception as error:  # noqa: BLE001 - one bad label must not
                failed += 1              # abort the rest of the sweep
                logger.warning(
                    "count check failed for tracked label %s: %s", label_gid, error
                )
    finally:
        cache.close()
        if mirror is not None:
            mirror.close()

    return {
        "tracked": len(tracked),
        "checked": checked,
        "changed": changed,
        "incremental": incrementals,
        "queued": queued,
        "failed": failed,
    }


def cleanup_cache_task(**_):
    """
    Drop cache rows nothing can reach, and hand the space back.

    Runs weekly rather than after each sync: the rows it collects are produced a
    few at a time by prune_label_links, and VACUUM wants the write lock, which is
    not something to take on a schedule measured in minutes.
    """
    cache, _source, mirror = build_mb_runtime(pool_size=0)
    try:
        return cleanup_cache(
            cache,
            search_ttl_days=settings.MB_SEARCH_TTL_DAYS,
            grace_days=settings.MB_CLEANUP_GRACE_DAYS,
            logger=logger,
        )
    finally:
        cache.close()
        if mirror is not None:
            mirror.close()


def _recent_year(state):
    """
    Year to search from when catching a label up.

    Backs off a year from the last successful sync so a release dated slightly
    before it — MusicBrainz dates are entered by hand and often backfilled — is
    still inside the window.
    """
    last = getattr(state, "last_full_sync_at", None) if state else None
    return (last.year - 1) if last else 1900


if settings.USE_SCHEDULER:
    # Register in every process, including ones that will never run the loop.
    # Registration is what makes a task name known; a scheduler polling with an
    # empty registry marks the runs it finds as "unknown" and discards them, so
    # the ordering matters wherever the loop does start. See scheduling.py.
    scheduler.register_task(SYNC_LABEL_TASK, sync_label_task)
    scheduler.register_task(REFRESH_TRACKED_TASK, refresh_tracked_task)
    scheduler.register_task(CLEANUP_TASK, cleanup_cache_task)

if settings.USE_SCHEDULER and settings.RUN_SCHEDULER:
    scheduler.start()

    # One standing periodic run, enqueued only by the process that runs the
    # loop. The guard below is read-then-write across processes, and web
    # processes are neither single nor coordinated: two of them starting
    # together would both see nothing pending and both enqueue. Since the
    # scheduler reschedules a periodic task after each completion, that
    # duplicate would double the sweep permanently rather than resolve itself.
    # Only one worker ever runs the loop -- cron holds a lock to guarantee it,
    # see DEPLOY.md -- so confining the enqueue here removes the race.
    if not db(
        (db.task_run.name == REFRESH_TRACKED_TASK)
        & (db.task_run.status.belongs(PENDING_STATUSES))
    ).count():
        scheduler.enqueue_run(
            REFRESH_TRACKED_TASK,
            description="check followed labels for new releases",
            inputs={},
            timeout=settings.MB_SYNC_TIMEOUT,
            period=settings.MB_REFRESH_PERIOD,
        )

    if not db(
        (db.task_run.name == CLEANUP_TASK)
        & (db.task_run.status.belongs(PENDING_STATUSES))
    ).count():
        scheduler.enqueue_run(
            CLEANUP_TASK,
            description="drop unreachable cache rows",
            inputs={},
            timeout=settings.MB_SYNC_TIMEOUT,
            period=settings.MB_CLEANUP_PERIOD,
        )
