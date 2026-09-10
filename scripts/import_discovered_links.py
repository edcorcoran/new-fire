#!/usr/bin/env python
"""
Load confirmed streaming-link matches into storage.db, and project them.

The matches come from searching a streaming service's catalogue for releases
MusicBrainz describes but has no link for. That search is fuzzy and its output
has to be reviewed by a person before it goes anywhere, so this script's input
is the *reviewed* set: a CSV carrying at least a MusicBrainz release MBID and
the URL confirmed for it.

    # everything a review marked confident
    python scripts/import_discovered_links.py matches.csv

    # see what it would do first
    python scripts/import_discovered_links.py matches.csv --dry-run

The CSV needs `mb_release_mbid` and `apple_url` (or `url`). If it also carries a
`tier` column -- as the matcher's own output does -- only `confident` rows are
imported unless --tier says otherwise, so the raw matcher file and a reviewed
export can both be fed in without editing either.

Links land in discovered_link in storage.db, which is the record, and are then
copied into the cache so pages show them. That copy is redone after every sync
by tasks._restore_discovered_links; running it here just means the links appear
without waiting for one.
"""

import argparse
import csv
import datetime
import os
import re
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "newfire")
)

from py4web import DAL  # noqa: E402

from musicbrainz.cache import connect_cache  # noqa: E402
from musicbrainz.discovered import (  # noqa: E402
    apply_discovered_links,
    create_discovered_indexes,
    define_discovered_link_table,
)
from musicbrainz.normalize import classify_url  # noqa: E402

DEFAULT_STORAGE = "apps/newfire/databases/storage.db"
DEFAULT_CACHE = "apps/newfire/databases/mbcache.db"

# Which MusicBrainz relationship each service's link would be filed under. Apple
# Music is a subscription service, so its releases are "streaming" rather than
# "free streaming" -- the same distinction MusicBrainz draws, and the one
# normalize.LISTENABLE_REL_TYPES ranks on.
REL_TYPES = {
    "apple_music": "streaming",
    "spotify": "free streaming",
    "bandcamp": "free streaming",
}

MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def clean_url(url):
    """
    Strip the tracking parameters Apple's search API appends.

    `?uo=4` is an affiliate marker on every collectionViewUrl it returns. It
    does not belong in our own database and would certainly not belong in an
    edit offered to MusicBrainz.
    """
    url = (url or "").strip()
    url = re.sub(r"[?&]uo=\d+", "", url)
    return re.sub(r"[?&]l=[a-z-]+$", "", url)


def read_matches(path, tier):
    """Parse the CSV into link dicts, reporting what it skipped and why."""
    links, skipped = [], {"tier": 0, "no url": 0, "bad mbid": 0, "unknown service": 0}
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        gid_col = "mb_release_mbid" if "mb_release_mbid" in fields else "release_gid"
        url_col = "apple_url" if "apple_url" in fields else "url"
        if gid_col not in fields or url_col not in fields:
            raise SystemExit(
                f"{path}: need a release MBID column ({gid_col}) and a URL column "
                f"({url_col}); found {', '.join(fields) or 'nothing'}"
            )

        for row in reader:
            if tier and "tier" in fields and (row.get("tier") or "") != tier:
                skipped["tier"] += 1
                continue
            gid = (row.get(gid_col) or "").strip().lower()
            url = clean_url(row.get(url_col))
            if not url:
                skipped["no url"] += 1
                continue
            if not MBID.match(gid):
                skipped["bad mbid"] += 1
                continue
            service = classify_url(url)
            if not service:
                skipped["unknown service"] += 1
                continue
            links.append(
                dict(
                    release_gid=gid,
                    service=service,
                    url=url,
                    rel_type=REL_TYPES.get(service),
                )
            )
    return links, skipped


def store(db, links, source, dry_run=False):
    """
    Upsert into discovered_link. Returns (added, updated, unchanged).

    Identity is (release_gid, service): re-importing a corrected match should
    replace the URL rather than leave both, which is what the unique index is
    for.
    """
    table = db.discovered_link
    existing = {
        (row.release_gid, row.service): row
        for row in db(table.id > 0).select()
    }
    added = updated = unchanged = 0
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    for link in links:
        row = existing.get((link["release_gid"], link["service"]))
        if row is None:
            added += 1
            if not dry_run:
                table.insert(source=source, found_on=now, **link)
        elif row.url != link["url"] or row.rel_type != link["rel_type"]:
            updated += 1
            if not dry_run:
                db(table.id == row.id).update(
                    url=link["url"],
                    rel_type=link["rel_type"],
                    source=source,
                    found_on=now,
                )
        else:
            unchanged += 1

    if not dry_run:
        db.commit()
    return added, updated, unchanged


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="reviewed matches to import")
    parser.add_argument("--storage", default=DEFAULT_STORAGE)
    parser.add_argument("--cache", default=DEFAULT_CACHE)
    parser.add_argument(
        "--tier",
        default="confident",
        help="only import rows with this tier when the CSV has a tier column "
        "('' imports every row)",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="what to record as the origin of these links "
        "(default: the CSV's name and today's date)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    source = args.source or "%s %s" % (
        os.path.basename(args.csv),
        datetime.date.today().isoformat(),
    )

    links, skipped = read_matches(args.csv, args.tier)
    print(f"{len(links)} link(s) to import from {args.csv}")
    for reason, count in skipped.items():
        if count:
            print(f"  skipped {count} ({reason})")
    if not links:
        return 0

    storage_dir, storage_name = os.path.split(os.path.abspath(args.storage))
    db = DAL(f"sqlite://{storage_name}", folder=storage_dir, migrate=True, pool_size=0)
    define_discovered_link_table(db)
    create_discovered_indexes(db)
    try:
        added, updated, unchanged = store(db, links, source, args.dry_run)
        print(
            f"discovered_link: {added} added, {updated} updated, {unchanged} unchanged"
            + (" (dry run, nothing written)" if args.dry_run else "")
        )

        if args.dry_run:
            return 0

        cache_dir, cache_name = os.path.split(os.path.abspath(args.cache))
        cache = connect_cache(
            f"sqlite://{cache_name}", folder=cache_dir, migrate=False, pool_size=0
        )
        try:
            applied = apply_discovered_links(cache, links)
            cache.commit()
            print(f"cache: {applied} link row(s) added, {len(links) - applied} already present")
        finally:
            cache.close()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
