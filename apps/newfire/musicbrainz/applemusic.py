"""
Finding Apple Music links MusicBrainz does not carry.

MusicBrainz describes a great many records well and has no streaming link for
them. Apple's iTunes Search API is public, unauthenticated and free, so the gap
can be closed by asking it for the artist and title MusicBrainz already knows.

Three things learned from doing this over a real 302-label cache shape the code,
and each is a rule rather than a preference:

  - **Albums and EPs only.** Most releases missing an Apple link are advance
    singles, which MusicBrainz models as standalone releases and Apple carries
    only as a track on the parent album. There is no album page to link, so
    searching for them produces confident-looking nonsense: Stereolab's
    "Transmuted Matter" is a track on "Instant Holograms On Metal Film". Of the
    releases since 2024 with a Spotify link but no Apple one, 82% were singles.

  - **The release year has to agree.** Titles repeat across decades. A 2025
    record matched a same-titled 2008 one on the very first row of the first
    run, and a year check is what caught it.

  - **Apple suffixes the format onto the title.** "Foo - Single", "Foo - EP".
    Comparing raw titles rejects most correct matches.

Matching is deliberately conservative and produces three tiers. Only
`confident` is written anywhere; `review` exists so a near miss can be looked at
by a person rather than silently dropped, and `miss` is recorded only as "we
looked", so the same fruitless search is not repeated every week.

What this must never become is a MusicBrainz bot. These links go into this
app's own database, where a wrong one costs a bad link on one page. Offering
them back to MusicBrainz is a separate decision with a separate standard of
evidence -- see docs and the discovered.py docstring.
"""

import re
import time
import unicodedata
from difflib import SequenceMatcher

import requests

SEARCH_URL = "https://itunes.apple.com/search"

# Apple documents "approximately 20 calls per minute" and throttles by IP. Three
# seconds is that limit exactly; there is nothing to gain by crowding it, since
# every run of this is a background sweep nobody is waiting on.
DEFAULT_INTERVAL = 3.0

# Release group types worth searching. See the module docstring: singles are
# excluded because the thing being searched for usually does not exist.
MATCHABLE_TYPES = ("Album", "EP")

TIER_CONFIDENT = "confident"
TIER_REVIEW = "review"
TIER_MISS = "miss"

# How far apart the two release years may be before a same-titled record is
# assumed to be a different one. One year rather than zero: a release genuinely
# appears in another territory, or on another format, the year after its first.
MAX_YEAR_GAP = 1

# Similarity above which two titles are close enough to be worth a human look,
# but not close enough to accept unattended.
REVIEW_RATIO = 0.85

