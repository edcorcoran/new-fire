"""
Tests for musicbrainz/reader.py.

Three rules carry this module, and most of what follows checks one of them:

1. A row is an album, not a release — the CD, LP and digital editions of one
   record collapse into a single card.
2. A row is dated by when *this label* first issued the record, so a repress
   does not resurface as news while a genuine reissue is flagged.
3. Filters describe the group, not the edition, which is why nearly all of
   them are HAVING clauses over the collapsed row.
"""

import datetime

from conftest import add, label, release, streaming

from musicbrainz.cache import SYNC_COMPLETE, SYNC_PARTIAL
from musicbrainz.reader import (
    count_recent_releases,
    count_release_groups,
    count_releases,
    get_recent_releases,
    get_releases,
    is_complete,
)
from musicbrainz.writer import upsert_label

TODAY = datetime.date.today()
LAST_YEAR = str(TODAY.year - 1) + "-01-01"
NEXT_YEAR = str(TODAY.year + 1) + "-01-01"


def album(gid_prefix, group, dates, **kwargs):
    """Several editions of one record, sharing a release group."""
    return [
        release(f"{gid_prefix}{n}", group=group, date=date, **kwargs)
        for n, date in enumerate(dates)
    ]


def only(cache, label_gid="L1", filters=None, limit=50):
    return get_releases(cache, label_gid, limit, 0, filters)


# ------------------------------------------------------- editions vs albums


def test_editions_collapse_into_one_card(cache):
    """
    Listing editions separately made a label page mostly repetition — one
    record occupied sixteen rows on Warp.
    """
    add(cache, "L1", *album("e", "G1", ["2020-01-01", "2020-03-01", "2020-06-01"]))

    rows = only(cache)
    assert len(rows) == 1
    assert rows[0]["editions"] == 3


def test_the_two_counts_answer_different_questions(cache):
    """
    count_releases feeds the sync machinery and must stay a release count;
    count_release_groups is what the page paginates over.
    """
    add(cache, "L1", *album("e", "G1", ["2020-01-01", "2020-03-01"]))
    add(cache, "L1", release("solo", date="2021-01-01"))

    assert count_releases(cache, "L1") == 3
    assert count_release_groups(cache, "L1") == 2


def test_an_ungrouped_release_is_its_own_album(cache):
    """
    Rows cached before release_group_gid existed have none. Without the
    fallback to the release's own gid they would all share a NULL key and
    collapse into one nonsensical row.
    """
    first, second = release("r1"), release("r2", date="2021-01-01")
    first["release_group_gid"] = None
    second["release_group_gid"] = None
    add(cache, "L1", first, second)

    assert count_release_groups(cache, "L1") == 2
    assert len(only(cache)) == 2


# ---------------------------------------------------------------- collapsing


def test_the_card_is_the_labels_earliest_edition(cache):
    """
    That is the moment the row reports: when this label put this record out.
    Dating by the newest pressing made every repress look like news.
    """
    add(
        cache,
        "L1",
        release("cd", group="G1", date="2020-01-01", title="The Record"),
        release("repress", group="G1", date="2024-01-01", title="The Record (2024)"),
    )

    row = only(cache)[0]
    assert row["date"] == "2020-01-01"
    assert row["title"] == "The Record"


def test_streaming_links_merge_across_editions(cache):
    """In practice the links live on the digital edition; the vinyl has none."""
    add(
        cache,
        "L1",
        release("vinyl", group="G1", date="2020-01-01"),
        release(
            "digital", group="G1", date="2020-02-01", urls=[streaming("spotify")]
        ),
    )

    assert "spotify" in only(cache)[0]["streaming"]


def test_cover_art_comes_from_whichever_edition_has_it(cache):
    add(
        cache,
        "L1",
        release("vinyl", group="G1", date="2020-01-01", has_front_cover=False),
        release("digital", group="G1", date="2020-02-01", has_front_cover=True),
    )

    assert only(cache)[0]["cover_art_url"].endswith("/release/digital/front-500")


def test_no_cover_art_url_when_no_edition_has_one(cache):
    """The archive 404s for about a third of releases; better to not ask."""
    add(cache, "L1", release("r1", has_front_cover=False))
    assert only(cache)[0]["cover_art_url"] is None


def test_an_undated_edition_never_represents_its_album(cache):
    """
    An edition with no date is not evidence the label put the record out
    earlier than the dated ones, so it must not become the card.
    """
    add(
        cache,
        "L1",
        release("undated", group="G1", date=None, title="Undated Pressing"),
        release("dated", group="G1", date="2020-01-01", title="The Record"),
    )

    row = only(cache)[0]
    assert row["gid"] == "dated"
    assert row["date"] == "2020-01-01"


def test_cover_art_breaks_a_tie_between_editions_of_the_same_date(cache):
    """An edition with art makes a visibly better row."""
    add(
        cache,
        "L1",
        release("plain", group="G1", date="2020-01-01", has_front_cover=False),
        release("with_art", group="G1", date="2020-01-01", has_front_cover=True),
    )

    assert only(cache)[0]["gid"] == "with_art"


