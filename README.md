# new fire

Follow record labels; see what they put out.

Search MusicBrainz for a label, follow it, and its releases appear newest-first
with links to hear them. It is built for people who follow *labels* rather than
artists — the Drag City or Light in the Attic listener who wants to know what
just landed.

## How it works

MusicBrainz is the source of truth, but querying it per page view is not viable:
its web service allows one request per second, and a label the size of Warp is
26 requests. So the app keeps a local SQLite **cache** of just the labels people
follow, and serves every page from it.

```
  MusicBrainz web service ─┐
                           ├─→  mbcache.db  ─→  pages
  Postgres mirror (seed) ──┘     (SQLite)
```

Three ideas carry most of the design:

- **One source interface, two implementations.** `WebServiceSource` (live, rate
  limited) and `PostgresMirrorSource` (a local MusicBrainz mirror, no limit)
  emit identical normalized dicts, so the cache writer cannot tell them apart.
  The mirror seeds; the web service keeps things current.
- **Nothing waits on MusicBrainz.** Syncing a label is a background job. A
  label already cached renders immediately even if it is due a refresh, and the
  refresh lands before the next visit.
- **A row is an album, not a release.** MusicBrainz models the CD, LP and
  digital edition of one record as three releases sharing a *release group*.
  Pages collapse those into a single card, dated when *this label* first issued
  the record — so a repress does not resurface as news, while a genuine reissue
  is flagged against the work's original date.

The reasoning, and the measurements behind it, are in
[docs/musicbrainz-cache-plan.md](docs/musicbrainz-cache-plan.md). It is worth
reading before changing anything in `apps/newfire/musicbrainz/`.

## Running it

Requires Python 3.9+ — the floor its dependencies declare, and the oldest the
code itself needs. Developed on 3.14.

```bash
python -m venv .venv && .venv/bin/pip install py4web psycopg2-binary
.venv/bin/py4web run apps --app_names _default --port 8123
```

Then open <http://127.0.0.1:8123/>.

`--app_names _default` is not optional. `apps/_default` is a symlink to
`apps/newfire`, which mounts the app at `/` rather than `/newfire`, but py4web
enumerates its apps folder by listing it — so without the flag both names load,
the app is imported twice as two unrelated modules, and each copy starts its own
scheduler. The two then share a worker identity, since pydal derives one from
the host IP, and re-queue each other's claimed runs.

`psycopg2-binary` is only needed to read a local MusicBrainz mirror. A checkout
that reads the web service does not need it.

The stock py4web apps -- the dashboard, the docs, the scaffold -- are not in
this repo, so there is no dashboard password prompt on first run. Avoid
`py4web setup apps`: it reinstalls them from py4web's bundled copies. Plain
`py4web run apps` never does.

### Configuration

App settings live in `apps/newfire/settings.py`. Anything local or secret goes in
`apps/newfire/settings_private.py`, which is gitignored and overrides it:

```python
# Which MusicBrainz to read. "webservice" is the production answer.
MB_SOURCE = "webservice"

# MusicBrainz blocks generic User-Agents. An email or a URL, but a real one.
MB_USER_AGENT_CONTACT = "https://github.com/you/your-fork"

# Only needed for seeding — a local MusicBrainz Postgres mirror.
MB_DB_URI = "postgres://musicbrainz:musicbrainz@host:5432/musicbrainz_db"
```

Settings worth knowing:

| Setting | Meaning |
|---|---|
| `MB_SOURCE` | `webservice` (live) or `postgres` (a local mirror) |
| `MB_REFRESH_PERIOD` | How often followed labels are checked for new releases |
| `MB_FULL_RESYNC_DAYS` | How often a label is re-read in full regardless |
| `MB_HIDE_LABEL_TYPES` | Label types hidden from search; `None` uses the default |
| `MB_CLEANUP_PERIOD` | How often unreachable cache rows are collected |

## Seeding the cache

