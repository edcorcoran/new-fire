"""
Reads the cache for display.

Everything here returns plain dicts shaped for the templates rather than DAL
rows, so the template never reaches back into the database and the page's data
requirements stay visible in one place.

Two rules this module enforces:

1. A label may only be paginated by date once its sync is COMPLETE. Release
   order is undefined until then — the web service returns releases roughly
   oldest-added first, so a partial cache sorted newest-first shows records from
   the wrong end of the label's history. See docs/musicbrainz-cache-plan.md
   section 4.3.

2. Editions are collapsed to one row per release group. MusicBrainz models the
   CD, the LP and the digital release of one record as three releases sharing a
   release group; listing them separately made a label page mostly repetition —
   Aphex Twin's "Richard D. James Album" occupies sixteen rows on Warp. See
   _group_releases for what a collapsed row is made of.

3. A row is dated by when *this label* first issued the record, not by its
   newest pressing. That is what makes a reissue legible. A label pressing its
   own album again does not move the row, so it stops resurfacing as news; a
   label putting an out-of-print record back in print gets a row dated when it
   did so, flagged as a reissue against the work's original release date. The
   difference matters more than it sounds: reissues are 2% of Warp's catalogue
   and 43% of Light in the Attic's.
"""

import datetime

from .cache import SYNC_COMPLETE
from .normalize import format_catalog_numbers

COVER_ART_BASE = "https://coverartarchive.org"
MUSICBRAINZ_BASE = "https://musicbrainz.org"

# How far a label's edition must trail the work's first release before the row
# is called a reissue. Two years is comfortably clear of the ordinary case of a
# record appearing in another territory, or on vinyl, the year after its
# release, which nobody would call a reissue.
REISSUE_GAP_YEARS = 2

# The group a release belongs to, falling back to the release's own id. The
# fallback matters for rows cached before release_group_gid existed and for the
# handful of releases MusicBrainz leaves ungrouped: without it every such
# release would share the NULL key and collapse into one nonsensical row.
_GROUP_KEY = "COALESCE(r.release_group_gid, r.gid)"


# ------------------------------------------------------------------ filters

# Filter values the pages accept. Kept as plain strings because they travel in
# query parameters and belong in bookmarks and shared links.
TYPE_ALBUM, TYPE_EP, TYPE_SINGLE, TYPE_OTHER = "album", "ep", "single", "other"
_PRIMARY_TYPES = {TYPE_ALBUM: "Album", TYPE_EP: "EP", TYPE_SINGLE: "Single"}

REISSUE_HIDE, REISSUE_ONLY = "hide", "only"
STATUS_RELEASED, STATUS_UPCOMING = "released", "upcoming"
SERVICE_ANY = "any"


def _filter_sql(filters, today=None):
    """
    Translate display filters into WHERE and HAVING fragments.

    Almost everything here is a HAVING rather than a WHERE, because what the
    pages filter on describes the *group* and the group is an aggregate over its
    editions: a record is a reissue because the earliest edition this label
    issued trails the work's first release, and it is on Spotify if *any*
    edition carries the link. Filtering the rows before grouping would ask a
    different question — "show me editions that are on Spotify" — and would drop
    the CD from a group whose digital edition has the link.

    The exception is the label restriction, which genuinely narrows which rows
    form the groups.

    Returns (where_parts, where_params, having_parts, having_params).
    """
    filters = filters or {}
    today = today or datetime.date.today().isoformat()
    where, wparams, having, hparams = [], [], [], []

    if filters.get("label"):
        where.append("rl.label_gid = ?")
        wparams.append(filters["label"])

    kind = filters.get("type")
    if kind in _PRIMARY_TYPES:
        having.append("MAX(COALESCE(r.release_group_type, '')) = ?")
        hparams.append(_PRIMARY_TYPES[kind])
    elif kind == TYPE_OTHER:
        # Compilations, live albums, soundtracks, and the untyped remainder.
        having.append(
            "MAX(COALESCE(r.release_group_type, '')) NOT IN ('Album', 'EP', 'Single')"
        )

    reissue = filters.get("reissue")
    if reissue in (REISSUE_HIDE, REISSUE_ONLY):
        # Same year-level test as _is_reissue, expressed in SQL so it can be
        # paginated. Both dates must be known for the question to mean anything.
        test = (
            "MIN(r.release_group_first_date) IS NOT NULL AND MIN(r.date) IS NOT NULL "
            f"AND CAST(substr(MIN(r.date), 1, 4) AS INTEGER) "
            f"- CAST(substr(MIN(r.release_group_first_date), 1, 4) AS INTEGER) "
            f">= {REISSUE_GAP_YEARS}"
        )
        having.append(f"({test})" if reissue == REISSUE_ONLY else f"NOT ({test})")

    status = filters.get("status")
    if status == STATUS_UPCOMING:
        having.append("COALESCE(MIN(r.date), '') > ?")
        hparams.append(today)
    elif status == STATUS_RELEASED:
        # Undated records count as out: an undated release is old, not announced.
        having.append("COALESCE(MIN(r.date), '') <= ?")
        hparams.append(today)

    service = filters.get("service")
    if service:
        listenable = (
            "MAX(CASE WHEN EXISTS (SELECT 1 FROM mb_release_url u "
            "WHERE u.release_gid = r.gid{extra}) THEN 1 ELSE 0 END) = 1"
        )
        if service == SERVICE_ANY:
            having.append(listenable.format(extra=""))
        else:
            having.append(listenable.format(extra=" AND u.service = ?"))
            hparams.append(service)

    if filters.get("since"):
        having.append("COALESCE(MIN(r.date), '') >= ?")
        hparams.append(filters["since"])

    return where, wparams, having, hparams


