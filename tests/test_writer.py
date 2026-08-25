"""
Tests for musicbrainz/writer.py.

The writer's contract is idempotence: syncs are interrupted routinely — the
scheduler forks children that can be killed, and a large label is dozens of
requests during which the API can 503 — so rerunning a partial sync has to
converge rather than duplicate. Most of what follows is that property, stated
one behaviour at a time.
"""

import datetime

import pytest
from conftest import BrokenSource, FakeSource, add, label, release, streaming

from musicbrainz.cache import SYNC_COMPLETE, SYNC_ERROR, SYNC_PARTIAL
from musicbrainz.writer import (
    count_cached_releases,
    get_sync_state,
    mark_sync_complete,
    mark_sync_error,
    prune_label_links,
    sync_label,
    sync_label_incremental,
    upsert_label,
    upsert_releases,
)

JANUARY = datetime.datetime(2020, 1, 1)
LATER = datetime.datetime(2021, 6, 1)


# ------------------------------------------------------------------- labels


def test_upsert_label_inserts_then_updates_in_place(cache):
    """A label fetched twice is one row, carrying the newer values."""
    upsert_label(cache, label(name="Old Name"))
    upsert_label(cache, label(name="New Name", area_name="United Kingdom"))
    cache.commit()

    rows = cache(cache.mb_label.gid == "L1").select()
    assert len(rows) == 1
    assert rows.first().name == "New Name"
    assert rows.first().area_name == "United Kingdom"


def test_upsert_label_returns_the_row_id(cache):
    first = upsert_label(cache, label())
    second = upsert_label(cache, label(name="Renamed"))
    assert first == second


# ----------------------------------------------------------------- releases


def test_upsert_releases_reports_inserts_and_updates(cache):
    inserted, updated = upsert_releases(cache, "L1", [release("r1"), release("r2")])
    assert (inserted, updated) == (2, 0)

    inserted, updated = upsert_releases(
        cache, "L1", [release("r1", title="Retitled"), release("r2")]
    )
    assert (inserted, updated) == (0, 1)


def test_unchanged_release_keeps_its_fetched_at(cache):
    """
    Rewriting a row that did not move would make fetched_at meaningless — it
    is the only record of how current the cached copy is.
    """
    upsert_releases(cache, "L1", [release("r1")], now=JANUARY)
    upsert_releases(cache, "L1", [release("r1")], now=LATER)
    cache.commit()

    assert cache(cache.mb_release.gid == "r1").select().first().fetched_at == JANUARY


def test_changed_release_advances_its_fetched_at(cache):
    upsert_releases(cache, "L1", [release("r1")], now=JANUARY)
    upsert_releases(cache, "L1", [release("r1", title="Retitled")], now=LATER)
    cache.commit()

    row = cache(cache.mb_release.gid == "r1").select().first()
    assert row.fetched_at == LATER
    assert row.title == "Retitled"


def test_a_release_repeated_within_one_batch_collapses(cache):
    """
    A duplicate inside one batch would hit the unique index mid-insert and
    abort the whole page. Sources are allowed to be sloppier than ours.
    """
    inserted, _ = upsert_releases(
        cache, "L1", [release("r1"), release("r1", title="Same record")]
    )
    assert inserted == 1
    assert cache(cache.mb_release.gid == "r1").count() == 1


def test_rerunning_a_sync_does_not_duplicate_anything(cache):
    """The whole point: an interrupted sync rerun converges."""
    batch = [release("r1", catalog_numbers=["DC1"], urls=[streaming()]), release("r2")]
    add(cache, "L1", *batch)
    add(cache, "L1", *batch)

    assert cache(cache.mb_release).count() == 2
    assert cache(cache.mb_release_label).count() == 2
    assert cache(cache.mb_release_url).count() == 1


# ------------------------------------------------------------ label linkage


def test_count_counts_releases_not_catalog_rows(cache):
    """
    A release carrying two catalog numbers occupies two linkage rows but is
    one release. Counting rows instead would inflate the local count past the
    remote one and leave a complete label looking permanently out of sync.
    """
    add(cache, "L1", release("r1", catalog_numbers=["DC-173-CD", "DC173CD"]))

    assert cache(cache.mb_release_label.label_gid == "L1").count() == 2
    assert count_cached_releases(cache, "L1") == 1


def test_a_release_with_no_catalog_number_still_gets_linked(cache):
    add(cache, "L1", release("r1"))
    row = cache(cache.mb_release_label.release_gid == "r1").select().first()
    assert row.label_gid == "L1"
    assert row.catalog_number is None


def test_a_catalog_number_removed_upstream_is_dropped(cache):
    """Convergence again: stale variants must not accumulate across resyncs."""
    add(cache, "L1", release("r1", catalog_numbers=["OLD1", "OLD2"]))
    add(cache, "L1", release("r1", catalog_numbers=["OLD1"]))

    numbers = {
        row.catalog_number
        for row in cache(cache.mb_release_label.release_gid == "r1").select()
    }
    assert numbers == {"OLD1"}


