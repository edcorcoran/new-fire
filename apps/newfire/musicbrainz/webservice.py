"""
The MusicBrainz web service as an MBSource.

Emits the same normalized dicts as the Postgres mirror, so the cache writer and
the label page cannot tell the two apart.

Two behaviours here are not optional:

  - Every request goes through a cross-process rate limiter. See ratelimit.py.
  - 503s are retried with backoff. These are routine rather than exceptional:
    crawling Drag City's 13 pages while already pacing at 1.1s drew two of them.
    A client without retries fails partway through most large labels.

A descriptive User-Agent carrying a contact address is mandatory; MusicBrainz
blocks generic ones.

See docs/musicbrainz-cache-plan.md sections 2 and 4.4.
"""

import time

import requests

from .normalize import (
    classify_url,
    dedupe_urls,
    flatten_artist_credit,
    is_listenable,
    make_label,
    make_release,
    make_url,
)
from .sources import MBSource

BASE_URL = "https://musicbrainz.org/ws/2"

# Everything the label page renders, in one request per 100 releases: artist
# credits, catalog numbers, streaming links, and the release group the pages
# collapse editions on. Without these the page would need a follow-up request
# per release, which no rate limit could absorb.
RELEASE_INCLUDES = "artist-credits+labels+url-rels+release-groups"

# The web service refuses anything larger.
MAX_PAGE_SIZE = 100

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class MusicBrainzError(RuntimeError):
    """A request failed in a way retrying will not fix."""