def _clause(keyword, parts):
    return f"{keyword} " + " AND ".join(parts) if parts else ""


def get_label(cache_db, label_gid):
    """The cached label, or None if it has never been fetched."""
    return cache_db(cache_db.mb_label.gid == label_gid).select().first()


def is_complete(state):
    """True when a label's releases are fully cached and safe to date-sort."""
    return bool(state) and state.status == SYNC_COMPLETE


def count_releases(cache_db, label_gid):
    """
    Distinct releases cached for a label — every edition counted separately.

    This is the number the sync machinery compares against the source's
    release-count, so it must stay a release count and not become an album
    count. Pages that show "how many records has this label put out" want
    count_release_groups instead.
    """
    return cache_db(cache_db.mb_release_label.label_gid == label_gid).count(
        distinct=cache_db.mb_release_label.release_gid
    )


def count_release_groups(cache_db, label_gid, filters=None):
    """Distinct albums cached for a label — what the label page paginates over."""
    where, wparams, having, hparams = _filter_sql(filters)
    sql = f"""
        SELECT COUNT(*) FROM (
          SELECT {_GROUP_KEY} AS grp
          FROM mb_release_label rl
          JOIN mb_release r ON r.gid = rl.release_gid
          WHERE rl.label_gid = ? {_clause('AND', where)}
          GROUP BY grp
          {_clause('HAVING', having)}
        )
    """
    rows = cache_db.executesql(sql, tuple([label_gid] + wparams + hparams))
    return rows[0][0] if rows else 0


def get_releases(cache_db, label_gid, limit, offset, filters=None):
    """
    One page of a label's albums, newest first, one row per release group.

    Paginates over groups rather than releases, so a page is twenty albums
    rather than twenty editions of maybe six albums. The page's groups are
    chosen first and their editions fetched second; doing it the other way round
    would let a group straddle a page boundary and appear on both.

    Ordered by the earliest edition this label released, so a repress does not
    lift an old record back to the top of its own label page.

    Partial dates sort correctly as text because they are stored zero-padded:
    '2006-03' sorts between '2006' and '2006-04'. MIN ignores NULLs and yields
    NULL only when every edition is undated, which COALESCE turns into the empty
    string — below any real date, so descending order puts undated groups last
    without a special case.
    """
    where, wparams, having, hparams = _filter_sql(filters)
    sql = f"""
        SELECT {_GROUP_KEY} AS grp, COALESCE(MIN(r.date), '') AS first_here
        FROM mb_release_label rl
        JOIN mb_release r ON r.gid = rl.release_gid
        WHERE rl.label_gid = ? {_clause('AND', where)}
        GROUP BY grp
        {_clause('HAVING', having)}
        ORDER BY first_here DESC, MIN(COALESCE(r.title, '')) ASC
        LIMIT ? OFFSET ?
    """
    keys = [
        row[0]
        for row in cache_db.executesql(
            sql, tuple([label_gid] + wparams + hparams + [limit, offset])
        )
    ]
    if not keys:
        return []

    releases = cache_db.mb_release
    links = cache_db.mb_release_label
    rows = cache_db(
        (links.label_gid == label_gid)
        & (releases.gid == links.release_gid)
        & (releases.release_group_gid.belongs(keys) | releases.gid.belongs(keys))
    ).select(releases.ALL, distinct=True)

    catalogs = _catalog_numbers(cache_db, label_gid, [row.gid for row in rows])
    return _group_releases(cache_db, rows, keys, catalogs_by_release=catalogs)


