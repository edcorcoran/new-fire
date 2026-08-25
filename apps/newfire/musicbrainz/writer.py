"""
Writes normalized MusicBrainz records into the SQLite cache.

The writer is source-agnostic: it consumes the dicts from normalize.make_* and
never knows whether they came from the mirror or the web service.

Every operation is idempotent. Syncs get interrupted — the scheduler forks
processes that can be killed mid-run, and large labels take dozens of requests
during which the API can 503 — so rerunning a partial sync must converge rather
than duplicate.

See docs/musicbrainz-cache-plan.md sections 4.2 and 4.3.
"""

import datetime

from .cache import SYNC_COMPLETE, SYNC_ERROR, SYNC_PARTIAL
from .normalize import dedupe_urls


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _changed(record, values):
    """True if any of `values` differs from what's already stored."""
    return any(record[key] != value for key, value in values.items())


def insert_or_ignore(cache_db, statement, rows):
    """
    Insert many rows, skipping ones that violate a unique index.

    Needed because two syncs of the same label can overlap: pydal's scheduler
    forks a run before the previous one has reported itself as running, so a
    duplicate job can slip past the enqueue check. Without OR IGNORE the second
    sync aborts an entire task over a row that is already correct.
    """
    cursor = cache_db._adapter.cursor
    cursor.executemany(statement, rows)


def upsert_label(cache_db, label, now=None):
    """Insert or refresh one label. Returns its cache row id."""
    now = now or utcnow()
    values = dict(
        name=label["name"],
        sort_name=label.get("sort_name"),
        disambiguation=label.get("disambiguation") or "",
        label_type=label.get("label_type"),
        area_name=label.get("area_name"),
        label_code=label.get("label_code"),
        fetched_at=now,
    )
    table = cache_db.mb_label
    existing = cache_db(table.gid == label["gid"]).select().first()
    if existing:
        cache_db(table.id == existing.id).update(**values)
        return existing.id

    # Claim the row before filling it in. Two requests for the same uncached
    # label arrive together often enough — a link shared with several people —
    # and a plain insert lets the loser fail the whole request on mb_label.gid.
    insert_or_ignore(
        cache_db, "INSERT OR IGNORE INTO mb_label (gid) VALUES (?)", [(label["gid"],)]
    )
    cache_db(table.gid == label["gid"]).update(**values)
    row = cache_db(table.gid == label["gid"]).select().first()
    return row.id if row else None


def upsert_releases(cache_db, label_gid, releases, now=None):
    """
    Write a batch of releases, their label linkage and their streaming URLs.

    Reads the whole batch's existing rows up front rather than issuing a
    select-per-release, because seeding a label like Drag City writes 1,297 rows
    in 13 batches and per-row round trips dominate otherwise.

    Returns (inserted, updated).
    """
    if not releases:
        return 0, 0
    now = now or utcnow()

    # Collapse any release that arrives twice in one batch. The mirror source
    # pages over distinct releases so this should be a no-op, but a duplicate
    # here would hit the unique index on gid mid-insert and abort the whole
    # batch, and sources are allowed to be sloppier than this one.
    releases = list({release["gid"]: release for release in releases}.values())

    gids = [release["gid"] for release in releases]
    existing = {
        row.gid: row
        for row in cache_db(cache_db.mb_release.gid.belongs(gids)).select()
    }

    inserted = updated = 0
    new_rows = []
    for release in releases:
        values = dict(
            title=release["title"],
            artist_credit=release["artist_credit"],
            artist_gid=release.get("artist_gid"),
            release_group_gid=release.get("release_group_gid"),
            release_group_type=release.get("release_group_type"),
            release_group_first_date=release.get("release_group_first_date"),
            date=release.get("date"),
            country=release.get("country"),
            status=release.get("status"),
            disambiguation=release.get("disambiguation") or "",
            has_front_cover=release.get("has_front_cover"),
        )
        row = existing.get(release["gid"])
        if row is None:
            new_rows.append(dict(gid=release["gid"], fetched_at=now, **values))
            inserted += 1
        else:
            # Only touch rows whose content actually moved, so fetched_at stays
            # meaningful and SQLite isn't rewriting pages for no reason.
            if _changed(row, values):
                cache_db(cache_db.mb_release.id == row.id).update(
                    fetched_at=now, **values
                )
                updated += 1

    if new_rows:
        cache_db.mb_release.bulk_insert(new_rows)

    _link_releases_to_label(cache_db, label_gid, releases)
    _replace_release_urls(cache_db, releases)
    return inserted, updated


