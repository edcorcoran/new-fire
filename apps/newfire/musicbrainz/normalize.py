"""
The normalized record shapes every MusicBrainz source emits.

Both the local Postgres mirror and (later) the web service produce these exact
dicts, so the cache writer never has to know where a record came from. Keeping
the constructors here means the contract is defined in one place, and the two
sources can be diffed against each other in tests.

See docs/musicbrainz-cache-plan.md section 4.1.
"""

from urllib.parse import urlparse

# Streaming services the label page renders, keyed by hostname. A URL matches
# when its parsed host is one of these or a subdomain of one ("label
# .bandcamp.com"), never by substring — a path or query string containing
# "open.spotify.com" must not dress an arbitrary link up as Spotify.
STREAMING_SERVICES = (
    ("open.spotify.com", "spotify"),
    ("music.apple.com", "apple_music"),
    ("bandcamp.com", "bandcamp"),
)

# Relationship types that mean "you can listen to / buy this here", most
# listen-like first. These are the link_type names in Postgres and the "type"
# strings in the API, which are identical, so one tuple serves both sources.
#
# The order matters because one URL commonly carries several of these at once: a
# Bandcamp album is typically both "purchase for download" and "free streaming".
# The page shows a single icon per service, so the most listenable type wins.
LISTENABLE_REL_TYPES = (
    "free streaming",
    "streaming",
    "download for free",
    "purchase for download",
)

_REL_TYPE_RANK = {name: rank for rank, name in enumerate(LISTENABLE_REL_TYPES)}


def rel_type_rank(rel_type):
    """Sort key for relationship types; unknown types sort last."""
    return _REL_TYPE_RANK.get(rel_type, len(LISTENABLE_REL_TYPES))


def is_listenable(rel_type):
    return rel_type in _REL_TYPE_RANK


def dedupe_urls(urls):
    """
    Collapse a release's links to one entry per URL, keeping the best rel_type.

    MusicBrainz attaches the same URL under several relationship types, so
    without this a release ends up with duplicate rows that differ only in a
    field the page never shows.
    """
    best = {}
    for url in urls:
        current = best.get(url["url"])
        if current is None or rel_type_rank(url["rel_type"]) < rel_type_rank(
            current["rel_type"]
        ):
            best[url["url"]] = url
    return list(best.values())


def classify_url(url):
    """
    Return a service key for a URL, or None if it isn't one we display.

    Matches the parsed hostname rather than a substring, and only for http(s)
    URLs: these end up as hrefs on the label page styled as a known service, so
    the check has to say "this link goes to Spotify", not "this string mentions
    Spotify somewhere".
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    for known, service in STREAMING_SERVICES:
        if host == known or host.endswith("." + known):
            return service
    return None


def format_partial_date(year, month, day):
    """
    Build MusicBrainz's partial-date string from its three integer columns.

    Returns 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Stops at the first missing
    component so a row with a day but no month can't produce 'YYYY-DD'.
    Returns None when there is no year at all.

    This reproduces exactly what the web service puts in a release's "date"
    field; the two were verified equal across all 1,281 Drag City releases.
    """
    if not year:
        return None
    if not month:
        return "%04d" % year
    if not day:
        return "%04d-%02d" % (year, month)
    return "%04d-%02d-%02d" % (year, month, day)


def flatten_artist_credit(credits):
    """
    Collapse a web-service artist-credit array into its display string.

    The API returns credits as [{name, joinphrase, artist{...}}, ...]; joining
    name+joinphrase in order reproduces what Postgres stores pre-flattened in
    musicbrainz.artist_credit.name.
    """
    if not credits:
        return ""
    return "".join(
        (entry.get("name") or "") + (entry.get("joinphrase") or "") for entry in credits
    )


def make_url(service, url, rel_type=None):
    """One streaming/purchase link attached to a release."""
    return dict(service=service, url=url, rel_type=rel_type)


def make_label(
    gid,
    name,
    disambiguation="",
    label_type=None,
    area_name=None,
    label_code=None,
    sort_name=None,
):
    """
    One label.

    sort_name is web-service only: this Postgres schema has no label.sort_name
    column, so the mirror source leaves it None. Nothing renders it yet; it is
    carried so the API source doesn't have to discard data.
    """
    return dict(
        gid=str(gid),
        name=name,
        disambiguation=disambiguation or "",
        label_type=label_type,
        area_name=area_name,
        label_code=label_code,
        sort_name=sort_name,
    )


def make_release(
    gid,
    title,
    artist_credit="",
    artist_gid=None,
    release_group_gid=None,
    release_group_type=None,
    release_group_first_date=None,
    date=None,
    country=None,
    status=None,
    disambiguation="",
    has_front_cover=None,
    catalog_numbers=None,
    urls=None,
):
    """
    One release, as rendered by the label page.

    catalog_numbers is a list because MusicBrainz genuinely allows a release to
    carry several catalog numbers for the same label — usually formatting
    variants like 'DC-173-CD' alongside 'DC173CD'. Six of Drag City's 1,281
    releases do this. The web service reports them as repeated label-info
    entries, so a list is what both sources can honestly produce.

    has_front_cover is a tri-state: True/False when the source knows, None when
    it hasn't been determined. That keeps "no cover art" distinguishable from
    "not looked up yet".

    release_group_gid is what the pages group on: one record's CD, LP and
    digital editions are three releases sharing one release group. Sources that
    don't ask for it leave it None, and the readers then treat the release as a
    group of one rather than merging every ungrouped release together.
    """
    return dict(
        gid=str(gid),
        title=title,
        artist_credit=artist_credit or "",
        artist_gid=str(artist_gid) if artist_gid else None,
        release_group_gid=str(release_group_gid) if release_group_gid else None,
        release_group_type=release_group_type,
        release_group_first_date=release_group_first_date,
        date=date,
        country=country,
        status=status,
        disambiguation=disambiguation or "",
        has_front_cover=has_front_cover,
        catalog_numbers=list(catalog_numbers or []),
        urls=list(urls or []),
    )


def format_catalog_numbers(catalog_numbers):
    """Join a release's catalog numbers for display, or return None if it has none."""
    values = [value for value in (catalog_numbers or []) if value]
    return ", ".join(values) if values else None
