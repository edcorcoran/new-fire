"""
Shared fixtures for the cache tests.

The cache layer is the part of this app that can be tested without booting
py4web: everything under musicbrainz/ reaches the database through a plain
pydal DAL, and define_cache_tables can point that schema at an in-memory
SQLite database. So each test builds a throwaway cache, fills it through the
real writer, and reads it back through the real reader — no HTTP, no network,
no app import.

Tests that need a source use FakeSource rather than either real one. The point
of the MBSource seam is that the writer cannot tell implementations apart, so
a fake exercising the same contract is the honest way to test paging, pruning
and failure handling without a mirror or a rate limit.
"""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "newfire"),
)

from musicbrainz.cache import connect_cache  # noqa: E402
from musicbrainz.normalize import make_label, make_release  # noqa: E402
from musicbrainz.writer import upsert_releases  # noqa: E402


@pytest.fixture
def cache(tmp_path):
    """A fresh, empty cache with its indexes, thrown away after each test."""
    cache_db = connect_cache(
        "sqlite:memory", folder=str(tmp_path), migrate=True, pool_size=0
    )
    yield cache_db
    cache_db.close()


def label(gid="L1", name="Test Label", **kwargs):
    """A normalized label dict, as either source would emit."""
    return make_label(gid=gid, name=name, **kwargs)


def release(
    gid,
    title="A Record",
    date="2020-01-01",
    group=None,
    group_type="Album",
    group_first_date=None,
    artist="An Artist",
    catalog_numbers=None,
    urls=(),
    has_front_cover=None,
):
    """
    A normalized release dict.

    `group` defaults to the release's own gid so that a release is its own
    release group unless a test deliberately shares one — otherwise every
    release in a test would collapse into a single card.
    """
    return make_release(
        gid=gid,
        title=title,
        artist_credit=artist,
        release_group_gid=group or gid,
        release_group_type=group_type,
        release_group_first_date=group_first_date,
        date=date,
        has_front_cover=has_front_cover,
        catalog_numbers=catalog_numbers,
        urls=list(urls),
    )


def streaming(service="spotify", url=None):
    """One streaming link, of the shape normalize.make_url produces."""
    hosts = {
        "spotify": "https://open.spotify.com/album/x",
        "apple_music": "https://music.apple.com/us/album/x",
        "bandcamp": "https://label.bandcamp.com/album/x",
    }
    return dict(
        service=service, url=url or hosts[service], rel_type="free streaming"
    )


def add(cache_db, label_gid, *releases, now=None):
    """Write releases into the cache under a label, the way a sync would."""
    upsert_releases(cache_db, label_gid, list(releases), now=now)
    cache_db.commit()


# Lets a test pass label_record=None to mean "this label does not exist",
# which is distinct from not specifying one at all.
_UNSET = object()


class FakeSource:
    """
    An MBSource that serves a fixed catalogue.

    Records the calls it received so tests can assert on request *count* — the
    thing the whole cache exists to keep small — rather than only on results.
    """

    name = "fake"

    def __init__(self, label_record=_UNSET, releases=(), total=None, data_as_of=None):
        self.label_record = label() if label_record is _UNSET else label_record
        self.releases = list(releases)
        # Lets a test claim more releases exist than it will hand over, which
        # is what an interrupted or drifting catalogue looks like.
        self._total = total
        self.data_as_of = data_as_of
        self.browse_calls = []
        self.get_release_calls = []

    @property
    def total(self):
        return len(self.releases) if self._total is None else self._total

    def get_label(self, gid):
        return self.label_record

    def count_releases_by_label(self, gid):
        return self.total

    def browse_releases_by_label(self, gid, limit=100, offset=0):
        self.browse_calls.append((limit, offset))
        return self.releases[offset : offset + limit], self.total

    def get_release(self, gid, label_gid=None):
        self.get_release_calls.append(gid)
        for candidate in self.releases:
            if candidate["gid"] == gid:
                return candidate
        return None

    def find_releases_since(self, label_gid, label_name, since_year, limit=100):
        return [
            candidate
            for candidate in self.releases
            if (candidate.get("date") or "") >= str(since_year)
        ]


class BrokenSource(FakeSource):
    """A source that fails partway through paging, as a 503 storm would."""

    def __init__(self, *args, fail_after=1, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_after = fail_after

    def browse_releases_by_label(self, gid, limit=100, offset=0):
        if len(self.browse_calls) >= self.fail_after:
            raise RuntimeError("musicbrainz is having a moment")
        return super().browse_releases_by_label(gid, limit=limit, offset=offset)
