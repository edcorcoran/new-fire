#!/usr/bin/env python
"""
Build a prewarmed mbcache.db from the local MusicBrainz mirror.

Why this exists: a label nobody has looked at is unrenderable for as long as its
first sync takes, and over the web service that is one request per hundred
releases at one request a second — half a minute for a label the size of Drag
City. Fine for you, alone, once. Not fine as the first thing a stranger sees.
Seeding from the mirror is ~25x faster than the API and costs MusicBrainz
nothing, so the labels people are most likely to follow can arrive already warm.

Seeding the whole of MusicBrainz is not an option: 278,345 labels carry 4.5M
releases, which is 2-3 GB of cache. This seeds a chosen list instead, at roughly
0.7 KB per release, and prints what it built.

    # the labels you already follow
    python scripts/seed_cache.py --from-tracked apps/newfire/databases/storage.db

    # a data-driven starter set: independent labels, active, not majors
    python scripts/seed_cache.py --top 100 --out ./seed

    # an explicit list, one MBID per line, # for comments
    python scripts/seed_cache.py --labels scripts/seed_labels.txt

Order matters if you are refreshing an existing cache rather than building one.
sync_label prunes links the source no longer lists, and the mirror lags the web
service, so seeding *over* a cache the app has already brought up to date will
roll it back to the mirror's view. Seed cold, then let the app catch up.
"""

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "apps", "newfire"))

from py4web import DAL  # noqa: E402

from musicbrainz.cache import connect_cache  # noqa: E402
from musicbrainz.maintenance import cleanup_cache  # noqa: E402
from musicbrainz.sources import PostgresMirrorSource  # noqa: E402
from musicbrainz.writer import sync_label  # noqa: E402

# Placeholder, matching apps/newfire/settings.py. A mirror is a machine you have
# to have, so there is no default worth shipping: point --mb-uri or MB_DB_URI at
# yours. Left as a well-formed URI rather than None so an unset run fails while
# connecting, naming the host it could not reach, instead of inside argparse.
DEFAULT_MB_URI = "postgres://musicbrainz:musicbrainz@host:5432/musicbrainz_db"

# Types that are companies rather than imprints — the same list search hides.
# A holding company with 4,000 releases is exactly what a "most active labels"
# query surfaces and exactly what nobody follows.
CORPORATE_TYPES = ("Holding", "Publisher", "Rights Society", "Creative Agency", "Manufacturer")