A label nobody has looked at takes one request per hundred releases to fill, at
one request a second. Fine for you, once. Not fine as a stranger's first
impression. If you have a MusicBrainz Postgres mirror, seed from it instead. The
69 labels below took **85 seconds** from a mirror; the same catalogue over the
web service is 559 requests and at least ten minutes, all of it MusicBrainz's
bandwidth rather than yours:

```bash
export MB_DB_URI="postgres://musicbrainz:musicbrainz@your-mirror:5432/musicbrainz_db"
python scripts/seed_cache.py --labels scripts/seed_labels.txt
python scripts/seed_cache.py --from-tracked apps/newfire/databases/storage.db
```

The mirror comes from `--mb-uri`, or `$MB_DB_URI` as above. There is no useful
default: a mirror is a machine you have to have, and the placeholder in the
script is there only so an unset run fails while connecting, naming the host it
could not reach.

`scripts/seed_labels.txt` is a starter list, meant to be edited. Seeding all of
MusicBrainz is not an option — 278,000 labels carry 4.5M releases, some 2–3 GB
of cache — so seeding is always a chosen list. The 69 labels shipped here are
~56,000 releases and about 40 MB. Note the list records MBIDs, not names:
resolving labels by name picks the wrong entity often enough to matter.

**Seed cold.** A sync prunes releases its source no longer lists, and a mirror
lags the live service, so seeding *over* a cache the app has already brought up
to date will roll it back. Seed first, then let the app catch up.

## Background work

The scheduler runs three tasks, all registered in `apps/newfire/tasks.py`:

| Task | When | What |
|---|---|---|
| `mb_sync_label` | On demand | Pulls one label's releases into the cache |
| `mb_refresh_tracked` | Daily | Checks each followed label for new releases |
| `mb_cleanup_cache` | Weekly | Drops unreachable rows and vacuums |

The daily sweep is cheap by design: asking whether a label changed is one
request, and only labels that moved are re-read.

## Layout

```
apps/newfire/
  controllers.py        pages
  models.py             the one user table: which labels you follow
  tasks.py              background jobs
  scheduling.py         a scheduler that survives its own accidents
  settings.py           configuration
  musicbrainz/
    sources.py          MBSource interface + Postgres mirror
    webservice.py       the live MusicBrainz API
    normalize.py        the record shape both sources emit
    cache.py            SQLite cache schema
    writer.py           syncing and upserts
    reader.py           what the pages read, including grouping and filters
    service.py          search, and deciding when a sync is due
    maintenance.py      cache cleanup
    ratelimit.py        cross-process rate limiter
scripts/seed_cache.py   build a prewarmed cache from a mirror
deploy/                 Passenger and cron entry points; see DEPLOY.md
docs/                   the design study
```

## Deploying

[DEPLOY.md](DEPLOY.md) covers the live deployment: a Passenger web tier that only
enqueues work, and a single cron-supervised worker that runs it. The split
exists because the application server owns the lifetime of its web processes,
which is the wrong shape for a queue that has to run overnight.

## Notes for anyone changing this

- **The cache is disposable.** It is rebuildable from either source and holds no
  user data; the only thing that would hurt to lose is `tracked_label` in
  `storage.db`. Keep it that way.
- **Everything is keyed on MBIDs.** MusicBrainz's integer ids are not stable
  across mirror rebuilds and the web service does not expose them.
- **Sync order is undefined until a label is complete.** The browse endpoint
  does not return releases by date, so a partially synced label cannot be
  paginated newest-first. `is_complete` guards this.
- **The source abstraction is clean at the API boundary, not the process
  boundary.** Each implementation drags in its own hazards inside the forked
  scheduler child — see the `gssencmode` driver argument for psycopg and
  `trust_env` for requests. Swapping sources means re-testing the scheduler,
  not just the pages.

## Data

Release data from [MusicBrainz](https://musicbrainz.org), cover art from the
[Cover Art Archive](https://coverartarchive.org). If you run this against the
live web service, set a real contact address — it is their policy, and they
block requests without one.

## License

[GNU Affero General Public License](LICENSE), version 3 or (at your option)
any later version. The AGPL rather than a permissive license because this is
a web application: anyone offering a modified copy of it as a service must
publish their changes, the same way these are published.