def _link_releases_to_label(cache_db, label_gid, releases):
    """
    Maintain mb_release_label for this batch.

    A release can hold several catalog numbers under one label, so identity here
    is (release, label, catalog_number) — one row per catalog number, plus a
    single null-catalog row for releases that have none. Rows whose catalog
    number vanished upstream are deleted, which is what keeps a rerun converging
    rather than accumulating stale variants.
    """
    table = cache_db.mb_release_label
    gids = [release["gid"] for release in releases]

    existing = {}
    for row in cache_db(
        (table.label_gid == label_gid) & (table.release_gid.belongs(gids))
    ).select():
        existing.setdefault(row.release_gid, {})[row.catalog_number] = row

    new_rows = []
    for release in releases:
        # Normalize "no catalog number" to a single None key so the release
        # still gets its label linkage row.
        wanted = set(release.get("catalog_numbers") or []) or {None}
        current = existing.get(release["gid"], {})

        for catalog_number in wanted - set(current):
            new_rows.append(
                (release["gid"], label_gid, catalog_number)
            )
        for catalog_number in set(current) - wanted:
            cache_db(table.id == current[catalog_number].id).delete()

    if new_rows:
        insert_or_ignore(
            cache_db,
            "INSERT OR IGNORE INTO mb_release_label "
            "(release_gid, label_gid, catalog_number) VALUES (?, ?, ?)",
            new_rows,
        )


def _replace_release_urls(cache_db, releases):
    """
    Replace the URL set for each release that carries one.

    Delete-then-insert rather than merge: URL relationships are removed upstream
    as well as added, and the set per release is tiny. Releases arriving with no
    urls key at all are left alone, so a source that doesn't fetch relationships
    (the search endpoint, for one) can't silently wipe good data.
    """
    table = cache_db.mb_release_url
    with_urls = [release for release in releases if release.get("urls") is not None]
    if not with_urls:
        return

    gids = [release["gid"] for release in with_urls]
    cache_db(table.release_gid.belongs(gids)).delete()

    new_rows = [
        (release["gid"], url["service"], url["url"], url.get("rel_type"))
        for release in with_urls
        # Defensive: the same URL legitimately carries several relationship
        # types upstream, and only one row per (release, url) can be stored.
        for url in dedupe_urls(release["urls"])
    ]
    if new_rows:
        insert_or_ignore(
            cache_db,
            "INSERT OR IGNORE INTO mb_release_url "
            "(release_gid, service, url, rel_type) VALUES (?, ?, ?, ?)",
            new_rows,
        )


def count_cached_releases(cache_db, label_gid):
    """
    How many distinct releases the cache holds for a label.

    Counts distinct releases, not linkage rows: a release with two catalog
    numbers occupies two rows but is one release. Getting this wrong would
    inflate the local count past the remote one and make a complete label look
    permanently out of sync.
    """
    return cache_db(cache_db.mb_release_label.label_gid == label_gid).count(
        distinct=cache_db.mb_release_label.release_gid
    )


def get_sync_state(cache_db, label_gid):
    """Return the sync row for a label, or None."""
    return (
        cache_db(cache_db.mb_sync_state.label_gid == label_gid).select().first()
    )


