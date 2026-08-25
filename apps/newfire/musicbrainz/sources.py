"""
Where MusicBrainz data comes from.

MBSource is the seam that lets the same cache-filling code run against either
the local Postgres mirror (fast, offline, used for development and for seeding a
production cache) or the web service (authoritative, rate limited). This module
provides the interface and the mirror; the web service implementation lives in
webservice.py.

See docs/musicbrainz-cache-plan.md section 4.1.
"""

from .normalize import (
    classify_url,
    dedupe_urls,
    format_partial_date,
    is_listenable,
    make_label,
    make_release,
    make_url,
)


class MBSource:
    """
    Interface for a MusicBrainz data source.

    Implementations return the normalized dicts built by normalize.make_*, never
    raw rows or raw JSON.
    """

    name = "abstract"

    @property
    def data_as_of(self):
        """
        How current this source's data is, or None when it is live.

        The writer stamps a label's last_full_sync_at with this rather than the
        wall clock, so "when was this label last synced" means "how old is the
        data" and not "when did the machine last run". Without it a sync from a
        source that lags marks the label fresh, and every staleness check
        downstream believes it — see PostgresMirrorSource.
        """
        return None

    def get_label(self, gid):
        """Return one normalized label dict, or None if unknown."""
        raise NotImplementedError

    def browse_releases_by_label(self, gid, limit=100, offset=0):
        """
        Return (releases, total) for a label.

        `total` is the label's full release count, independent of limit/offset,
        so callers can page and can detect drift against a cached count.
        """
        raise NotImplementedError

    def search_labels(self, query, limit=25):
        """Return normalized label dicts matching a free-text query."""
        raise NotImplementedError

    def get_release(self, gid, label_gid=None):
        """
        One release in full, including its streaming links.

        Used to fill in releases discovered by a date-filtered search, which
        does not carry URL relationships. label_gid selects which label's
        catalog numbers to report.
        """
        raise NotImplementedError

    def find_releases_since(self, label_gid, label_name, since_year, limit=100):
        """
        Releases on a label dated `since_year` or later, cheaply.

        The point is to spot what a label has just put out without re-reading its
        whole catalogue. Results may omit streaming links, so callers finish the
        job with get_release.
        """
        raise NotImplementedError


# Paging happens here, over DISTINCT releases rather than release_label rows: a
# release can be listed more than once under one label (different catalog-number
# formattings), so paging the join table would hand out duplicate releases and
# desynchronise LIMIT/OFFSET from the release count the web service reports.
#
# Resolving ids in their own cheap, index-only query is what keeps the detail
# query below fast. Putting LIMIT/OFFSET on the detail query instead makes
# Postgres run every LATERAL for all 1,281 Drag City releases and then throw away
# all but the requested page -- measured at 21s for page 1 and 284s for page 13.
_RELEASE_ID_PAGE = """
SELECT DISTINCT rl.release AS release_id
FROM musicbrainz.release_label rl
JOIN musicbrainz.label lab ON lab.id = rl.label
WHERE lab.gid = %s
ORDER BY rl.release
LIMIT %s OFFSET %s
"""