def test_the_same_release_can_be_linked_to_two_labels(cache):
    add(cache, "L1", release("r1"))
    add(cache, "L2", release("r1"))

    assert count_cached_releases(cache, "L1") == 1
    assert count_cached_releases(cache, "L2") == 1


# --------------------------------------------------------------------- urls


def test_urls_are_replaced_wholesale(cache):
    add(cache, "L1", release("r1", urls=[streaming("spotify")]))
    add(cache, "L1", release("r1", urls=[streaming("bandcamp")]))

    services = {
        row.service for row in cache(cache.mb_release_url.release_gid == "r1").select()
    }
    assert services == {"bandcamp"}


def test_a_release_with_no_urls_key_leaves_cached_links_alone(cache):
    """
    `urls=None` means "not looked up", which is what the search endpoint
    returns. Treating it as "has no links" would let an incremental sync wipe
    good data.
    """
    add(cache, "L1", release("r1", urls=[streaming("spotify")]))

    from_search = release("r1")
    from_search["urls"] = None
    add(cache, "L1", from_search)

    assert cache(cache.mb_release_url.release_gid == "r1").count() == 1


def test_an_empty_url_list_does_clear_the_links(cache):
    """An empty list is knowledge — the release was fetched and has none."""
    add(cache, "L1", release("r1", urls=[streaming("spotify")]))
    add(cache, "L1", release("r1", urls=[]))

    assert cache(cache.mb_release_url.release_gid == "r1").count() == 0


def test_one_url_carrying_several_rel_types_is_stored_once(cache):
    """A Bandcamp album is typically both a purchase and a free stream."""
    url = "https://label.bandcamp.com/album/x"
    add(
        cache,
        "L1",
        release(
            "r1",
            urls=[
                dict(service="bandcamp", url=url, rel_type="purchase for download"),
                dict(service="bandcamp", url=url, rel_type="free streaming"),
            ],
        ),
    )

    rows = cache(cache.mb_release_url.release_gid == "r1").select()
    assert len(rows) == 1
    # The most listenable type wins, since the page shows one icon per service.
    assert rows.first().rel_type == "free streaming"


# --------------------------------------------------------------- sync state


def test_sync_state_records_completion(cache):
    add(cache, "L1", release("r1"))
    state = mark_sync_complete(cache, "L1", total=1, source="fake")

    assert state.status == SYNC_COMPLETE
    assert state.release_count_remote == 1
    assert state.release_count_local == 1
    assert state.error_count == 0


def test_completion_stamps_the_data_date_not_the_clock(cache):
    """
    A source that lags must not mark a label fresh. Conflating the two let 65
    labels seeded from a mirror five months behind report a sync today, which
    every staleness check downstream believed.
    """
    stale_mirror_date = datetime.datetime(2020, 3, 1)
    state = mark_sync_complete(
        cache, "L1", total=0, source="postgres", data_as_of=stale_mirror_date
    )

    assert state.last_full_sync_at == stale_mirror_date
    # The check itself did happen now, so that stays wall-clock.
    assert state.last_checked_at > stale_mirror_date


def test_an_error_with_releases_cached_is_partial_not_error(cache):
    """A resume should be an ordinary continuation, not a fresh start."""
    add(cache, "L1", release("r1"))
    state = mark_sync_error(cache, "L1", "boom")

    assert state.status == SYNC_PARTIAL
    assert state.release_count_local == 1


def test_an_error_with_nothing_cached_is_an_error(cache):
    state = mark_sync_error(cache, "L1", "boom")
    assert state.status == SYNC_ERROR


def test_errors_accumulate_a_count(cache):
    mark_sync_error(cache, "L1", "first")
    state = mark_sync_error(cache, "L1", "second")

    assert state.error_count == 2
    assert state.error_message == "second"


def test_a_long_error_message_is_truncated(cache):
    state = mark_sync_error(cache, "L1", "x" * 2000)
    assert len(state.error_message) == 500


# ------------------------------------------------------------------ pruning


def test_prune_drops_links_the_source_no_longer_lists(cache):
    """
    Releases get merged, deleted, or moved to another label upstream. Without
    pruning the local count drifts permanently above the remote one, and the
    nightly sweep reads that as "out of date" forever.
    """
    add(cache, "L1", release("r1"), release("r2"), release("r3"))
    removed = prune_label_links(cache, "L1", seen_gids={"r1", "r2"})

    assert removed == 1
    assert count_cached_releases(cache, "L1") == 2


def test_prune_leaves_other_labels_alone(cache):
    add(cache, "L1", release("r1"))
    add(cache, "L2", release("r1"))
    prune_label_links(cache, "L1", seen_gids=set())

    assert count_cached_releases(cache, "L1") == 0
    assert count_cached_releases(cache, "L2") == 1