class WebServiceSource(MBSource):
    name = "webservice"

    def __init__(
        self,
        user_agent,
        limiter,
        base_url=BASE_URL,
        timeout=30,
        max_retries=5,
        session=None,
        logger=None,
    ):
        if not user_agent:
            raise ValueError(
                "MusicBrainz requires a descriptive User-Agent with a contact "
                "address; set MB_USER_AGENT_CONTACT in settings"
            )
        self.user_agent = user_agent
        self.limiter = limiter
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.logger = logger
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": user_agent, "Accept": "application/json"}
        )
        # Not a preference — this stops the scheduler segfaulting on macOS.
        #
        # requests consults the environment for proxies on every request, and
        # on macOS that ends in _scproxy -> SCDynamicStoreCopyProxiesWithOptions
        # -> CoreFoundation. CoreFoundation is not fork-safe, and pydal's
        # scheduler runs each task in a forked child, so the first request in
        # that child dies with SIGSEGV in CFPreferences before doing any work.
        # The label page retries every ten seconds, which turns one sync into a
        # crash loop. Same family as MB_DB_DRIVER_ARGS' gssencmode workaround
        # for psycopg in a forked child.
        #
        # The cost is that HTTP(S)_PROXY, REQUESTS_CA_BUNDLE and .netrc are
        # ignored. MusicBrainz is a public API reached directly, so none apply;
        # a deployment needing a proxy must pass a preconfigured session.
        self.session.trust_env = False

    # ---------------------------------------------------------------- requests

    def _get(self, path, params=None):
        """
        One rate-limited GET, retrying throttles and transient server errors.

        Returns the decoded body, or None for 404.
        """
        params = dict(params or {})
        params["fmt"] = "json"
        url = f"{self.base_url}/{path.lstrip('/')}"

        delay = 1.0
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                response = self.session.get(
                    url, params=params, timeout=self.timeout
                )
            except requests.RequestException as error:
                if attempt >= self.max_retries:
                    raise MusicBrainzError(f"{url}: {error}") from error
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue

            if response.status_code == 404:
                return None
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as error:
                    # A 200 with an unparseable body. Retrying won't help — the
                    # server already claimed success — so fail with the same
                    # error type as every other unfixable response.
                    raise MusicBrainzError(
                        f"{url}: invalid JSON in response: {error}"
                    ) from error

            if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = self._retry_after(response, delay)
                # Slow every process down, not just this one: a throttle is
                # aimed at the application, not at whichever worker got it.
                self.limiter.penalize(wait)
                if self.logger:
                    self.logger.warning(
                        "musicbrainz %s from %s, backing off %.1fs",
                        response.status_code,
                        url,
                        wait,
                    )
                delay = min(delay * 2, 30)
                continue

            raise MusicBrainzError(
                f"{url}: HTTP {response.status_code} {response.text[:200]}"
            )

        raise MusicBrainzError(f"{url}: giving up after {self.max_retries} retries")

    @staticmethod
    def _retry_after(response, fallback):
        """
        Honour Retry-After when the server sends a usable one, but never
        below the fallback.

        MusicBrainz answers some 503s with `Retry-After: 0`, which reads less
        like "retry immediately" than like a header nobody filled in. Taking it
        literally replaced the exponential backoff with no backoff at all and
        spent the whole retry budget inside a couple of seconds -- exactly when
        the service had just said it was struggling. Taking the larger of the
        two honours a server asking for longer while ignoring one asking for
        nothing.
        """
        header = response.headers.get("Retry-After")
        if header and header.strip().isdigit():
            return max(float(header.strip()), fallback)
        return fallback

    # ----------------------------------------------------------------- MBSource

    def get_label(self, gid):
        data = self._get(f"label/{gid}")
        if not data:
            return None
        return self._to_label(data)

    def count_releases_by_label(self, gid):
        """
        The label's release count, in one cheap request.

        This is the change check: when it matches what the cache holds, nothing
        was added or removed and a full re-crawl can be skipped entirely --
        one request instead of thirteen for a label the size of Drag City.
        """
        data = self._get(
            "release", {"label": str(gid), "limit": 1, "offset": 0}
        )
        if not data:
            return 0
        return data.get("release-count", 0)

    def browse_releases_by_label(self, gid, limit=100, offset=0):
        data = self._get(
            "release",
            {
                "label": str(gid),
                "inc": RELEASE_INCLUDES,
                "limit": min(limit, MAX_PAGE_SIZE),
                "offset": offset,
            },
        )
        if not data:
            return [], 0

        total = data.get("release-count", 0)
        releases = [
            self._to_release(item, str(gid)) for item in data.get("releases", [])
        ]
        return releases, total

    def search_labels(self, query, limit=25):
        if not query or not query.strip():
            return []
        data = self._get("label", {"query": query.strip(), "limit": limit})
        if not data:
            return []
        return [self._to_label(item) for item in data.get("labels", [])]

    def get_release(self, gid, label_gid=None):
        """One release in full, with its URL relationships. One request."""
        data = self._get(f"release/{gid}", {"inc": RELEASE_INCLUDES})
        if not data:
            return None
        return self._to_release(data, str(label_gid) if label_gid else None)

    def find_releases_since(self, label_gid, label_name, since_year, limit=100):
        """
        Releases on a label dated `since_year` or later, in one request.

        This is what makes an incremental sync worthwhile: asking the search
        index for a label's recent releases costs one request regardless of how
        big the label is, where re-reading Drag City's catalogue costs thirteen.

        Two awkward details, both verified against the live service:

        - There is no searchable label id. `labelid:<mbid>` is not a recognised
          field and silently returns zero results, so the query has to match on
          the label's *name* — which is ambiguous, as six different labels are
          called "Domino". Search hits do carry label-info with real MBIDs, so
          the results are filtered on the exact id afterwards. On a Drag City
          test that correctly kept 53 of 54 hits and dropped a same-name
          impostor.

        - Search results carry no URL relationships, so the releases returned
          here have `urls=None` — meaning "not looked up", which the writer
          treats as "leave whatever is cached alone". Callers wanting streaming
          links follow up with get_release.
        """
        if not label_name:
            return []

        escaped = label_name.replace('"', r"\"")
        query = f'label:"{escaped}" AND date:[{int(since_year)} TO 9999]'
        data = self._get("release", {"query": query, "limit": min(limit, MAX_PAGE_SIZE)})
        if not data:
            return []

        wanted = str(label_gid)
        releases = []
        for item in data.get("releases", []):
            if not _mentions_label(item, wanted):
                continue
            release = self._to_release(item, wanted)
            # Nothing was fetched about this release's links, so say so rather
            # than claiming it has none.
            release["urls"] = None
            releases.append(release)
        return releases

    # -------------------------------------------------------------- conversion

    @staticmethod
    def _to_label(data):
        area = data.get("area") or {}
        return make_label(
            gid=data["id"],
            name=data.get("name"),
            disambiguation=data.get("disambiguation"),
            label_type=data.get("type"),
            area_name=area.get("name"),
            label_code=data.get("label-code"),
            sort_name=data.get("sort-name"),
        )

    @staticmethod
    def _to_release(data, label_gid):
        credits = data.get("artist-credit") or []
        primary = (credits[0].get("artist") or {}) if credits else {}
        cover_art = data.get("cover-art-archive") or {}
        group = data.get("release-group") or {}

        return make_release(
            gid=data["id"],
            title=data.get("title"),
            artist_credit=flatten_artist_credit(credits),
            artist_gid=primary.get("id"),
            release_group_gid=group.get("id"),
            release_group_type=group.get("primary-type"),
            release_group_first_date=group.get("first-release-date") or None,
            # Already a partial-date string, and verified equal to what the
            # mirror's release_country logic produces for all 1,281 shared
            # Drag City releases.
            date=data.get("date") or None,
            country=data.get("country"),
            status=data.get("status"),
            disambiguation=data.get("disambiguation"),
            has_front_cover=cover_art.get("front"),
            catalog_numbers=_catalog_numbers(data, label_gid),
            urls=_streaming_urls(data),
        )


def _mentions_label(data, label_gid):
    """Whether a search hit really is on the label we asked about."""
    return any(
        (info.get("label") or {}).get("id") == label_gid
        for info in data.get("label-info") or []
    )


def _catalog_numbers(data, label_gid):
    """
    Catalog numbers this release carries under the label being browsed.

    label-info lists every label on the release, so entries for other labels are
    filtered out by MBID. A release can appear more than once for one label with
    different formattings of the same number.
    """
    numbers = []
    for info in data.get("label-info") or []:
        label = info.get("label") or {}
        if label.get("id") != label_gid:
            continue
        number = info.get("catalog-number")
        if number:
            numbers.append(number)
    return numbers


def _streaming_urls(data):
    """
    Listenable links from the release's URL relationships.

    Returns [] rather than None when there are none: the release was fetched
    with url-rels, so an empty list is knowledge ("this release has no links"),
    not absence of data. The writer only skips releases whose urls key is None.
    """
    urls = []
    for relation in data.get("relations") or []:
        if not is_listenable(relation.get("type")):
            continue
        resource = (relation.get("url") or {}).get("resource")
        service = classify_url(resource)
        if service:
            urls.append(make_url(service, resource, relation.get("type")))
    return dedupe_urls(urls)