# Detail query for one already-resolved page of release ids.
#
# The earliest release event is found with a LATERAL join, not a CTE. The
# obvious formulation --
#
#     WITH earliest_country AS (
#         SELECT DISTINCT ON (release) ... FROM musicbrainz.release_country
#         ORDER BY release, date_year NULLS LAST, ...)
#
# -- is uncorrelated with the label filter, so Postgres sorts and deduplicates
# the whole multi-million-row release_country table before returning a single
# page. The LATERAL asks the same question per release, hits the index on
# release_country.release, and runs in milliseconds. Its date semantics are
# identical: earliest dated event, then release_unknown_country as a fallback.
_RELEASE_SELECT = """
SELECT r.id                                     AS release_id,
       r.gid                                    AS gid,
       r.name                                   AS title,
       r.comment                                AS disambiguation,
       ac.name                                  AS artist_credit,
       primary_artist.gid                       AS artist_gid,
       rg.gid                                   AS release_group_gid,
       rgpt.name                                AS release_group_type,
       rgm.first_release_date_year              AS rg_year,
       rgm.first_release_date_month             AS rg_month,
       rgm.first_release_date_day               AS rg_day,
       rs.name                                  AS status,
       iso.code                                 AS country,
       COALESCE(ec.date_year,  ruc.date_year)   AS date_year,
       COALESCE(ec.date_month, ruc.date_month)  AS date_month,
       COALESCE(ec.date_day,   ruc.date_day)    AS date_day
FROM musicbrainz.release r
JOIN musicbrainz.artist_credit ac       ON ac.id = r.artist_credit
LEFT JOIN musicbrainz.release_group rg  ON rg.id = r.release_group
LEFT JOIN musicbrainz.release_group_primary_type rgpt ON rgpt.id = rg.type
LEFT JOIN musicbrainz.release_group_meta rgm ON rgm.id = rg.id
LEFT JOIN musicbrainz.release_status rs ON rs.id = r.status
LEFT JOIN musicbrainz.release_unknown_country ruc ON ruc.release = r.id
LEFT JOIN LATERAL (
    SELECT rc.date_year, rc.date_month, rc.date_day
    FROM musicbrainz.release_country rc
    WHERE rc.release = r.id
    ORDER BY rc.date_year NULLS LAST, rc.date_month NULLS LAST, rc.date_day NULLS LAST
    LIMIT 1
) ec ON TRUE
LEFT JOIN LATERAL (
    SELECT a.gid
    FROM musicbrainz.artist_credit_name acn
    JOIN musicbrainz.artist a ON a.id = acn.artist
    WHERE acn.artist_credit = ac.id
    ORDER BY acn.position
    LIMIT 1
) primary_artist ON TRUE
LEFT JOIN LATERAL (
    SELECT ia.code
    FROM musicbrainz.release_country rc2
    JOIN musicbrainz.iso_3166_1 ia ON ia.area = rc2.country
    WHERE rc2.release = r.id
    ORDER BY rc2.date_year NULLS LAST, rc2.date_month NULLS LAST, rc2.date_day NULLS LAST
    LIMIT 1
) iso ON TRUE
WHERE {filter}
ORDER BY r.gid
"""

_CATALOG_SELECT = """
SELECT rl.release AS release_id, rl.catalog_number AS catalog_number
FROM musicbrainz.release_label rl
JOIN musicbrainz.label lab ON lab.id = rl.label
WHERE lab.gid = %%s AND rl.release IN (%s)
ORDER BY rl.catalog_number
"""

_URL_SELECT = """
SELECT lru.entity0 AS release_id, u.url AS url, lt.name AS rel_type
FROM musicbrainz.l_release_url lru
JOIN musicbrainz.url u        ON u.id = lru.entity1
JOIN musicbrainz.link l       ON l.id = lru.link
JOIN musicbrainz.link_type lt ON lt.id = l.link_type
WHERE lru.entity0 IN (%s)
"""

# Queries the cover_art base tables rather than the cover_art_archive.
# index_listing view. The view is both slower and, in a mirror, wrong: its
# `approved` flag is not populated, so filtering on it reports zero front covers
# for every release. art_type id 1 is 'Front'.
_COVER_ART_SELECT = """
SELECT DISTINCT ca.release AS release_id
FROM cover_art_archive.cover_art ca
JOIN cover_art_archive.cover_art_type cat ON cat.id = ca.id
WHERE cat.type_id = 1 AND ca.release IN (%s)
"""


# Distinguishes "not looked up yet" from "looked up, and there is no date".
_UNSET = object()


def _release_query_by_id(release_ids):
    """Detail query filtered on a set of internal release ids."""
    placeholders = ",".join(["%s"] * len(release_ids))
    return _RELEASE_SELECT.format(filter=f"r.id IN ({placeholders})")


