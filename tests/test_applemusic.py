"""
Tests for musicbrainz/applemusic.py.

The matcher runs unattended and writes what it finds straight onto pages, so
what these protect is its conservatism. Nearly every case below is one the first
real run over a 302-label cache actually produced -- the accepted ones because
they were correct and a stricter matcher would have thrown them away, and the
rejected ones because they were wrong and a looser matcher would have kept them.

No network: the API client is exercised through a fake, and everything else here
is a pure function over dicts shaped like Apple's search results.
"""

from conftest import add, label, release, streaming

from musicbrainz.applemusic import (
    MATCHABLE_TYPES,
    TIER_CONFIDENT,
    TIER_MISS,
    TIER_REVIEW,
    artists_match,
    candidates_needing_links,
    clean_url,
    find_links,
    normalize,
    pick_match,
    years_agree,
)
from musicbrainz.writer import upsert_label


def apple(name, artist, date="2024-05-01", url="https://music.apple.com/us/album/x/1?uo=4"):
    """One result, shaped as the iTunes Search API returns it."""
    return dict(
        collectionName=name,
        artistName=artist,
        releaseDate=date,
        collectionViewUrl=url,
        trackCount=10,
    )


def ours(title, artist="An Artist", date="2024-05-01", gid="R1"):
    return dict(release_gid=gid, title=title, artist_credit=artist, date=date)


# ----------------------------------------------------------------- normalizing


def test_apple_format_suffix_is_not_a_difference():
    """Apple names an EP "Foo - EP"; comparing raw titles rejects real matches."""
    assert normalize("The Loft Sessions") == normalize("The Loft Sessions - EP")
    assert normalize("Raw") == normalize("Raw - Single")


def test_case_and_accents_are_not_differences():
    assert normalize("Big Rigs on the BQE") == normalize("Big Rigs on the Bqe")
    assert normalize("Miroslav Vitouš") == normalize("Miroslav Vitous")
    assert normalize("Gleann Ciùin") == normalize("Gleann Ciuin")


def test_punctuation_is_dropped_but_words_are_not():
    assert normalize("(It (Is) It) Critical Band") == normalize("It Is It Critical Band")
    assert normalize("Star Trek: Starfleet Academy") == normalize("Star Trek Starfleet Academy")
    # "&" is punctuation and "and" is a word, so this pair does *not* collapse.
    # That is deliberate: it lands in the review tier rather than being accepted
    # unattended, which is what test_near_miss_on_punctuation_is_review_not_confident
    # pins down.
    assert normalize("Preludes & Songs") != normalize("Preludes and Songs")


def test_artist_containment_matches_either_way():
    """Collaborations are credited differently by the two catalogues."""
    assert artists_match("Peyton", "Peyton & Shafiq Husayn")
    assert artists_match("Amnesia Scanner & Freeka Tet", "Amnesia Scanner")
    assert artists_match("Miroslav Vitouš", "Miroslav Vitous, Michel Portal")
    assert not artists_match("Preservation Hall Jazz Band", "Preservation Brass")
    assert not artists_match("", "Someone")


def test_years_agree_abstains_when_a_date_is_unknown():
    """MusicBrainz dates are often absent; that is not evidence against a match."""
    assert years_agree(None, "2024-01-01")
    assert years_agree("2024", None)
    assert years_agree("2024-01-01", "2025-12-01")   # a year apart is ordinary
    assert not years_agree("2025-01-01", "2008-06-01")


# --------------------------------------------------------------------- tiering


def test_exact_title_and_artist_and_year_is_confident():
    tier, result = pick_match(
        ours("Below the Waste", "Goat Girl"),
        [apple("Below the Waste", "Goat Girl", "2024-06-07")],
    )
    assert tier == TIER_CONFIDENT
    assert result["collectionName"] == "Below the Waste"


def test_format_suffix_still_matches_confidently():
    tier, _ = pick_match(
        ours("Butterfly", "Thornhill"), [apple("Butterfly - EP", "Thornhill")]
    )
    assert tier == TIER_CONFIDENT


def test_same_title_wrong_era_is_demoted_to_review():
    """
    The first row of the first real run: a 2025 record matching a same-titled
    2008 one. Without the year check this would have been written to a page.
    """
    tier, _ = pick_match(
        ours("Shaking Moving Dancing People", "Babytalk & Watussi", date="2025-04-01"),
        [apple("Shaking Moving Dancing People", "Babytalk & Watussi", "2008-03-01")],
    )
    assert tier == TIER_REVIEW


def test_same_title_different_artist_is_not_confident():
    """Two MusicBrainz albums matched one Apple album; one of them was wrong."""
    tier, _ = pick_match(
        ours("For Fat Man", "Preservation Brass"),
        [apple("For Fat Man", "Preservation Hall Jazz Band")],
    )
    assert tier == TIER_REVIEW


def test_near_miss_on_punctuation_is_review_not_confident():
    tier, result = pick_match(
        ours("Preludes and Songs", "A Pianist"),
        [apple("Preludes & Songs", "A Pianist")],
    )
    assert tier == TIER_REVIEW
    assert result is not None