def test_catalog_numbers_merge_across_editions(cache):
    add(
        cache,
        "L1",
        release("cd", group="G1", date="2020-01-01", catalog_numbers=["DC1CD"]),
        release("lp", group="G1", date="2020-02-01", catalog_numbers=["DC1LP"]),
    )

    assert only(cache)[0]["catalog_number"] == "DC1CD, DC1LP"


# ------------------------------------------------------------------ ordering


def test_albums_come_back_newest_first(cache):
    add(
        cache,
        "L1",
        release("old", date="2018-01-01"),
        release("new", date="2023-01-01"),
        release("middle", date="2020-01-01"),
    )

    assert [row["gid"] for row in only(cache)] == ["new", "middle", "old"]


def test_partial_dates_sort_chronologically(cache):
    """
    Dates are stored zero-padded as text precisely so this works: '2006-03'
    sorts between '2006' and '2006-04'.
    """
    add(
        cache,
        "L1",
        release("year_only", date="2006"),
        release("march", date="2006-03"),
        release("april", date="2006-04"),
    )

    assert [row["gid"] for row in only(cache)] == ["april", "march", "year_only"]


def test_undated_albums_sort_last(cache):
    """An undated release is old, not forthcoming."""
    add(cache, "L1", release("dated", date="2020-01-01"), release("undated", date=None))

    assert [row["gid"] for row in only(cache)] == ["dated", "undated"]


def test_a_repress_does_not_lift_an_old_record_back_to_the_top(cache):
    """The point of dating by the label's first edition rather than its last."""
    add(cache, "L1", *album("old", "G1", ["1995-01-01", "2024-06-01"]))
    add(cache, "L1", release("recent", group="G2", date="2020-01-01"))

    assert [row["gid"] for row in only(cache)] == ["recent", "old0"]


# ---------------------------------------------------------------- pagination


def test_pagination_walks_groups_not_editions(cache):
    """
    A page is twenty albums, not twenty editions of maybe six albums — and a
    group must never straddle a boundary and appear on both pages.
    """
    for n, year in enumerate(["2021", "2022", "2023"]):
        add(cache, "L1", *album(f"g{n}", f"G{n}", [f"{year}-01-01", f"{year}-06-01"]))

    first = get_releases(cache, "L1", 2, 0)
    second = get_releases(cache, "L1", 2, 2)

    assert len(first) == 2 and len(second) == 1
    assert not {row["gid"] for row in first} & {row["gid"] for row in second}


# ------------------------------------------------------------------ reissues


def test_a_record_reissued_years_later_is_flagged(cache):
    add(
        cache,
        "L1",
        release("r1", date="2015-01-01", group_first_date="1970-01-01"),
    )

    row = only(cache)[0]
    assert row["is_reissue"] is True
    assert row["original_date"] == "1970-01-01"


def test_a_record_issued_the_year_after_its_release_is_not_a_reissue(cache):
    """A record appearing on vinyl the following year is not a reissue."""
    add(cache, "L1", release("r1", date="2020-01-01", group_first_date="2019-06-01"))
    assert only(cache)[0]["is_reissue"] is False


def test_a_record_with_no_original_date_is_not_guessed_at(cache):
    add(cache, "L1", release("r1", date="2020-01-01", group_first_date=None))
    assert only(cache)[0]["is_reissue"] is False


# ------------------------------------------------------------------- filters


def test_type_filter_selects_a_format(cache):
    add(
        cache,
        "L1",
        release("lp", date="2020-01-01", group_type="Album"),
        release("single", date="2021-01-01", group_type="Single"),
    )

    assert [row["gid"] for row in only(cache, filters={"type": "single"})] == ["single"]


def test_other_type_filter_catches_comps_and_the_untyped(cache):
    add(
        cache,
        "L1",
        release("lp", date="2020-01-01", group_type="Album"),
        release("comp", date="2021-01-01", group_type="Compilation"),
        release("untyped", date="2022-01-01", group_type=None),
    )

    found = {row["gid"] for row in only(cache, filters={"type": "other"})}
    assert found == {"comp", "untyped"}


def test_reissue_filters_both_ways(cache):
    add(
        cache,
        "L1",
        release("new", date="2020-01-01", group_first_date="2020-01-01"),
        release("reissue", date="2021-01-01", group_first_date="1980-01-01"),
    )

    assert [r["gid"] for r in only(cache, filters={"reissue": "only"})] == ["reissue"]
    assert [r["gid"] for r in only(cache, filters={"reissue": "hide"})] == ["new"]


def test_status_filter_separates_out_now_from_not_out_yet(cache):
    add(
        cache,
        "L1",
        release("out", date=LAST_YEAR),
        release("upcoming", date=NEXT_YEAR),
    )

    assert [r["gid"] for r in only(cache, filters={"status": "upcoming"})] == [
        "upcoming"
    ]
    assert [r["gid"] for r in only(cache, filters={"status": "released"})] == ["out"]