# Picks labels that look like real imprints. Ranking purely by recent release
# count does not work: it returns lofi aggregators, ASMR farms and an account
# named "user-177606669", because release count measures upload rate rather than
# whether anyone wants the records. These three conditions filter those out —
# an IFPI label code, external links (site, Discogs, Bandcamp), and a catalogue
# spread across many artists rather than a handful of aliases.
#
# It still only gets you *legitimate* labels, not labels to your taste; it
# skews to dance and classical, which is where the volume is. Treat the output
# as a shortlist to edit, and see scripts/seed_labels.txt for a hand-picked
# alternative. The release-count band is what keeps the majors out: Polydor
# alone is 26,348 releases, about 18 MB.
TOP_LABELS_SQL = """
SELECT l.gid, l.name, COUNT(DISTINCT rl.release) AS n
FROM label l
JOIN release_label rl ON rl.label = l.id
JOIN release r        ON r.id = rl.release
JOIN artist_credit ac ON ac.id = r.artist_credit
JOIN release_group_meta rgm ON rgm.id = r.release_group
LEFT JOIN label_type lt ON lt.id = l.type
WHERE (lt.name IS NULL OR lt.name <> ALL(%s))
  AND l.label_code IS NOT NULL
  AND EXISTS (SELECT 1 FROM l_label_url lu WHERE lu.entity0 = l.id)
GROUP BY l.id
HAVING COUNT(DISTINCT rl.release) BETWEEN %s AND %s
   AND COUNT(DISTINCT ac.id)::float / COUNT(DISTINCT rl.release) > 0.25
   AND COUNT(DISTINCT rl.release) FILTER (WHERE rgm.first_release_date_year >= %s) >= 5
ORDER BY COUNT(DISTINCT rl.release) FILTER (WHERE rgm.first_release_date_year >= %s) DESC
LIMIT %s
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("what to seed")
    src.add_argument("--labels", metavar="FILE", help="file of label MBIDs, one per line")
    src.add_argument("--from-tracked", metavar="STORAGE_DB",
                     help="seed every label followed in this app database")
    src.add_argument("--top", type=int, metavar="N",
                     help="seed the N most active independent labels in the mirror")
    ap.add_argument("--out", default="seed", help="directory to write mbcache.db into")
    ap.add_argument("--mb-uri", default=os.environ.get("MB_DB_URI", DEFAULT_MB_URI),
                    help="the MusicBrainz mirror to read; also read from $MB_DB_URI")
    ap.add_argument("--min-releases", type=int, default=25, help="--top: smallest catalogue")
    ap.add_argument("--max-releases", type=int, default=2500, help="--top: largest catalogue")
    ap.add_argument("--since-year", type=int, default=2022, help="--top: what counts as active")
    args = ap.parse_args()

    if not (args.labels or args.from_tracked or args.top):
        ap.error("choose at least one of --labels, --from-tracked, --top")

    os.makedirs(args.out, exist_ok=True)
    mirror = DAL(args.mb_uri, folder=args.out, pool_size=0, migrate=False,
                 fake_migrate=False, driver_args={"gssencmode": "disable"})
    source = PostgresMirrorSource(mirror)

    wanted = []
    if args.from_tracked:
        wanted += _tracked_labels(args.from_tracked)
    if args.labels:
        wanted += _read_label_file(args.labels)
    if args.top:
        wanted += _top_labels(mirror, args)
    # Preserve order, drop repeats: the same label can arrive from two sources.
    seen, ordered = set(), []
    for gid in wanted:
        if gid not in seen:
            seen.add(gid)
            ordered.append(gid)

    print(f"seeding {len(ordered)} label(s) into {os.path.join(args.out, 'mbcache.db')}\n")
    cache = connect_cache("sqlite://mbcache.db", folder=args.out, migrate=True, pool_size=0)

    total_releases = failed = 0
    started = time.time()
    for i, gid in enumerate(ordered, 1):
        t0 = time.time()
        try:
            state = sync_label(source, cache, gid)
        except Exception as error:  # noqa: BLE001 - one bad label must not stop the seed
            failed += 1
            print(f"  [{i}/{len(ordered)}] {gid} FAILED: {error}")
            continue
        label = cache(cache.mb_label.gid == gid).select().first()
        name = label.name if label else gid
        total_releases += state.release_count_local or 0
        print(f"  [{i}/{len(ordered)}] {name[:38]:<38} {state.release_count_local:>6} releases"
              f"  {time.time() - t0:>5.1f}s")

    print("\ntidying...")
    print(" ", cleanup_cache(cache, grace_days=0, logger=None))
    cache.close()
    mirror.close()

    path = os.path.join(args.out, "mbcache.db")
    size = os.path.getsize(path) / (1024 * 1024)
    print(f"\n{len(ordered) - failed} labels, {total_releases:,} releases, "
          f"{size:.1f} MB in {time.time() - started:.0f}s -> {path}")
    if failed:
        print(f"{failed} label(s) failed; rerun to retry, seeding is idempotent")


def _read_label_file(path):
    with open(path) as handle:
        return [
            line.split("#")[0].strip()
            for line in handle
            if line.split("#")[0].strip()
        ]


def _tracked_labels(storage_db):
    """Every label followed by anyone in an app database."""
    conn = sqlite3.connect(storage_db)
    try:
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT label_gid FROM tracked_label ORDER BY label_gid")]
    finally:
        conn.close()


def _top_labels(mirror, args):
    rows = mirror.executesql(
        TOP_LABELS_SQL,
        (list(CORPORATE_TYPES), args.min_releases, args.max_releases,
         args.since_year, args.since_year, args.top),
    )
    print(f"picked {len(rows)} active labels from the mirror "
          f"({args.min_releases}-{args.max_releases} releases, active since {args.since_year})")
    print("review these before shipping — the query finds real labels, not "
          "necessarily ones anyone wants to follow:")
    for _gid, name, n in rows:
        print(f"    {name[:44]:<44} {n:>6} releases")
    return [str(row[0]) for row in rows]


if __name__ == "__main__":
    main()