def update_sync_state(cache_db, label_gid, **values):
    """Insert or update a label's sync row and return it."""
    table = cache_db.mb_sync_state
    existing = get_sync_state(cache_db, label_gid)
    if existing:
        cache_db(table.id == existing.id).update(**values)
    else:
        table.insert(label_gid=label_gid, **values)
    return get_sync_state(cache_db, label_gid)


def mark_sync_started(cache_db, label_gid, total, source, now=None):
    """Record that a sync is under way and how many releases we expect."""
    now = now or utcnow()
    return update_sync_state(
        cache_db,
        label_gid,
        status=SYNC_PARTIAL,
        release_count_remote=total,
        release_count_local=count_cached_releases(cache_db, label_gid),
        last_checked_at=now,
        source=source,
        error_message=None,
    )


def mark_sync_progress(cache_db, label_gid, now=None):
    """Update the local count mid-sync so an interrupted run can resume."""
    return update_sync_state(
        cache_db,
        label_gid,
        release_count_local=count_cached_releases(cache_db, label_gid),
        last_checked_at=now or utcnow(),
    )


def mark_sync_complete(cache_db, label_gid, total, source, now=None, data_as_of=None):
    """
    Mark a label fully synced.

    Only after this may the label page paginate it by date — see the ordering
    finding in the plan, section 4.3.

    last_full_sync_at records how current the *data* is, not when the machine
    last ran, which is why a source that lags passes its own date in through
    data_as_of. last_checked_at stays the wall clock: the check did happen now.
    Conflating the two let 65 labels seeded from a mirror five months behind
    report a full sync today, which every staleness check downstream believed.
    """
    now = now or utcnow()
    return update_sync_state(
        cache_db,
        label_gid,
        status=SYNC_COMPLETE,
        release_count_remote=total,
        release_count_local=count_cached_releases(cache_db, label_gid),
        last_full_sync_at=data_as_of or now,
        last_checked_at=now,
        source=source,
        error_message=None,
        error_count=0,
    )


def mark_sync_error(cache_db, label_gid, message, now=None):
    """
    Record a failure without discarding what was already written.

    Status drops back to partial rather than error when some releases are
    already cached, so a later resume is an ordinary continuation.
    """
    now = now or utcnow()
    existing = get_sync_state(cache_db, label_gid)
    local = count_cached_releases(cache_db, label_gid)
    return update_sync_state(
        cache_db,
        label_gid,
        status=SYNC_PARTIAL if local else SYNC_ERROR,
        release_count_local=local,
        last_checked_at=now,
        error_message=str(message)[:500],
        error_count=(existing.error_count or 0) + 1 if existing else 1,
    )


def sync_label_incremental(
    source, cache_db, label_gid, since_year, max_new=25, logger=None
):
    """
    Pick up a label's recent additions without re-reading its whole catalogue.

    Costs about three requests where a full sync costs thirteen for a label the
    size of Drag City: one to list recent releases, then one per release that
    turns out to be new, because the search index carries no streaming links.

    Returns the sync-state row on success, or None to mean "this didn't work,
    do a full sync". It gives up rather than guessing whenever the result can't
    be trusted:

    - the search found nothing new, yet the counts still disagree
    - more new releases than max_new, where paging the label is cheaper anyway
    - the counts still disagree afterwards, which means something was missed

    That last check is what makes this safe to run unattended: an incremental
    sync that silently misses a release would leave the cache permanently wrong,
    so instead it declines to claim success and the next sweep does the thorough
    version.
    """
    label = get_label_row(cache_db, label_gid)
    if label is None or not label.name:
        return None

    remote_total = source.count_releases_by_label(label_gid)
    candidates = source.find_releases_since(label_gid, label.name, since_year)

    known = {
        row.release_gid
        for row in cache_db(
            cache_db.mb_release_label.label_gid == label_gid
        ).select(cache_db.mb_release_label.release_gid)
    }
    fresh = [release for release in candidates if release["gid"] not in known]

    if len(fresh) > max_new:
        if logger:
            logger.info(
                "label %s has %s new releases, paging in full instead",
                label_gid,
                len(fresh),
            )
        return None

    if not fresh and remote_total != count_cached_releases(cache_db, label_gid):
        # Counts disagree but the date window explains none of it — a release was
        # removed, or edited outside the window. Only a full read settles it.
        return None

    for release in fresh:
        # The search index gave us no streaming links; fetch each new release in
        # full so the cached row is as complete as a full sync would leave it.
        detailed = source.get_release(release["gid"], label_gid)
        upsert_releases(cache_db, label_gid, [detailed or release])
    cache_db.commit()

    local_total = count_cached_releases(cache_db, label_gid)
    if local_total != remote_total:
        if logger:
            logger.info(
                "incremental sync of %s left %s cached against %s remote; "
                "falling back to a full sync",
                label_gid,
                local_total,
                remote_total,
            )
        return None

    state = mark_sync_complete(
        cache_db,
        label_gid,
        remote_total,
        source.name,
        data_as_of=getattr(source, "data_as_of", None),
    )
    cache_db.commit()
    return state