def test_an_undated_record_counts_as_released(cache):
    """Undated means old, not announced."""
    add(cache, "L1", release("undated", date=None))

    assert len(only(cache, filters={"status": "released"})) == 1
    assert only(cache, filters={"status": "upcoming"}) == []


def test_service_filter_matches_any_edition_in_the_group(cache):
    """
    Filtering rows before grouping would ask a different question, and would
    drop the CD from a group whose digital edition carries the link.
    """
    add(
        cache,
        "L1",
        release("vinyl", group="G1", date="2020-01-01"),
        release("digital", group="G1", date="2020-02-01", urls=[streaming("spotify")]),
        release("silent", group="G2", date="2021-01-01"),
    )

    assert [r["gid"] for r in only(cache, filters={"service": "spotify"})] == ["vinyl"]
    assert [r["gid"] for r in only(cache, filters={"service": "any"})] == ["vinyl"]
    assert only(cache, filters={"service": "bandcamp"}) == []


def test_counts_respect_filters_so_the_last_page_is_never_empty(cache):
    add(
        cache,
        "L1",
        release("lp", date="2020-01-01", group_type="Album"),
        release("single", date="2021-01-01", group_type="Single"),
    )

    filters = {"type": "album"}
    assert count_release_groups(cache, "L1", filters) == 1
    assert count_release_groups(cache, "L1") == 2


# ---------------------------------------------------------------- the feed


def test_the_feed_credits_the_label_each_record_came_from(cache):
    upsert_label(cache, label(gid="L1", name="First Label"))
    upsert_label(cache, label(gid="L2", name="Second Label"))
    add(cache, "L1", release("r1", date="2020-01-01"))
    add(cache, "L2", release("r2", date="2021-01-01"))
    cache.commit()

    feed = get_recent_releases(cache, ["L1", "L2"])
    assert [row["label_name"] for row in feed] == ["Second Label", "First Label"]


def test_a_record_on_two_followed_labels_appears_once_per_label(cache):
    """
    The honest answer for a feed keyed on "why am I being shown this".
    """
    upsert_label(cache, label(gid="L1", name="First Label"))
    upsert_label(cache, label(gid="L2", name="Second Label"))
    add(cache, "L1", release("split", date="2020-01-01"))
    add(cache, "L2", release("split", date="2020-01-01"))
    cache.commit()

    feed = get_recent_releases(cache, ["L1", "L2"])
    assert len(feed) == 2
    assert {row["label_gid"] for row in feed} == {"L1", "L2"}
    assert count_recent_releases(cache, ["L1", "L2"]) == 2


def test_the_feed_merges_editions_only_within_one_label(cache):
    """
    Bucketing by (group, label) is what stops a feed row for one label
    absorbing editions the other label released.
    """
    upsert_label(cache, label(gid="L1"))
    upsert_label(cache, label(gid="L2"))
    add(cache, "L1", release("a", group="G1", date="2020-01-01"))
    add(cache, "L2", release("b", group="G1", date="2021-01-01"))
    cache.commit()

    feed = get_recent_releases(cache, ["L1", "L2"])
    assert [(row["label_gid"], row["editions"]) for row in feed] == [
        ("L2", 1),
        ("L1", 1),
    ]


def test_the_feed_can_be_narrowed_to_one_label(cache):
    upsert_label(cache, label(gid="L1"))
    upsert_label(cache, label(gid="L2"))
    add(cache, "L1", release("r1", date="2020-01-01"))
    add(cache, "L2", release("r2", date="2021-01-01"))
    cache.commit()

    feed = get_recent_releases(cache, ["L1", "L2"], filters={"label": "L1"})
    assert [row["gid"] for row in feed] == ["r1"]
    assert count_recent_releases(cache, ["L1", "L2"], {"label": "L1"}) == 1


def test_the_feed_paginates(cache):
    upsert_label(cache, label(gid="L1"))
    add(cache, "L1", *[release(f"r{n}", date=f"20{n:02d}-01-01") for n in range(5)])
    cache.commit()

    assert len(get_recent_releases(cache, ["L1"], limit=2)) == 2
    assert len(get_recent_releases(cache, ["L1"], limit=2, offset=4)) == 1


def test_following_nothing_costs_no_query(cache):
    assert get_recent_releases(cache, []) == []
    assert count_recent_releases(cache, []) == 0


# ------------------------------------------------------------------ is_complete


def test_only_a_complete_sync_may_be_paginated_by_date(cache):
    """
    Release order is undefined until a label is fully cached, so a partial
    cache sorted newest-first shows records from the wrong end of history.
    """
    assert is_complete(None) is False

    from musicbrainz.writer import update_sync_state

    assert is_complete(update_sync_state(cache, "L1", status=SYNC_PARTIAL)) is False
    assert is_complete(update_sync_state(cache, "L1", status=SYNC_COMPLETE)) is True