def count_recent_releases(cache_db, label_gids, filters=None):
    """How many albums the tracked-labels feed holds, for pagination."""
    label_gids = list(label_gids)
    if not label_gids:
        return 0
    where, wparams, having, hparams = _filter_sql(filters)
    placeholders = ",".join(["?"] * len(label_gids))
    sql = f"""
        SELECT COUNT(*) FROM (
          SELECT {_GROUP_KEY} AS grp, rl.label_gid
          FROM mb_release_label rl
          JOIN mb_release r ON r.gid = rl.release_gid
          WHERE rl.label_gid IN ({placeholders}) {_clause('AND', where)}
          GROUP BY grp, rl.label_gid
          {_clause('HAVING', having)}
        )
    """
    rows = cache_db.executesql(sql, tuple(label_gids + wparams + hparams))
    return rows[0][0] if rows else 0


def get_recent_releases(cache_db, label_gids, limit=50, offset=0, filters=None):
    """
    Newest albums across several labels — the tracked-labels feed.

    Each row carries the label it came from, since the point of the feed is
    seeing what a followed label just put out. An album released on two followed
    labels appears once per label, which is the honest answer for a feed keyed
    on "why am I being shown this".

    Dated by when each label first issued the record, so the feed answers "what
    has this label put out that it had not before" rather than "what has it
    pressed most recently".
    """
    label_gids = list(label_gids)
    if not label_gids:
        return []

    where, wparams, having, hparams = _filter_sql(filters)
    placeholders = ",".join(["?"] * len(label_gids))
    sql = f"""
        SELECT {_GROUP_KEY} AS grp, rl.label_gid AS label_gid,
               COALESCE(MIN(r.date), '') AS first_here
        FROM mb_release_label rl
        JOIN mb_release r ON r.gid = rl.release_gid
        WHERE rl.label_gid IN ({placeholders}) {_clause('AND', where)}
        GROUP BY grp, rl.label_gid
        {_clause('HAVING', having)}
        ORDER BY first_here DESC, MIN(COALESCE(r.title, '')) ASC
        LIMIT ? OFFSET ?
    """
    pairs = cache_db.executesql(
        sql, tuple(label_gids + wparams + hparams + [limit, offset])
    )
    if not pairs:
        return []

    labels = {
        row.gid: row
        for row in cache_db(
            cache_db.mb_label.gid.belongs({label_gid for _, label_gid, _ in pairs})
        ).select()
    }

    releases = cache_db.mb_release
    links = cache_db.mb_release_label
    keys = {grp for grp, _, _ in pairs}
    rows = cache_db(
        (links.label_gid.belongs(label_gids))
        & (releases.gid == links.release_gid)
        & (releases.release_group_gid.belongs(keys) | releases.gid.belongs(keys))
    ).select(releases.ALL, links.label_gid, distinct=True)

    # Bucket the editions by (group, label) so each feed row only ever merges
    # editions actually released on the label that row is about.
    by_pair = {}
    for row in rows:
        key = (_key_of(row.mb_release), row.mb_release_label.label_gid)
        by_pair.setdefault(key, []).append(row.mb_release)

    feed = []
    for grp, label_gid, _ in pairs:
        editions = by_pair.get((grp, label_gid))
        if not editions:
            continue
        entry = _collapse(cache_db, editions)
        label = labels.get(label_gid)
        entry["label_gid"] = label_gid
        entry["label_name"] = label.name if label else label_gid
        feed.append(entry)
    return feed


def _key_of(release):
    """The group key for one cached release row."""
    return release.release_group_gid or release.gid


def _group_releases(cache_db, rows, keys, catalogs_by_release=None):
    """Collapse release rows into one entry per group, in `keys` order."""
    grouped = {}
    for row in rows:
        grouped.setdefault(_key_of(row), []).append(row)

    return [
        _collapse(cache_db, grouped[key], catalogs_by_release)
        for key in keys
        if key in grouped
    ]


