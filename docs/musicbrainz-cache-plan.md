# MusicBrainz SQLite Caching Layer — Plan & Feasibility Study

**Status:** proposal, no code written
**Date:** 2026-07-27

---

## 1. Verdict

**The SQLite cache is the right call — but for different reasons than a naive read of the API
suggests.** This section was rewritten after testing against Drag City; the corrections are in §2.5.

The case rests on three measured findings:

1. **The API's `date` field exactly reproduces the "funky SQL".** Tested across all 1,281 Drag City
   releases present in both sources: **1,281/1,281 exact match**, including identical null handling
   (15 nulls each). The `earliest_country` CTE in
   [controllers.py:70-90](../apps/newfire/controllers.py#L70-L90) can be deleted.
2. **Cold sync is slow but is paid once, by a background job.** Drag City's 1,297 releases took
   **13 requests / 28.4 seconds** — not the ~1s a per-request reading implies. That is fine when
   nothing is waiting on it, and it can be skipped entirely by seeding from your mirror.
3. **Steady state is cheap regardless of label size.** Checking whether Drag City changed costs
   **1 request**. Fetching *only* what's new costs **1 more** — not a 13-page re-crawl. See §4.4.

**The cache is ~6,500× smaller than the mirror.** Your Postgres mirror is 64 GB. Drag City's full
1,297 releases occupy roughly **1 MB**. Fifty Drag-City-sized labels — a pessimistic model of your
actual usage — is **~39 MB**.

The argument is *not* "labels are small, so everything is one request." That was wrong, and §2.5
explains why.

---

## 2. What I measured

All numbers are from a live local mirror and the live MusicBrainz web service today.

### Your Postgres mirror

| Metric | Value |
|---|---|
| Total DB size | **64 GB** (`musicbrainz` schema alone: 59 GB) |
| Labels | 333,307 |
| Releases | 5,407,856 |
| Labels with ≥1 release | 278,346 |

### Releases per label — and why this distribution is misleading

| Population | p50 | p75 | p90 | p99 | mean |
|---|---|---|---|---|---|
| All 278,346 labels with releases | 2 | — | 19 | 185 | 16.7 |
| Active since 2021 **and** externally linked (46,958) | 8 | 27 | 86 | 728 | 67.2 |

**Neither row justifies the design, because both are the wrong population.** Nobody follows a
defunct label that put out five records. The labels that matter here sit at p99 and beyond:

| Label | Releases | Requests @ limit=100 |
|---|---|---|
| Hyperdub | 474 | 5 |
| Ghostly International | 795 | 8 |
| Stones Throw | 1,020 | 11 |
| **Drag City** | **1,297** | **13** |
| Ninja Tune | 2,250 | 23 |
| 4AD | 2,799 | 28 |

Plan for **~1,000–3,000 releases per tracked label**, not 19.

### Drag City — the full cold-sync measurement

Paging the entire label, `inc=artist-credits+labels+url-rels`, `limit=100`, paced at 1.1s:

```
13 requests | 2 retries (503) | 1,297 releases | 2.7 MB | 28.4s wall | 2.18s/req effective
```

Two things fell out of this that the first draft got wrong:

- **28.4 seconds, not ~1.3.** Effective throughput is 2.18s/request once pacing and retries are
  counted — roughly half the naive 1 req/sec assumption.
- **503s are routine, not exceptional.** I was throttled **twice in 13 requests** while already
  pacing at 1.1s. Retry-with-backoff is load-bearing infrastructure here, not a nicety. A client
  without it fails partway through every large label.

Also: the API reports 1,297 releases, your mirror has 1,287, and 16 releases exist in the API that
your mirror lacks entirely. The mirror is measurably behind.

### The date question — settled empirically

I ran your exact `earliest_country` CTE against the API's top-level `date` for every Drag City
release present in both:

```
overlap = 1,281 releases
DATE MATCH: 1,281/1,281 (100.0%)   mismatches: 0
null dates -> CTE: 15   API: 15
API top-level date != min(release-events.date): 0
```

The API's `date` implements exactly the same semantics as your CTE — earliest release event, with
the `release_unknown_country` fallback — and hands it over as a pre-formatted partial-date string.
**This claim held up under a full-label test.**

### The API response — no field fan-out, but you *do* page

Measured: `GET /release?label=<hyperdub>&inc=artist-credits+labels+url-rels&limit=100`
→ **1.27s, 224 KB, `release-count: 474`**, 100 releases returned.

The useful property is that **one request carries every field for up to 100 releases** — there is no
N+1 fan-out to fetch artist credits, catalog numbers, streaming URLs or cover-art status separately.
That is what makes a 1 req/sec budget survivable at all. It does **not** mean a label costs one
request; `limit=100` is the hard maximum and large labels page (Drag City: 13 pages).

Per-release fields present in that one response:

| App needs | API field | Coverage in sample |
|---|---|---|
| Release MBID | `id` | 100/100 |
| Title | `title` | 100/100 |
| Artist name | `artist-credit[]` (name + joinphrase) | 100/100 |
| Release date | `date` — already `YYYY` / `YYYY-MM` / `YYYY-MM-DD` | **100/100** |
| Catalog number | `label-info[].catalog-number` | present |
| Spotify/Apple/Bandcamp | `relations[]` (url-rels) | 84/100 have ≥1 |
| Cover art exists? | `cover-art-archive.front` | 60/100 true |

Two bonuses worth calling out:

- **`cover-art-archive.front` is free.** You currently emit a `coverartarchive.org/.../front-500`
  URL for every release and rely on `onerror` to hide the broken ones — 40% of them. Caching this
  boolean kills the broken-image flashes with zero extra requests.
- **`date` arrives pre-resolved.** The API already merges `release_country` /
  `release_unknown_country` for you. The whole `earliest_country` CTE in
  [controllers.py:70-90](../apps/newfire/controllers.py#L70-L90) disappears. Stored as TEXT,
  partial dates sort chronologically under plain lexicographic `ORDER BY date DESC`.

### Search

`GET /label?query=hyperdub&limit=5` → **0.41s, 2 KB**, with relevance scores, `type`, `area`, and
`disambiguation`. Disambiguation matters: there are **six** distinct labels named "Domino" in the
mirror.

### Rate limiting — two corrections to the premise

The response headers report `X-RateLimit-Limit: 1200` with a **~1 second** reset window, and the
remaining count fluctuated (476 → 231) between my calls without me making 1000 requests. **That
counter is MusicBrainz's global load-shedding bucket, not your personal allowance.** It will not
tell you when *you* are over your limit.

Short bursts are technically tolerated (3 back-to-back requests returned 200 in 1.35s), but the
documented policy is an average of 1 req/sec per source, enforced by blocking abusive IPs and
User-Agents. Self-enforce 1 req/sec; a descriptive `User-Agent` with contact info is mandatory.

**The second correction is more practical: 1 req/sec is not actually reliable.** Sustained paging at
1.1s intervals drew a 503 twice in 13 requests. Effective throughput on a long crawl is closer to
**2.2s/request** once backoff is included. Budget for that, not for 1/sec.

### Storage

I normalized the real 100-release API response into the proposed schema and measured the resulting
SQLite file: **76 KB**, i.e. 778 bytes/release. That is a conservative upper bound — a 100-row
database is dominated by page and index overhead, so the marginal cost at scale is lower.

| Cache contents | Size |
|---|---|
| Drag City alone (1,297 releases) | **~1 MB** |
| 50 Drag-City-sized labels (~50,000 releases) | **~39 MB** |
| 100 large labels (~150,000 releases) | ~110 MB |

Even modelling *every* tracked label as a big one, the cache stays in the tens of megabytes against
a 64 GB mirror. **Storage was never the constraint, and the corrected label sizes don't threaten
it** — sync time was the real question, and §4.4 addresses that.

### 2.5 Corrections to the first draft

Recording these explicitly, since two of the original claims did not survive contact with Drag City.

| Original claim | Verdict | Reality |
|---|---|---|
| "A single API request returns everything the label page renders" | **Half wrong** | True for *fields* (no N+1 fan-out); false for *releases*. `limit=100` is a hard cap — Drag City needs 13 requests. |
| "Labels are tiny: p50=2, p90=19, so one request covers 90%" | **Wrong population** | Correct arithmetic over an irrelevant denominator. Tracked labels are p99+ (1,000–3,000 releases). Dropped as a justification. |
| "Cold miss costs ~1.3s" | **Wrong for real labels** | 28.4s for Drag City. Mitigated by moving it off the request path and seeding from the mirror — not by pretending it's fast. |
| "The API's `date` replaces the CTE" | **Confirmed** | 1,281/1,281 exact match including nulls. |
| "Backoff needed for 503s" | **Understated** | Not an edge case: 2 throttles in 13 paced requests. |

---

## 3. Options compared

| | Latency (cached) | Cold cost for Drag City | Monthly hosting | Ops burden |
|---|---|---|---|---|
| **A. Live API only** | n/a | ~2.2s **on every page view**, forever | $5 | none |
| **B. Postgres mirror on VPS** | ~20ms | ~20ms | **$25–80** | replication, schema migrations, backups |
| **C. SQLite cache + API fill** ✅ | ~5ms | 28s **once**, in the background — or 0s if seeded | **$5** | one background job |
| **D. Async JS fan-out** | n/a | ~2.2s/page, still serialized | $5 | rate limit unchanged; logic leaves Python |

The Drag City column is the honest comparison. Option C's 28 seconds looks bad next to B's 20ms
until you notice it is paid **once, off the request path, and never again** — whereas Option A pays
2.2s on every single page view of a label, forever.

**Option B costs real money for data you will never look at.** You would pay to host 5.4M releases
across 333K labels to serve a friend group that will touch a few hundred labels. A 64 GB database
needs ~100 GB provisioned disk (WAL, vacuum headroom, index rebuilds) and 4–8 GB RAM — that is
roughly €15–25/mo on Hetzner, $40–80/mo on DigitalOcean or Linode, *plus* keeping replication
running and handling the schema changes MusicBrainz ships periodically.

Option D was your fallback, and it is worth being explicit about why it loses: parallel JS requests
do not raise the rate limit. Six concurrent browser fetches to MusicBrainz are still six requests
against a 1 req/sec budget — you would just be violating the policy from six sockets instead of
one, and moving the fetch logic out of Python to do it.

**Option C is the only one that is both fast and cheap**, and it stays entirely server-side Python.

One more datapoint in its favour: the API reported `release-count: 474` for Hyperdub while your
mirror has 470. **Your mirror is already stale.** The API is the fresher source, so caching from it
is not a downgrade in data quality.

---

## 4. Recommended architecture

### 4.1 The source abstraction — the keystone

You already own a complete local copy of MusicBrainz. Do not throw that asset away; make it a
*second implementation of the same interface*.

```python
class MBSource(Protocol):
    def get_label(self, gid) -> dict | None: ...
    def browse_releases_by_label(self, gid, limit, offset) -> tuple[list[dict], int]: ...
    def search_labels(self, query, limit) -> list[dict]: ...

class WebServiceSource(MBSource):     # production: rate-limited HTTP
class PostgresMirrorSource(MBSource): # dev + bulk seeding: your 64 GB mirror, no rate limit
```

Both return **identically-shaped normalized dicts**. One cache-writer consumes either. This buys
three things:

1. **Development is fast and free.** Point at Postgres locally; never wait on a rate limiter while
   iterating.
2. **Cold start is solved.** Seed the production cache from the mirror offline — build
   `mbcache.db` on your Mac, ship the file to the VPS. Your friends' first visit hits a warm cache,
   at zero API cost.
3. **The two implementations validate each other.** Same label through both sources should produce
   the same normalized dict. That is your test suite, and it is a genuinely strong one.

### 4.2 Schema

Separate SQLite file (`databases/mbcache.db`), third DAL alongside `db` and `mb`. Rationale: it is
disposable, rebuildable, ships as a prewarmed artifact, and has a different backup policy from user
data. Cost: no cross-DB joins — but joining a few dozen tracked labels in Python is nothing.

**Critical: key everything on MBIDs, never MusicBrainz internal integer IDs.** Your current code
uses `label.id`, `rl.release` etc. Those integers are assigned per-dump and are **not stable across
mirror rebuilds** — and the API does not expose them at all. This is the main reason the cache
schema must diverge from your current query shape.

```
mb_label
  gid TEXT UNIQUE           -- MBID, the natural key
  name, sort_name
  disambiguation            -- required: six labels are named "Domino"
  label_type, area_name, label_code
  fetched_at

mb_release
  gid TEXT UNIQUE
  title
  artist_credit TEXT        -- flattened display string (name+joinphrase concatenation)
  artist_gid TEXT           -- primary artist MBID, for future artist pages
  date TEXT                 -- 'YYYY' | 'YYYY-MM' | 'YYYY-MM-DD', sorts correctly as text
  country, status, disambiguation
  has_front_cover INTEGER   -- free from cover-art-archive.front
  fetched_at

mb_release_label            -- release↔label join, by MBID
  release_gid, label_gid, catalog_number
  UNIQUE(release_gid, label_gid, catalog_number)

mb_release_url
  release_gid, service, url, rel_type
  -- service: 'spotify' | 'apple_music' | 'bandcamp' | ...

mb_sync_state               -- see 4.3; the heart of the design
mb_search_cache             -- query_norm, entity_type, result_gids JSON, fetched_at
```

Indexes: `mb_release_label(label_gid)`, `mb_release_url(release_gid)`, `mb_release(date)`,
`mb_label(name)`.

The label page query collapses from the 20-line CTE in `controllers.py` to roughly:

```sql
SELECT r.*, rl.catalog_number FROM mb_release r
JOIN mb_release_label rl ON rl.release_gid = r.gid
WHERE rl.label_gid = ? ORDER BY r.date DESC LIMIT ? OFFSET ?
```

### 4.3 Cache completeness — the part that is easy to get wrong

Caching individual releases is trivial. The hard question is **"do I have *all* of this label's
releases?"** A row-level cache cannot answer it — 20 cached releases for a 300-release label looks
identical to a complete 20-release label. You need collection-level state:

```
mb_sync_state
  label_gid TEXT UNIQUE
  status              -- 'never' | 'partial' | 'complete' | 'error'
  release_count_remote  -- from the API's release-count
  release_count_local
  last_full_sync_at, last_checked_at
  error_message, error_count
```

#### The browse endpoint is not sorted — a partial cache cannot be date-sorted

I assumed a cold miss could fetch API page 1, render it, and backfill the rest. **Tested, and it is
wrong.** The browse endpoint returns releases in an undefined order (effectively internal insertion
order, oldest-added first). Drag City's API page 1 looks like this:

```
1995-10     O How I Enjoy The Light
1994-01-17  Twin Infinitives
1994        Horses / Stable Will
1991-08-23  Summer Babe
...
```

Against the truly newest 20 releases in the label, **the overlap is 0/20**. The newest records
(2026-08-28, 2026-07-13, …) sit near offset 1200.

Since [label.html](../apps/newfire/templates/label.html) presents releases newest-first, rendering
a date-sorted page from a partially-synced label would confidently show 1990s records as the latest
Drag City releases. **`sync_state` is therefore a correctness guard, not just an optimisation: never
serve date-sorted pagination unless `status == 'complete'`.**

Read path:

| State | Behaviour | API cost |
|---|---|---|
| `complete`, fresh | Serve from cache | **0** |
| `complete`, stale | **Serve stale immediately**, enqueue background refresh | 0 on the request path |
| `partial` / `never` | Enqueue full sync; render a "syncing" state, optionally with a fast recent-releases preview via the search endpoint (1 request, ~0.7s) | 0–1 |
| `error`, recent | Serve what exists + a warning; do not retry-storm | 0 |

Two consequences worth being explicit about:

- **A cold label cannot be rendered correctly and instantly.** Either the user sees a "syncing"
  state for ~30s, or a search-backed preview of recent releases that is *labelled* as partial.
  Pretending otherwise means showing wrong data.
- **This is the strongest argument for seeding from your mirror** (§4.1). Seeded labels are born
  `complete` and never expose this state at all.

Stale-while-revalidate remains the key property: **once a label is complete, a user never waits
again**, even during a refresh. The design is also inherently resumable — an interrupted sync
leaves `partial` and the next run continues, which matters because the scheduler forks processes
that can die mid-run.

### 4.4 Incremental sync — what makes big labels affordable

This is the section that carries the design now that label sizes are known to be large. A 13-page
re-crawl of Drag City every night would be indefensible. It isn't necessary.

**Step 1 — change detection, 1 request.** `GET /release?label=X&limit=1` returns `release-count`
in ~0.4s. If it equals `release_count_local`, nothing was added or removed — **stop**. An unchanged
label costs exactly one request no matter how many releases it has.

**Step 2 — fetch only what's new, 1 request.** When the count *has* moved, do not re-page. The
search endpoint accepts a date range, which I verified against Drag City:

```
GET /ws/2/release?query=label:"Drag City" AND date:[2025 TO 2026]&limit=100
  -> 0.69s, 66 KB, 54 releases, in ONE request
```

Returns `date`, `title`, `artist-credit`, `label-info[].catalog-number`, `release-events`,
`track-count`. Two verified caveats:

- **`labelid:<mbid>` is not a supported search field** — it silently returns `count: 0`. You must
  query by label *name*, which is ambiguous (six labels named "Domino").
- **Mitigation, confirmed working:** search hits *do* include `label-info[].label.id`. Post-filter
  on the exact MBID. In the Drag City test this correctly kept 53 of 54 hits and dropped one
  same-name impostor.
- **Search results carry no `relations`** — no streaming URLs. Fetch those for the handful of
  genuinely new releases only.

**Resulting cost per tracked label per night:**

| Case | Requests | Time |
|---|---|---|
| Unchanged (the common case) | 1 | ~0.4s |
| A few new releases | ~3 | ~5s |
| First-ever sync | 13 (Drag City) | 28s — or **0**, seeded from the mirror |

Fifty tracked Drag-City-sized labels, mostly unchanged: **~50–70 requests, about two minutes.**
That is the result that makes the tracking feature viable, and it is independent of label size —
which is precisely the property the release-count distribution failed to provide.

Caveat: a count-neutral edit (a release retitled, a Bandcamp link added, one added and one removed)
is invisible to the count check. Mitigate with a staggered full re-page per label every ~30 days —
amortised, that is one 13-request crawl per label per month.

### 4.5 Rate limiting — a real gotcha

**pydal's scheduler runs each task in a forked process** (`pydal/tools/scheduler.py` uses `os.fork`,
`max_concurrent_runs` defaults to 2). Your web process will also make API calls on the cache-miss
path. So **at least two processes will want to call MusicBrainz concurrently, and a module-global
token bucket with a `threading.Lock` will not work** — each process gets its own copy and you
silently double your rate.

The limiter must be **cross-process**. Simplest correct approach: a single-row table in the cache
DB holding `last_request_at`, updated inside an immediate transaction; a caller that cannot claim
the slot sleeps until it can. Combine with:

- `SCHEDULER_MAX_CONCURRENT_RUNS = 1` (already the default in your `settings.py`)
- Respect `Retry-After` on 503, exponential backoff
- Mandatory descriptive `User-Agent` with contact address
- A hard per-request timeout so a hung fetch cannot block a page render

### 4.6 SQLite concurrency

Background writer + web readers on one file needs:

- `PRAGMA journal_mode=WAL` — readers never block on the writer
- `PRAGMA busy_timeout=5000`
- Keep **one writer** (the scheduler); the web process writes only on the cold-miss path, in a
  short transaction

pydal supports this via the `after_connection` callable on `DAL(...)`
(confirmed in `pydal/base.py:439`), so the pragmas can be set declaratively at connection time.

### 4.7 Search

Be clear-eyed: **the cache can never fully serve search.** It only knows labels it has already
seen, and MusicBrainz search is a Lucene index you cannot replicate in SQLite. So:

1. Query local `mb_label` first → instant results for anything seen before
2. Also hit `/label?query=...` (0.41s) and merge, upserting the results into `mb_label`
3. Cache query→results in `mb_search_cache` with a TTL (~7 days)

For a small friend group with overlapping taste, the popular-label set converges fast and most
searches become cache hits. SQLite FTS5 for local search is a possible later refinement (needs raw
`executesql`; pydal does not manage virtual tables).

### 4.8 Background sync

Use the pydal scheduler already wired into [common.py:239-245](../apps/newfire/common.py#L239-L245)
— flip `USE_SCHEDULER = True`. `enqueue_run` supports a `period` argument, so the nightly job is
native. Two tasks:

- `sync_label(label_gid, full=False)` — enqueued on cache miss, and by the nightly job
- `sync_tracked_labels()` — periodic; count-check each tracked label, enqueue full syncs for
  changed ones

For the cold-miss "warming up" state, a plain `<meta http-equiv="refresh">` or a small poll is
fine — the fetching logic stays in Python, which is what you actually care about.

---

## 5. Risks and honest downsides

| Risk | Severity | Mitigation |
|---|---|---|
| **Loses whole-database queries** | **Highest strategic risk** | Any future feature like "most active labels of 2024" or genre rollups cannot be served by a cache of a few hundred labels. **Keep the local mirror as a dev asset.** |
| **Cold label is unrenderable for ~30s** | **High** | Real, unavoidable via the API alone (browse is unsorted, §4.3). Seed from the mirror so tracked labels are never cold; show an explicit syncing state otherwise. |
| Big labels are slow to fully cache | Medium | 28s for Drag City, off the request path; incremental sync (§4.4) means it happens once |
| 503 throttling mid-crawl | Medium | Observed 2× in 13 requests. Backoff + resumable `partial` state are mandatory, not optional |
| Rate limiter must be cross-process | Medium | See 4.5 — pydal forks scheduler tasks |
| Search-by-name ambiguity in incremental sync | Medium | `labelid:` unsupported; post-filter on `label-info[].label.id` (verified: 53/54 correct) |
| Stale data between syncs | Low | TTL + count check; your mirror is already 16 releases behind on Drag City alone |
| Rewriting the label page SQL | Low | Real work, but the query gets *simpler* |
| Losing internal-integer-ID joins | Medium | Deliberate: MBID-keyed from day one |
| Scheduler dies mid-sync | Low | `sync_state` makes syncs resumable and idempotent |

---

## 6. Phased implementation

Each phase is independently useful and leaves the app working.

| Phase | Work | Outcome |
|---|---|---|
| **0** | `MBSource` protocol + normalized dict shape + `PostgresMirrorSource` | Differential test harness; no behaviour change |
| **1** | Cache schema, DAL #3, WAL pragmas, upsert writer | Cache exists, nothing reads it |
| **2** | Rewrite label page to read cache; cold miss → sync page 1 via Postgres source | Page served from cache, still no API dependency |
| **3** | `WebServiceSource` + cross-process rate limiter + backoff | Prod-capable; swap source by config |
| **4** | **Seed script (mirror → `mbcache.db`)** — moved up; big labels make cold start painful | Tracked labels born `complete` |
| **5** | Enable scheduler; `sync_label` background full-fetch; stale-while-revalidate | No user ever waits twice |
| **6** | Tracked-labels table + nightly count-check + search-by-date incremental sync (§4.4) | The tracking feature |
| **7** | Label search: local + WS merge + query cache | Search |

Phases 0–2 need no API access at all and are pure refactoring against data you already have.
**Phase 4 moved ahead of the background sync**: with labels at Drag City scale, seeding is what
makes the cold-start problem disappear rather than merely deferring it.

All seven are done. What followed is outside this study's scope but worth recording: search and
the tracked-labels feed existed as routes with nothing linking to them, and the home page was
still the scaffold's. Wiring up navigation and a front door came next.

---

## 6.5 Findings from implementing phases 0–1

Four things surfaced while building against real data that the study hadn't predicted.

**The `earliest_country` CTE is the real performance problem — in production, today.**
It is uncorrelated with the label filter, so Postgres sorts and deduplicates the entire
multi-million-row `release_country` table on every request. Measured against the mirror: **29s per
page**, and putting `LIMIT/OFFSET` on the detail query made page 13 take **284s**. Rewritten as a
LATERAL join against a pre-resolved page of release ids, the same query runs in **0.29s**, flat
across all pages. The live `/label/<gid>` page still carries the original CTE and takes **4.36s** to
render — this is not only a seeding concern.

**The label page's release total is wrong.** It counts `release_label` rows, but a release may be
listed more than once under one label with different catalog-number formattings (`DC-173-CD`
alongside `DC173CD`). Drag City reports 1,287 where the true release count is 1,281 — so the last
page of pagination is partly empty. The cache counts distinct releases, matching what the web
service reports. Phase 2 inherits the fix.

**A URL carries several relationship types at once.** A Bandcamp album is typically both
"purchase for download" and "free streaming". Storing them naively produces duplicate rows that
differ only in a field nothing renders; the cache keeps one row per URL, preferring the most
listen-like type.

**`cover_art_archive.index_listing` is unusable against a mirror.** It is a view, it is slow, and
its `approved` flag is unpopulated — filtering on it reports zero front covers for every release.
Querying the `cover_art` base tables directly gives the expected ~62% coverage.

Verified after seeding: **1,281/1,281 exact matches against the captured API data for date, artist
credit and title**, zero duplicate rows, and a re-seed that is a clean no-op. The three catalog
numbers that differ are genuine editor reformatting since the dump, not transformation errors.
Seeding Drag City from the mirror takes **2.1s** against 28.4s over the API, and the cache file is
**0.94 MB** — close to the 1 MB estimate.

---

## 6.6 Findings from running it — the mirror is the stale one

Two things the study got directionally right and materially understated.

**"Your mirror is already stale" was the understatement of the document.** §3 noted the mirror was
4 releases behind on Hyperdub and §5 rated stale-data a *low* risk. In practice the mirror's
`replication_control.last_replication_date` is **2026-04-01** and there is no `cron` service in the
musicbrainz-docker stack, so it has never replicated since the dump was loaded. Measured against
the live service: Drag City 1,281 cached against 1,300 remote, Warp 2,464 against 2,526, Ghostly
792 against 806. Every followed label looked frozen in spring, and *the cache was not at fault* —
it matched the mirror exactly, label counts and newest dates alike. §4.1 already says the mirror is
"dev + bulk seeding" and the web service is "production"; `MB_SOURCE` had simply never been flipped
after phase 3 made it possible. The lesson is that a config default is a decision, and this one
silently reverted the whole quality argument for caching from the API.

**Releases are the wrong unit for a page about records.** MusicBrainz models the CD, the LP and the
digital release of one record as separate releases sharing a *release group*, and the study's
schema (§4.2) has no column for it. That made the label page mostly repetition — across the three
followed labels 4,535 releases collapse to 2,570 groups, and Warp alone goes from 2,464 rows to
1,215. Aphex Twin's "Richard D. James Album" occupied sixteen. `mb_release` now carries
`release_group_gid` and the readers group on it, falling back to the release's own gid so a row
cached before the column existed is a group of one rather than merging with every other ungrouped
release. Collapsing also *recovers* data rather than hiding it: streaming links are merged across
the group, so a card shows Spotify even when only the digital edition carries the relationship.

**The web service is not a drop-in for the mirror inside a forked child.** Switching `MB_SOURCE`
turned every scheduler task into a segfault. `requests` consults the environment for proxies on
each request; on macOS that reaches `_scproxy` → `SCDynamicStoreCopyProxiesWithOptions` →
CoreFoundation, and CoreFoundation is not fork-safe. pydal runs each task in a forked child, so the
first request died with SIGSEGV in CFPreferences before doing any work — and because the label page
retries every ten seconds, one follow became a crash every ten seconds. `trust_env = False` on the
session avoids the lookup entirely. This is the same family as the `gssencmode` workaround §4.1
already needed for psycopg in a forked child, and the general lesson is that **the source
abstraction is clean at the API boundary but not at the process boundary**: each implementation
drags in its own fork hazards, and swapping sources means re-testing the scheduler, not just the
pages.

Two smaller things fell out of that crash loop. `sync_label` only ever added and updated, so
releases MusicBrainz had stopped listing under a label kept their links forever — moving off the
stale mirror left Warp and Ghostly four releases *above* remote, which the sweep reads as "out of
date" and answers with a full re-crawl every night, permanently. And a worker killed between
claiming a run and forking its child strands the row: pydal only re-queues rows matching its own
name, `next_run()` only considers rows with no worker at all, and the name is an IP address, so the
same machine will not recognise its own work after a DHCP change. Both are fixed; the second is the
failure mode `scheduling.py` was written for, one status value short.

---

## 6.7 Seeding for a public deployment

Phase 4's seed script exists now as `scripts/seed_cache.py`, and the shape of the
problem is narrower than the plan assumed. **Seeding everything is not on the
table**: 278,345 labels carry 4.5M releases, which at the measured ~0.72 KB per
release is 2–3 GB. Even "labels with 100+ releases" is 1.9 GB, because the size
is dominated by majors nobody follows — Polydor alone is 26,348 releases.

So the seed is a chosen list. Measured, building one from the mirror:

| | |
|---|---|
| 54 labels (the followed set plus a hand-picked starter list) | **48,056 releases** |
| Time from the mirror | **116 s** |
| Same catalogue over the web service | ~480 requests, ~9 min, all of it against MusicBrainz |
| Resulting `mbcache.db` | **35.4 MB** |

Two things worth recording. **Ranking labels automatically does not work.** An
obvious "seed the most active labels" query returns lofi aggregators, ASMR
farms, and an account named `user-177606669`, because release count measures
upload rate rather than whether anyone wants the records. Requiring an IFPI
label code, external URL relationships, and a catalogue spread across many
artists rather than a few aliases fixes the garbage but still only yields
*legitimate* labels, not ones to anyone's taste — it skews to dance and
classical, where the volume is. The shipped starter list is editorial, and
`--top` prints its picks for review rather than pretending otherwise.

**Seed cold, then let the app catch up.** `sync_label` prunes links its source
no longer lists, and the mirror lags the web service, so re-seeding over a cache
the app has already brought up to date rolls it back to the mirror's view —
observed here, where a mirror re-seed took Light in the Attic from 388 releases
back to 383 and dropped the 2026 ones. That is the prune working correctly, and
it makes seeding a cold-start step rather than a refresh mechanism.

---

## 7. Open questions

1. **Same SQLite file or separate?** I recommend separate (`mbcache.db`) for disposability and
   prewarm-shipping. At 7 MB it could just as well live in `storage.db` — your call on operational
   preference over purity.
2. **Cache TTL?** Suggest 7 days for label release lists, 30 days for full re-page. Tracked labels
   refresh nightly regardless.
3. **Does the mirror stay?** Recommend yes, as a local dev + seeding asset — but see §6.6: an
   unreplicated mirror is a *silently* stale one, and it should never be the production source. It
   costs nothing where it is, and it is your only route to whole-database queries.
4. **Does anything need whole-database queries?** The single question that could invalidate this
   plan. If a future feature needs to query across all of MusicBrainz rather than a tracked subset,
   revisit Option B.
5. **Track releases too, or only labels?** Current scope is labels; the schema supports
   artist-level tracking later via `artist_gid`.

---

## Appendix — reproducing the measurements

Probe scripts are in the session scratchpad: `probe.py` / `probe2.py` (mirror distribution),
`api_probe*.py` (response shape, rate-limit headers), `dragcity2.py` (full 13-page crawl with
backoff), `datediff.py` (CTE vs API date comparison), `searchtest2.py` (incremental sync),
`size_test.py` (storage). All calls used a descriptive User-Agent with a contact address and paced
at ≥1s. Total API requests: ~30, of which 13 were the one-off Drag City crawl.

Key reproducible results:

- Drag City full crawl: 13 requests, 2× 503, 2.7 MB, 28.4s
- Date comparison: 1,281/1,281 exact match vs the `earliest_country` CTE
- Browse ordering: 0/20 overlap between API page 1 and the newest 20 releases
- Incremental: `label:"Drag City" AND date:[2025 TO 2026]` → 54 hits, 1 request, 0.69s
- `labelid:<mbid>` returns `count: 0` — unsupported field, do not use