def prune_label_links(cache_db, label_gid, seen_gids):
    """
    Drop links to releases the source no longer lists under this label.

    Releases get merged into one another, deleted outright, or moved to a
    different label upstream, and sync_label otherwise only ever adds and
    updates. Without this the local count drifts permanently above the remote
    one — switching from the stale mirror to the web service left Warp and
    Ghostly four releases over — and the nightly sweep reads any inequality as
    "out of date" and answers it with a full re-crawl, every night, for good.
    The page also keeps showing records the label no longer has.

    Only ever call this after a pass that covered the whole catalogue; see the
    caller. Returns the number of releases unlinked.
    """
    table = cache_db.mb_release_label
    stale = [
        row.release_gid
        for row in cache_db(table.label_gid == label_gid).select(
            table.release_gid, distinct=True
        )
        if row.release_gid not in seen_gids
    ]
    if stale:
        cache_db(
            (table.label_gid == label_gid) & (table.release_gid.belongs(stale))
        ).delete()
        cache_db.commit()
    return len(stale)


def get_label_row(cache_db, label_gid):
    """The cached label row, or None."""
    return cache_db(cache_db.mb_label.gid == label_gid).select().first()


def sync_label(source, cache_db, label_gid, page_size=100, on_page=None):
    """
    Pull a label and all its releases from `source` into the cache.

    Pages until the source's reported total is covered. On failure the sync
    state keeps whatever was written so a rerun resumes rather than restarts.

    Returns the final sync-state row.
    """
    label = source.get_label(label_gid)
    if label is None:
        return mark_sync_error(cache_db, label_gid, "label not found")

    upsert_label(cache_db, label)
    cache_db.commit()

    offset = 0
    total = None
    seen = set()
    try:
        while True:
            releases, total = source.browse_releases_by_label(
                label_gid, limit=page_size, offset=offset
            )
            if not releases:
                break

            upsert_releases(cache_db, label_gid, releases)
            seen.update(release["gid"] for release in releases)
            offset += len(releases)

            if offset >= total:
                cache_db.commit()
                break

            mark_sync_progress(cache_db, label_gid)
            cache_db.commit()
            if on_page:
                on_page(offset, total)

        # Only a pass that reached the end of the catalogue can tell a release
        # that is gone from one it simply has not got to yet. `seen` must be
        # non-empty too: a browse that 404s reports ([], 0), which is
        # indistinguishable here from a label that genuinely emptied, and
        # deleting a whole catalogue is far worse than leaving a stale link.
        if seen and offset >= total:
            prune_label_links(cache_db, label_gid, seen)

        state = mark_sync_complete(
            cache_db,
            label_gid,
            total or 0,
            source.name,
            data_as_of=getattr(source, "data_as_of", None),
        )
        cache_db.commit()
        return state
    except Exception as error:  # noqa: BLE001 - recorded, then re-raised
        cache_db.rollback()
        mark_sync_error(cache_db, label_gid, error)
        cache_db.commit()
        raise