def _collapse(cache_db, editions, catalogs_by_release=None):
    """
    Turn one release group's editions into a single row.

    The row is the label's *first* edition — its title, its date, its
    MusicBrainz page — because that is the moment the row is reporting: when
    this label put this record out. Dating by the newest pressing instead made
    every repress look like news and made a genuine reissue claim to be a new
    album.

    What the other editions contribute is everything the first one might be
    missing: cover art, and streaming links, which in practice live on the
    digital edition while the vinyl carries none.
    """
    editions = sorted(editions, key=_edition_sort_key)
    first = editions[0]
    gids = [edition.gid for edition in editions]

    streaming = _streaming_links(cache_db, gids)
    merged_streaming = {}
    # The edition the row represents wins any service the others also carry.
    for edition in editions:
        for service, url in streaming.get(edition.gid, {}).items():
            merged_streaming.setdefault(service, url)

    cover_gid = next(
        (edition.gid for edition in editions if edition.has_front_cover), None
    )

    catalogs = []
    for edition in editions:
        for number in (catalogs_by_release or {}).get(edition.gid, []) or []:
            if number not in catalogs:
                catalogs.append(number)

    original = next(
        (e.release_group_first_date for e in editions if e.release_group_first_date),
        None,
    )
    return dict(
        gid=first.gid,
        release_group_gid=first.release_group_gid,
        release_group_type=first.release_group_type,
        title=first.title,
        artist_credit=first.artist_credit,
        artist_gid=first.artist_gid,
        date=first.date,
        country=first.country,
        status=first.status,
        disambiguation=first.disambiguation,
        editions=len(editions),
        original_date=original,
        is_reissue=_is_reissue(original, first.date),
        catalog_number=format_catalog_numbers(catalogs),
        streaming=merged_streaming,
        musicbrainz_url=f"{MUSICBRAINZ_BASE}/release/{first.gid}",
        artist_url=(
            f"{MUSICBRAINZ_BASE}/artist/{first.artist_gid}"
            if first.artist_gid
            else None
        ),
        # Only offer an image when the cache knows one exists. The archive 404s
        # for the rest, and roughly a third of releases have no front cover.
        cover_art_url=(
            f"{COVER_ART_BASE}/release/{cover_gid}/front-500" if cover_gid else None
        ),
    )


def _is_reissue(original_date, label_date):
    """
    Whether this label's edition trails the work's first release enough to say so.

    Compares years only. MusicBrainz partial dates are frequently just a year,
    and a month-level comparison would call a January reissue of a December
    record a reissue by eleven months and miss one by thirteen.
    """
    original_year, label_year = _year(original_date), _year(label_date)
    if original_year is None or label_year is None:
        return False
    return label_year - original_year >= REISSUE_GAP_YEARS


def _year(partial_date):
    """The year from a 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' string, or None."""
    if not partial_date:
        return None
    try:
        return int(str(partial_date)[:4])
    except ValueError:
        return None


def _edition_sort_key(release):
    """
    Rank editions so the label's earliest, most complete one represents the group.

    Date first, since that is what the row displays. Undated editions sort last
    rather than first — an edition with no date is not evidence the label put
    the record out earlier than the ones that are dated. Cover art then breaks
    ties, because an edition with art makes a visibly better row, and the gid
    breaks the rest so the choice is stable between requests rather than
    depending on the order SQLite happened to return.
    """
    return (
        release.date or "9999",
        0 if release.has_front_cover else 1,
        release.gid,
    )


def _catalog_numbers(cache_db, label_gid, release_gids):
    """Map release gid -> list of catalog numbers under this label."""
    links = cache_db.mb_release_label
    rows = cache_db(
        (links.label_gid == label_gid) & (links.release_gid.belongs(release_gids))
    ).select(orderby=links.catalog_number)

    catalogs = {}
    for row in rows:
        if row.catalog_number:
            catalogs.setdefault(row.release_gid, []).append(row.catalog_number)
    return catalogs


def _streaming_links(cache_db, release_gids):
    """Map release gid -> {service: url}."""
    urls = cache_db.mb_release_url
    rows = cache_db(urls.release_gid.belongs(release_gids)).select()

    links = {}
    for row in rows:
        links.setdefault(row.release_gid, {})[row.service] = row.url
    return links