_FORMAT_SUFFIX = re.compile(r"\s[-–—]\s(single|ep|lp)$")
_EDITION_WORDS = re.compile(
    r"\b(deluxe|expanded|remaster(ed)?|edition|version|single|ep|lp)\b"
)
_PAREN_EDITION = re.compile(r"\((deluxe|expanded|remaster(ed)?)[^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text):
    """
    Reduce a title or artist to the form two catalogues can be compared in.

    Strips accents, Apple's trailing format suffix, edition wording and all
    punctuation. Aggressive on purpose: what survives is the part both databases
    agree on, and the year check below is what stops the aggression turning into
    a false match.
    """
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = _FORMAT_SUFFIX.sub("", text)
    text = _PAREN_EDITION.sub(" ", text)
    text = _EDITION_WORDS.sub(" ", text)
    return " ".join(_NON_ALNUM.sub(" ", text).split())


def artists_match(ours, theirs):
    """
    Whether two artist credits name the same act.

    Containment rather than equality, because the two catalogues credit
    collaborations differently: MusicBrainz's "Peyton" is Apple's "Peyton &
    Shafiq Husayn", and its "Miroslav Vitous" is Apple's "Miroslav Vitous,
    Michel Portal". Both directions, since either side can be the fuller one.
    """
    ours, theirs = normalize(ours), normalize(theirs)
    if not ours or not theirs:
        return False
    return ours == theirs or ours in theirs or theirs in ours


def year_of(partial_date):
    """The year from 'YYYY', 'YYYY-MM', 'YYYY-MM-DD' or an ISO timestamp."""
    if not partial_date:
        return None
    try:
        return int(str(partial_date)[:4])
    except (TypeError, ValueError):
        return None


def years_agree(ours, theirs):
    """
    Whether two release dates are close enough to be the same record.

    Unknown on either side is not evidence against a match -- MusicBrainz dates
    are often absent -- so an unknown year abstains rather than rejecting.
    """
    ours, theirs = year_of(ours), year_of(theirs)
    if ours is None or theirs is None:
        return True
    return abs(ours - theirs) <= MAX_YEAR_GAP


def pick_match(release, results):
    """
    Choose the best Apple result for one release, and say how sure we are.

    Returns (tier, result). `confident` requires the normalized titles to be
    equal, the artists to match and the years to agree -- all three. Anything
    that gets two of the three, or is merely very close, is `review`.

    Ordering matters: a confident match anywhere in the results wins over a
    closer-looking review-tier one earlier in them, because Apple ranks by its
    own relevance and the exact match is not reliably first.
    """
    best_review = None
    our_title = normalize(release.get("title"))

    for result in results or []:
        their_title = normalize(result.get("collectionName"))
        if not their_title:
            continue

        exact = their_title == our_title
        artist_ok = artists_match(release.get("artist_credit"), result.get("artistName"))
        year_ok = years_agree(release.get("date"), result.get("releaseDate"))

        if exact and artist_ok and year_ok:
            return TIER_CONFIDENT, result

        if best_review is not None:
            continue
        if exact and artist_ok:
            # Right record, wrong era -- almost always a different recording of
            # the same name, which is exactly what this tier is for.
            best_review = result
        elif exact:
            best_review = result
        elif artist_ok and year_ok:
            ratio = SequenceMatcher(None, our_title, their_title).ratio()
            if ratio >= REVIEW_RATIO:
                best_review = result

    if best_review is not None:
        return TIER_REVIEW, best_review
    return TIER_MISS, None


def clean_url(url):
    """
    Strip the tracking parameters Apple appends to collectionViewUrl.

    `?uo=4` is an affiliate marker. It does not belong in our database and would
    certainly not belong in an edit offered to MusicBrainz.
    """
    url = (url or "").strip()
    url = re.sub(r"[?&]uo=\d+", "", url)
    return re.sub(r"[?&]l=[a-z-]+$", "", url)


def candidates_needing_links(cache_db, service, since_year, types=MATCHABLE_TYPES, limit=None):
    """
    Release groups with no link on `service`, newest first.

    Grouping happens in Python rather than SQL: the candidate set is small
    (hundreds), and picking the representative edition needs the same
    earliest-dated-then-gid ordering the reader uses, which is clumsy to express
    in one GROUP BY and would pin the query to SQLite's window functions.

    The representative edition is the one the link gets attached to. Which one
    it is barely matters -- the reader merges streaming links across a group's
    editions, so a link on any of them reaches the card -- but it has to be
    deterministic so reruns do not scatter links across editions.
    """
    types = tuple(types)
    if not types:
        return []

    placeholders = ",".join(["?"] * len(types))
    rows = cache_db.executesql(
        f"""
        SELECT r.gid, r.title, r.artist_credit, r.date,
               COALESCE(r.release_group_gid, r.gid) AS grp, r.release_group_type
        FROM mb_release r
        WHERE COALESCE(r.release_group_type, '') IN ({placeholders})
          AND COALESCE(r.date, '') >= ?
        """,
        list(types) + [str(since_year)],
    )

    linked = {
        row[0]
        for row in cache_db.executesql(
            "SELECT DISTINCT COALESCE(r.release_group_gid, r.gid) "
            "FROM mb_release r JOIN mb_release_url u ON u.release_gid = r.gid "
            "WHERE u.service = ?",
            [service],
        )
    }

    groups = {}
    for gid, title, artist_credit, date, grp, group_type in rows:
        if grp in linked:
            continue
        entry = dict(
            group_gid=grp,
            release_gid=gid,
            title=title,
            artist_credit=artist_credit,
            date=date,
            release_group_type=group_type,
        )
        current = groups.get(grp)
        if current is None or (date or "9999", gid) < (
            current["date"] or "9999",
            current["release_gid"],
        ):
            groups[grp] = entry

    ordered = sorted(
        groups.values(), key=lambda row: (row["date"] or "", row["release_gid"]), reverse=True
    )
    return ordered[:limit] if limit else ordered


class AppleMusicSearch:
    """
    The iTunes Search API, paced and retried.

    Deliberately shaped like WebServiceSource, including the two details that
    are not preferences:

      - every request goes through the shared cross-process rate limiter, since
        Apple throttles by IP and a forked scheduler child is still this same
        client;
      - `trust_env = False`, because requests consults macOS's system proxy
        configuration on every call and CoreFoundation is not fork-safe. The
        scheduler runs tasks in a forked child, so without this the first
        request in that child dies with SIGSEGV before doing any work. Same
        hazard, same fix, as webservice.py.
    """

    def __init__(self, limiter, user_agent, timeout=20, max_retries=3, session=None, logger=None):
        self.limiter = limiter
        self.timeout = timeout
        self.max_retries = max_retries
        self.logger = logger
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json"}
        )
        self.session.trust_env = False

    def search_albums(self, term, limit=10, country="US"):
        """
        Albums matching a free-text term. Returns [] rather than raising.

        A failed lookup is not worth failing a sweep over: the release keeps its
        missing link and is retried on a later run, which is exactly what would
        happen if it had simply not matched.
        """
        term = (term or "").strip()
        if not term:
            return []

        params = {"term": term, "entity": "album", "limit": limit, "country": country}
        delay = 2.0
        for attempt in range(self.max_retries + 1):
            if self.limiter:
                self.limiter.acquire()
            try:
                response = self.session.get(SEARCH_URL, params=params, timeout=self.timeout)
            except requests.RequestException as error:
                if attempt >= self.max_retries:
                    self._warn("apple search failed for %r: %s", term, error)
                    return []
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            # 403 is what Apple returns when it is throttling, not a permission
            # problem, so it is retried like a 429 rather than given up on.
            if response.status_code in (403, 429) or response.status_code >= 500:
                if attempt >= self.max_retries:
                    self._warn(
                        "apple search gave HTTP %s for %r, giving up",
                        response.status_code,
                        term,
                    )
                    return []
                if self.limiter:
                    self.limiter.penalize(delay)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if response.status_code != 200:
                self._warn("apple search gave HTTP %s for %r", response.status_code, term)
                return []

            try:
                return response.json().get("results", []) or []
            except ValueError:
                self._warn("apple search returned unparseable JSON for %r", term)
                return []
        return []

    def _warn(self, message, *args):
        if self.logger:
            self.logger.warning(message, *args)


