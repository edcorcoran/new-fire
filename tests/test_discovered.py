"""
Tests for musicbrainz/discovered.py.

What these protect is the reason the module exists: a sync rebuilds a release's
URL set wholesale, so a found link written into the cache survives only until
that label is next refreshed. The record lives in storage.db and the cache holds
a projection of it, and the tests that matter are the ones proving the
projection converges — it can be re-applied after any sync, any number of times,
without duplicating a row or resurrecting one that was withdrawn upstream.
"""

import datetime

from conftest import add, label, release, streaming

from musicbrainz.discovered import (
    apply_discovered_links,
    create_discovered_indexes,
    create_link_lookup_indexes,
    define_discovered_link_table,
    define_link_lookup_table,
    read_discovered_links,
    recently_checked,
    record_lookups,
)
from musicbrainz.reader import get_releases
from musicbrainz.writer import sync_label, upsert_label

APPLE = "https://music.apple.com/us/album/found/123"


def found(release_gid, url=APPLE, service="apple_music", rel_type="streaming"):
    """One discovered link, of the shape apply_discovered_links takes."""
    return dict(
        release_gid=release_gid, service=service, url=url, rel_type=rel_type
    )


def page(cache_db, label_gid="L1", filters=None, limit=20):
    """One page of a label's albums, as the label page would ask for it."""
    return get_releases(cache_db, label_gid, limit, 0, filters)


def streaming_for(cache_db, label_gid="L1"):
    """The {service: url} map the label page would render for its one album."""
    rows = page(cache_db, label_gid)
    return rows[0]["streaming"] if rows else {}


def test_projected_link_reaches_the_page(cache):
    upsert_label(cache, label())
    add(cache, "L1", release("R1"))

    assert apply_discovered_links(cache, [found("R1")]) == 1
    cache.commit()

    assert streaming_for(cache) == {"apple_music": APPLE}


def test_projection_is_idempotent(cache):
    upsert_label(cache, label())
    add(cache, "R1-label", release("R1"))

    assert apply_discovered_links(cache, [found("R1")]) == 1
    # Re-applying is the normal case: it runs after every sync of every label.
    assert apply_discovered_links(cache, [found("R1")]) == 0
    assert apply_discovered_links(cache, [found("R1")]) == 0
    cache.commit()

    rows = cache(cache.mb_release_url.release_gid == "R1").select()
    assert len(rows) == 1


def test_a_sync_wipes_the_projection_and_re_applying_restores_it(cache):
    """The whole reason these links are not simply written into the cache."""
    from conftest import FakeSource

    source = FakeSource(releases=[release("R1")])
    sync_label(source, cache, "L1")
    apply_discovered_links(cache, [found("R1")])
    cache.commit()
    assert streaming_for(cache) == {"apple_music": APPLE}

    # A refresh rewrites the release's URL set from the source, which has never
    # heard of this link.
    sync_label(source, cache, "L1")
    cache.commit()
    assert streaming_for(cache) == {}

    assert apply_discovered_links(cache, [found("R1")]) == 1
    cache.commit()
    assert streaming_for(cache) == {"apple_music": APPLE}


def test_link_musicbrainz_now_carries_does_not_double_the_row(cache):
    upsert_label(cache, label())
    # The source has caught up and now reports the same URL itself.
    add(cache, "L1", release("R1", urls=[streaming("apple_music", url=APPLE)]))

    assert apply_discovered_links(cache, [found("R1")]) == 0
    cache.commit()

    rows = cache(cache.mb_release_url.release_gid == "R1").select()
    assert len(rows) == 1
    assert streaming_for(cache) == {"apple_music": APPLE}


def test_link_for_an_uncached_release_is_skipped(cache):
    upsert_label(cache, label())
    add(cache, "L1", release("R1"))

    # Nothing in the cache is R-missing; a row for it would be an orphan that
    # maintenance's cleanup would collect anyway.
    assert apply_discovered_links(cache, [found("R-missing")]) == 0
    cache.commit()
    assert cache(cache.mb_release_url.release_gid == "R-missing").count() == 0


def test_link_to_an_unrenderable_service_is_skipped(cache):
    upsert_label(cache, label())
    add(cache, "L1", release("R1"))

    unknown = dict(
        release_gid="R1", service=None, url="https://example.com/album", rel_type=None
    )
    assert apply_discovered_links(cache, [unknown]) == 0
    cache.commit()
    assert streaming_for(cache) == {}