class PostgresMirrorSource(MBSource):
    """
    Reads a local MusicBrainz Postgres mirror.

    No rate limit, so this is what development and bulk cache seeding run
    against. Note the mirror lags the live service (it was 16 releases behind on
    Drag City when measured), so it seeds a cache rather than replacing one.
    """

    name = "postgres"

    def __init__(self, mb_dal):
        self.mb = mb_dal
        self._replicated_at = _UNSET

    @property
    def data_as_of(self):
        """
        When this mirror last replicated, from its own replication_control row.

        A mirror is only as current as its last replication packet, and one that
        has stopped replicating says nothing about it — this one sat five months
        behind while every sync it fed reported success. Stamping that date on
        the labels it fills is what lets an ordinary TTL check notice.

        Looked up once and remembered: it cannot change under a running process
        in any way that matters, and this is read once per label sync.
        """
        if self._replicated_at is _UNSET:
            self._replicated_at = self._read_replication_date()
        return self._replicated_at

    def _read_replication_date(self):
        try:
            rows = self.mb.executesql(
                "SELECT last_replication_date FROM musicbrainz.replication_control"
            )
        except Exception:  # noqa: BLE001 - a mirror without it is not fatal
            return None
        if not rows or rows[0][0] is None:
            return None
        value = rows[0][0]
        # The cache stores naive UTC; the mirror hands back an aware datetime.
        return value.replace(tzinfo=None) if value.tzinfo else value

    def get_label(self, gid):
        rows = self.mb.executesql(
            """
            SELECT l.gid, l.name, l.comment, l.label_code,
                   lt.name AS label_type, a.name AS area_name
            FROM musicbrainz.label l
            LEFT JOIN musicbrainz.label_type lt ON lt.id = l.type
            LEFT JOIN musicbrainz.area a        ON a.id = l.area
            WHERE l.gid = %s
            """,
            (str(gid),),
            as_dict=True,
        )
        if not rows:
            return None
        row = rows[0]
        return make_label(
            gid=row["gid"],
            name=row["name"],
            disambiguation=row["comment"],
            label_type=row["label_type"],
            area_name=row["area_name"],
            label_code=row["label_code"],
        )

    def count_releases_by_label(self, gid):
        """
        Distinct releases for a label — the mirror's answer to a count-check.

        Counts DISTINCT releases rather than release_label rows so the number is
        directly comparable with the web service's "release-count". Drag City
        has 1,287 release_label rows but 1,281 releases.
        """
        rows = self.mb.executesql(
            """
            SELECT count(DISTINCT rl.release) FROM musicbrainz.release_label rl
            JOIN musicbrainz.label l ON l.id = rl.label
            WHERE l.gid = %s
            """,
            (str(gid),),
        )
        return rows[0][0] if rows else 0

    def browse_releases_by_label(self, gid, limit=100, offset=0):
        total = self.count_releases_by_label(gid)
        if not total:
            return [], 0

        id_rows = self.mb.executesql(
            _RELEASE_ID_PAGE, (str(gid), limit, offset)
        )
        release_ids = [row[0] for row in id_rows]
        if not release_ids:
            return [], total

        rows = self.mb.executesql(
            _release_query_by_id(release_ids), tuple(release_ids), as_dict=True
        )
        if not rows:
            return [], total

        return self._build_releases(rows, gid), total

    def _build_releases(self, rows, label_gid):
        """
        Turn detail rows into normalized releases.

        The three related lookups — streaming links, catalog numbers, cover art
        — are each one batched query for the whole set rather than per release.
        """
        release_ids = [row["release_id"] for row in rows]
        urls_by_release = self._fetch_urls(release_ids)
        catalogs_by_release = (
            self._fetch_catalog_numbers(label_gid, release_ids) if label_gid else {}
        )
        with_front_cover = self._fetch_front_cover_ids(release_ids)

        return [
            make_release(
                gid=row["gid"],
                title=row["title"],
                artist_credit=row["artist_credit"],
                artist_gid=row["artist_gid"],
                release_group_gid=row["release_group_gid"],
                release_group_type=row["release_group_type"],
                release_group_first_date=format_partial_date(
                    row["rg_year"], row["rg_month"], row["rg_day"]
                ),
                date=format_partial_date(
                    row["date_year"], row["date_month"], row["date_day"]
                ),
                country=row["country"],
                status=row["status"],
                disambiguation=row["disambiguation"],
                has_front_cover=row["release_id"] in with_front_cover,
                catalog_numbers=catalogs_by_release.get(row["release_id"], []),
                urls=urls_by_release.get(row["release_id"], []),
            )
            for row in rows
        ]

    def get_release(self, gid, label_gid=None):
        """One release in full. See MBSource.get_release."""
        rows = self.mb.executesql(
            _RELEASE_SELECT.format(filter="r.gid = %s"), (str(gid),), as_dict=True
        )
        if not rows:
            return None
        return self._build_releases(rows, label_gid)[0]

    def find_releases_since(self, label_gid, label_name, since_year, limit=100):
        """
        Releases on a label dated since a given year.

        The mirror can filter by label MBID directly, which the web service
        cannot — its release index has no searchable label id, so that
        implementation has to match on name and filter afterwards.
        """
        rows = self.mb.executesql(
            """
            SELECT DISTINCT rl.release AS release_id
            FROM musicbrainz.release_label rl
            JOIN musicbrainz.label lab ON lab.id = rl.label
            LEFT JOIN musicbrainz.release_country rc ON rc.release = rl.release
            LEFT JOIN musicbrainz.release_unknown_country ruc
                   ON ruc.release = rl.release
            WHERE lab.gid = %s
              AND COALESCE(rc.date_year, ruc.date_year) >= %s
            LIMIT %s
            """,
            (str(label_gid), int(since_year), limit),
        )
        release_ids = [row[0] for row in rows]
        if not release_ids:
            return []

        detail = self.mb.executesql(
            _release_query_by_id(release_ids), tuple(release_ids), as_dict=True
        )
        return self._build_releases(detail, label_gid)

    def _fetch_front_cover_ids(self, release_ids):
        """Set of internal release ids that have front cover art."""
        if not release_ids:
            return set()
        placeholders = ",".join(["%s"] * len(release_ids))
        rows = self.mb.executesql(
            _COVER_ART_SELECT % placeholders, tuple(release_ids)
        )
        return {row[0] for row in rows}

    def _fetch_catalog_numbers(self, label_gid, release_ids):
        """Map internal release id -> list of catalog numbers under this label."""
        if not release_ids:
            return {}
        placeholders = ",".join(["%s"] * len(release_ids))
        rows = self.mb.executesql(
            _CATALOG_SELECT % placeholders,
            (str(label_gid),) + tuple(release_ids),
            as_dict=True,
        )
        catalogs = {}
        for row in rows:
            if row["catalog_number"]:
                catalogs.setdefault(row["release_id"], []).append(
                    row["catalog_number"]
                )
        return catalogs

    def _fetch_urls(self, release_ids):
        """Map internal release id -> list of normalized url dicts."""
        if not release_ids:
            return {}
        placeholders = ",".join(["%s"] * len(release_ids))
        rows = self.mb.executesql(
            _URL_SELECT % placeholders, tuple(release_ids), as_dict=True
        )
        urls = {}
        for row in rows:
            if not is_listenable(row["rel_type"]):
                continue
            service = classify_url(row["url"])
            if not service:
                continue
            urls.setdefault(row["release_id"], []).append(
                make_url(service, row["url"], row["rel_type"])
            )
        return {
            release_id: dedupe_urls(found) for release_id, found in urls.items()
        }

    def search_labels(self, query, limit=25):
        """
        Substring search over label names.

        This is deliberately not equivalent to the web service's Lucene index —
        it exists so development doesn't need network access. Production search
        goes through the web service (see the plan, section 4.7).
        """
        if not query or not query.strip():
            return []
        rows = self.mb.executesql(
            """
            SELECT l.gid, l.name, l.comment, l.label_code,
                   lt.name AS label_type, a.name AS area_name,
                   (SELECT count(*) FROM musicbrainz.release_label rl
                    WHERE rl.label = l.id) AS release_count
            FROM musicbrainz.label l
            LEFT JOIN musicbrainz.label_type lt ON lt.id = l.type
            LEFT JOIN musicbrainz.area a        ON a.id = l.area
            WHERE l.name ILIKE %s
            ORDER BY release_count DESC
            LIMIT %s
            """,
            ("%" + query.strip() + "%", limit),
            as_dict=True,
        )
        return [
            make_label(
                gid=row["gid"],
                name=row["name"],
                disambiguation=row["comment"],
                label_type=row["label_type"],
                area_name=row["area_name"],
                label_code=row["label_code"],
            )
            for row in rows
        ]