def test_unrelated_result_is_a_miss():
    tier, result = pick_match(
        ours("Tractor Beam", "Snail Mail"),
        [apple("Ricochet", "Snail Mail"), apple("Valentine", "Snail Mail")],
    )
    assert tier == TIER_MISS
    assert result is None


def test_confident_match_wins_over_an_earlier_near_miss():
    """Apple ranks by its own relevance; the exact match is not reliably first."""
    tier, result = pick_match(
        ours("Luster", "Maria Somerville", date="2025-02-01"),
        [
            apple("Luster", "Maria Somerville", "1998-01-01"),   # right name, wrong era
            apple("Luster", "Maria Somerville", "2025-02-21"),   # the real one
        ],
    )
    assert tier == TIER_CONFIDENT
    assert result["releaseDate"] == "2025-02-21"


def test_no_results_is_a_miss():
    assert pick_match(ours("Anything"), []) == (TIER_MISS, None)


def test_tracking_parameters_are_stripped():
    assert clean_url("https://music.apple.com/us/album/x/1?uo=4") == (
        "https://music.apple.com/us/album/x/1"
    )
    assert clean_url(None) == ""


# ------------------------------------------------------------------ candidates


def test_singles_are_never_candidates(cache):
    """
    The expensive lesson from the first run: most releases missing an Apple link
    are advance singles that Apple carries only as a track on the parent album,
    so there is nothing to find and searching produces confident-looking noise.
    """
    upsert_label(cache, label())
    add(
        cache,
        "L1",
        release("R1", title="An Album", group="G1", group_type="Album", date="2025-01-01"),
        release("R2", title="A Single", group="G2", group_type="Single", date="2025-01-01"),
    )

    found = candidates_needing_links(cache, "apple_music", 2024, types=MATCHABLE_TYPES)
    assert [row["title"] for row in found] == ["An Album"]


def test_releases_that_already_have_the_link_are_skipped(cache):
    upsert_label(cache, label())
    add(
        cache,
        "L1",
        release("R1", title="Linked", group="G1", date="2025-01-01",
                urls=[streaming("apple_music")]),
        release("R2", title="Unlinked", group="G2", date="2025-01-01"),
    )

    found = candidates_needing_links(cache, "apple_music", 2024)
    assert [row["title"] for row in found] == ["Unlinked"]


def test_a_link_on_any_edition_covers_the_whole_group(cache):
    """The group is what gets rendered, so one edition's link satisfies it."""
    upsert_label(cache, label())
    add(
        cache,
        "L1",
        release("R1", title="Record", group="G1", date="2025-01-01"),
        release("R2", title="Record", group="G1", date="2025-02-01",
                urls=[streaming("apple_music")]),
    )

    assert candidates_needing_links(cache, "apple_music", 2024) == []


def test_the_representative_edition_is_the_earliest(cache):
    """Deterministic, so reruns do not scatter links across editions."""
    upsert_label(cache, label())
    add(
        cache,
        "L1",
        release("R2", title="Record", group="G1", date="2025-06-01"),
        release("R1", title="Record", group="G1", date="2025-01-01"),
    )

    found = candidates_needing_links(cache, "apple_music", 2024)
    assert [row["release_gid"] for row in found] == ["R1"]


def test_older_releases_are_out_of_scope(cache):
    upsert_label(cache, label())
    add(
        cache,
        "L1",
        release("R1", title="Recent", group="G1", date="2025-01-01"),
        release("R2", title="Ancient", group="G2", date="1996-01-01"),
    )

    found = candidates_needing_links(cache, "apple_music", 2024)
    assert [row["title"] for row in found] == ["Recent"]


# ---------------------------------------------------------------- the sweep


class FakeSearch:
    """Returns canned results per search term, and counts what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.terms = []

    def search_albums(self, term, limit=10, country="US"):
        self.terms.append(term)
        for needle, results in self.answers.items():
            if needle.lower() in term.lower():
                return results
        return []


def test_find_links_keeps_only_confident_matches():
    client = FakeSearch(
        {
            "Below the Waste": [apple("Below the Waste", "Goat Girl", "2024-06-07")],
            "Piedras": [apple("Piedras 1", "Someone", "2024-01-01")],
            "Nothing Here": [],
        }
    )
    candidates = [
        ours("Below the Waste", "Goat Girl", "2024-06-01", gid="R1"),
        ours("Piedras 1 & 2", "Someone", "2024-01-01", gid="R2"),
        ours("Nothing Here", "Nobody", "2024-01-01", gid="R3"),
    ]

    links, reviews, checked = find_links(client, candidates)

    assert [link["release_gid"] for link in links] == ["R1"]
    assert links[0]["service"] == "apple_music"
    assert links[0]["rel_type"] == "streaming"
    assert "?uo=4" not in links[0]["url"]

    assert [row["release_gid"] for row in reviews] == ["R2"]

    # Every candidate is recorded as searched, hit or miss, so the sweep does
    # not ask about the same fruitless one every week forever.
    assert checked == [("R1", True), ("R2", False), ("R3", False)]


def test_find_links_searches_artist_and_title_together():
    client = FakeSearch({})
    find_links(client, [ours("Acadia", "Yasmin Williams")])
    assert client.terms == ["Yasmin Williams Acadia"]