def test_the_same_link_twice_in_one_batch_inserts_once(cache):
    upsert_label(cache, label())
    add(cache, "L1", release("R1"))

    assert apply_discovered_links(cache, [found("R1"), found("R1")]) == 1
    cache.commit()
    assert cache(cache.mb_release_url.release_gid == "R1").count() == 1


def test_projected_link_satisfies_the_service_filter(cache):
    """
    The filter is SQL against mb_release_url, so a projected row has to satisfy
    it exactly as a MusicBrainz-sourced one does — otherwise a card shows an
    Apple Music link that filtering by Apple Music hides.
    """
    upsert_label(cache, label())
    add(cache, "L1", release("R1"), release("R2"))
    apply_discovered_links(cache, [found("R1")])
    cache.commit()

    filtered = page(cache, "L1", filters={"service": "apple_music"})
    assert [row["gid"] for row in filtered] == ["R1"]

    any_service = page(cache, "L1", filters={"service": "any"})
    assert [row["gid"] for row in any_service] == ["R1"]


def test_link_found_on_one_edition_shows_on_the_album(cache):
    """
    Editions collapse to one row, and the found link is attached to whichever
    edition the matcher happened to name — usually not the one representing the
    group. It still has to reach the card.
    """
    upsert_label(cache, label())
    add(
        cache,
        "L1",
        release("R1", group="G1", date="2024-01-01"),
        release("R2", group="G1", date="2024-06-01"),
    )
    apply_discovered_links(cache, [found("R2")])
    cache.commit()

    rows = page(cache, "L1")
    assert len(rows) == 1
    assert rows[0]["streaming"] == {"apple_music": APPLE}


def test_read_discovered_links_round_trips(tmp_path):
    """The storage.db side: what the importer writes is what the projector reads."""
    from py4web import DAL

    db = DAL("sqlite:memory", folder=str(tmp_path), migrate=True, pool_size=0)
    try:
        define_discovered_link_table(db)
        create_discovered_indexes(db)
        db.discovered_link.insert(
            release_gid="R1",
            service="apple_music",
            url=APPLE,
            rel_type="streaming",
            source="test",
        )
        db.commit()

        assert read_discovered_links(db) == [
            dict(
                release_gid="R1",
                service="apple_music",
                url=APPLE,
                rel_type="streaming",
            )
        ]
    finally:
        db.close()


# ------------------------------------------------- remembering what was searched


def _storage(tmp_path):
    """A throwaway storage.db with the two link tables."""
    from py4web import DAL

    db = DAL("sqlite:memory", folder=str(tmp_path), migrate=True, pool_size=0)
    define_discovered_link_table(db)
    create_discovered_indexes(db)
    define_link_lookup_table(db)
    create_link_lookup_indexes(db)
    return db


def test_a_miss_is_remembered_so_it_is_not_searched_again(tmp_path):
    """
    Most releases with no Apple Music link genuinely have no Apple album page.
    Without remembering that, the weekly sweep re-asks about every one of them
    forever and its cost grows with the back catalogue rather than with new
    releases.
    """
    db = _storage(tmp_path)
    try:
        record_lookups(db, "apple_music", [("R1", False), ("R2", True)])
        db.commit()

        assert recently_checked(db, "apple_music", within_days=180) == {"R1", "R2"}
    finally:
        db.close()


def test_a_check_expires_so_apple_gaining_the_record_is_noticed(tmp_path):
    """A miss is not permanent: Apple's catalogue gains records."""
    db = _storage(tmp_path)
    try:
        long_ago = datetime.datetime(2020, 1, 1)
        record_lookups(db, "apple_music", [("R1", False)], now=long_ago)
        db.commit()

        assert recently_checked(db, "apple_music", within_days=180) == set()
        assert recently_checked(db, "apple_music", within_days=100000) == {"R1"}
    finally:
        db.close()


def test_re_checking_updates_rather_than_duplicating(tmp_path):
    db = _storage(tmp_path)
    try:
        record_lookups(db, "apple_music", [("R1", False)])
        record_lookups(db, "apple_music", [("R1", True)])
        db.commit()

        rows = db(db.link_lookup.release_gid == "R1").select()
        assert len(rows) == 1
        assert rows[0].found is True
    finally:
        db.close()


def test_lookups_are_tracked_per_service(tmp_path):
    db = _storage(tmp_path)
    try:
        record_lookups(db, "apple_music", [("R1", False)])
        db.commit()

        assert recently_checked(db, "apple_music", within_days=180) == {"R1"}
        assert recently_checked(db, "spotify", within_days=180) == set()
    finally:
        db.close()