def find_links(client, candidates, service="apple_music", rel_type="streaming", logger=None):
    """
    Search for each candidate and sort the answers into tiers.

    Returns (links, reviews, checked) where `links` are confident matches ready
    for discovered_link, `reviews` are near misses worth a person's attention,
    and `checked` is every release looked at with whether anything was found --
    so the caller can record that a search happened and not repeat it weekly
    forever.
    """
    links, reviews, checked = [], [], []

    for candidate in candidates:
        term = f"{candidate.get('artist_credit') or ''} {candidate.get('title') or ''}".strip()
        results = client.search_albums(term)
        tier, result = pick_match(candidate, results)
        url = clean_url((result or {}).get("collectionViewUrl"))

        if tier == TIER_CONFIDENT and url:
            links.append(
                dict(
                    release_gid=candidate["release_gid"],
                    service=service,
                    url=url,
                    rel_type=rel_type,
                )
            )
            if logger:
                logger.info(
                    "matched %s - %s -> %s",
                    candidate.get("artist_credit"),
                    candidate.get("title"),
                    url,
                )
        elif tier == TIER_REVIEW and url:
            reviews.append(
                dict(
                    release_gid=candidate["release_gid"],
                    title=candidate.get("title"),
                    artist_credit=candidate.get("artist_credit"),
                    date=candidate.get("date"),
                    url=url,
                    apple_title=(result or {}).get("collectionName"),
                    apple_artist=(result or {}).get("artistName"),
                    apple_date=(result or {}).get("releaseDate"),
                )
            )

        checked.append((candidate["release_gid"], tier == TIER_CONFIDENT))

    return links, reviews, checked