# --------------------------------------------------------------- sync_label


def test_sync_label_pages_through_the_whole_catalogue(cache):
    source = FakeSource(releases=[release(f"r{n}") for n in range(250)])
    state = sync_label(source, cache, "L1", page_size=100)

    assert state.status == SYNC_COMPLETE
    assert count_cached_releases(cache, "L1") == 250
    assert source.browse_calls == [(100, 0), (100, 100), (100, 200)]


def test_sync_label_prunes_after_a_complete_pass(cache):
    add(cache, "L1", release("gone"))
    source = FakeSource(releases=[release("r1")])
    sync_label(source, cache, "L1")

    assert count_cached_releases(cache, "L1") == 1
    assert cache(cache.mb_release_label.release_gid == "gone").count() == 0


def test_sync_label_does_not_prune_after_a_short_pass(cache):
    """
    The guard that matters most here. A pass that did not reach the end of the
    catalogue cannot tell a release that is gone from one it simply has not
    got to yet, and deleting a whole catalogue is far worse than leaving a
    stale link.
    """
    add(cache, "L1", release("existing"))
    # Claims 500 releases, hands over 1: what a truncated catalogue looks like.
    source = FakeSource(releases=[release("r1")], total=500)
    sync_label(source, cache, "L1")

    assert cache(cache.mb_release_label.release_gid == "existing").count() == 1


def test_an_empty_catalogue_does_not_delete_everything(cache):
    """
    A browse that 404s reports ([], 0), indistinguishable here from a label
    that genuinely emptied. `seen` being empty is what stops the deletion.
    """
    add(cache, "L1", release("existing"))
    sync_label(FakeSource(releases=[]), cache, "L1")

    assert cache(cache.mb_release_label.release_gid == "existing").count() == 1


def test_sync_label_records_a_missing_label(cache):
    state = sync_label(FakeSource(label_record=None), cache, "L1")

    assert state.status == SYNC_ERROR
    assert "not found" in state.error_message


def test_a_failed_sync_records_state_and_re_raises(cache):
    """
    The state has to be accurate before the exception leaves, because the
    scheduler's retry is what reads it.
    """
    source = BrokenSource(releases=[release(f"r{n}") for n in range(150)], fail_after=1)

    with pytest.raises(RuntimeError):
        sync_label(source, cache, "L1", page_size=100)

    state = get_sync_state(cache, "L1")
    assert state.status == SYNC_PARTIAL
    assert state.release_count_local == 100


def test_a_resumed_sync_completes(cache):
    """A partial sync followed by a good one leaves no trace of the failure."""
    releases = [release(f"r{n}") for n in range(150)]
    with pytest.raises(RuntimeError):
        sync_label(
            BrokenSource(releases=releases, fail_after=1), cache, "L1", page_size=100
        )

    state = sync_label(FakeSource(releases=releases), cache, "L1", page_size=100)

    assert state.status == SYNC_COMPLETE
    assert state.error_count == 0
    assert state.error_message is None
    assert count_cached_releases(cache, "L1") == 150


# --------------------------------------------------------- incremental sync


def test_incremental_sync_adds_only_what_is_new(cache):
    """
    The cheap path: one request to list recent releases, then one per genuinely
    new release, against thirteen for a full re-read of a large label.
    """
    old, new = release("r1", date="2019-01-01"), release("r2", date="2024-01-01")
    upsert_label(cache, label())
    add(cache, "L1", old)

    source = FakeSource(releases=[old, new])
    state = sync_label_incremental(source, cache, "L1", since_year=2023)

    assert state.status == SYNC_COMPLETE
    assert count_cached_releases(cache, "L1") == 2
    # Only the new release was fetched in full for its streaming links.
    assert source.get_release_calls == ["r2"]


def test_incremental_sync_declines_when_there_is_too_much_new(cache):
    """Past max_new, paging the label in full is cheaper anyway."""
    upsert_label(cache, label())
    releases = [release(f"r{n}", date="2024-01-01") for n in range(30)]
    source = FakeSource(releases=releases)

    assert sync_label_incremental(source, cache, "L1", 2023, max_new=25) is None


def test_incremental_sync_declines_when_the_counts_still_disagree(cache):
    """
    A release was removed, or edited outside the date window. Claiming success
    would leave the cache permanently wrong, so it defers to a full sync.
    """
    upsert_label(cache, label())
    add(cache, "L1", release("r1", date="2019-01-01"))
    # Source says five releases exist but offers nothing dated recently.
    source = FakeSource(releases=[release("r1", date="2019-01-01")], total=5)

    assert sync_label_incremental(source, cache, "L1", 2023) is None


def test_incremental_sync_declines_for_an_uncached_label(cache):
    """Without the label's name there is nothing to search the index with."""
    assert sync_label_incremental(FakeSource(), cache, "unknown", 2023) is None
