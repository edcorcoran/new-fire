# Deploying new fire

Written against the DreamHost VPS this runs on (Ubuntu 22.04, Python 3.10.12).
Three things about that host shape everything below:

- **No root.** `newfire_admin` has no sudo, and neither does any other account
  on the box -- DreamHost grants it on Dedicated and DreamCompute, not VPS. So
  no `apt`, no systemd unit, no editing anything under `/etc`, and no installing
  the `python3-venv` package that a stock `python3 -m venv` needs -- which is
  why step 1 builds the virtualenv the long way round.
- **No Passenger.** DreamHost withdrew it. The supported way to run a Python app
  is now their **Proxy Server** -- Apache `mod_proxy` in front of a process you
  keep alive yourself on a high port. Some DreamHost KB pages still describe the
  Passenger checkbox; they are stale, and it is not in the panel.
- **Chef manages the box.** `chef-client.service` runs periodically and owns the
  Apache and nginx configuration, which is templated per-VPS
  (`nginx@apache2-<vps>.service`). Hand-edited system config gets reverted
  eventually, silently, and at a moment unrelated to when you edited it. So
  everything here lives in `$HOME`, and the vhost is configured through the
  DreamHost panel — which feeds Chef rather than fighting it.

## The shape

One long-lived process, and cron to keep it alive.

```
  the internet ──443──> Apache mod_proxy ────> 0.0.0.0:8123
                        (DreamHost panel,      deploy/serve.py
                         terminates TLS)        web + scheduler
                              │                       ^
                     dials the server's       cron + flock ┘
                     public IP, never         restarts it if it dies
                     127.0.0.1
```

**The port is open to the internet, and that is not a choice.** DreamHost's
Proxy Server connects to the server's public address rather than to loopback, so
a backend bound to `127.0.0.1` gets `Connection refused` and the domain answers
503 -- which is the entire failure, and it looks like a misconfigured proxy for
as long as you believe the docs that say to bind loopback. Nor is there a
firewall in front of the port: a connection from outside is refused only while
nothing is listening.

So the request is turned away in the application instead of at the socket.
`mod_proxy_http` stamps `X-Forwarded-For` and its siblings onto everything it
forwards, so anything arriving without them did not come through the proxy, and
`deploy/serve.py` answers it 403 before py4web sees it. That is a weaker
guarantee than a closed port -- the headers can be forged by anyone who knows
the address -- but it covers what actually finds an open port: scanners,
crawlers, and anyone who reads the address off a DNS record.

Because we own this process rather than having it spawned on demand, the
scheduler runs **inside it**, as it does in development -- the arrangement the
app was designed for, and the one a full label sync was tested under.
`RUN_SCHEDULER` and `deploy/worker.sh` still exist if you ever want the queue in
its own process; nothing here needs them.

The web tier is `deploy/serve.py`, which `deploy/serve.sh` execs and cron runs.
It is plain `py4web run` -- what development uses -- plus the guard above and one
more wrapper: behind a TLS-terminating proxy the app sees plain HTTP, which would
cost the session cookie its `Secure` flag, so serve.py sets `X-Forwarded-Proto`
before py4web looks at the scheme. Assuming HTTPS is safe there because the guard
ran first, and nothing reaches the app except through the proxy that holds the
certificate. Step 6 verifies both.

## Server layout

```
~/newfire/                       git checkout; nothing here is web-served
  apps/newfire/                    the app
  apps/_default -> newfire         tracked symlink; mounts the app at "/"
  apps/newfire/databases/          storage.db, mbcache.db — NOT in git
  deploy/serve.sh                  what cron runs; sets host/port, execs:
  deploy/serve.py                  the web tier -- proxy guard, then py4web
~/newfire-venv/                  virtualenv on the system Python 3.10
~/newfire-logs/newfire.log       server output; NOT ~/logs, see step 5
```

Apache serves no files from the checkout at all -- every request is proxied to
the process, which serves its own `/static/...`. That is why the usual proxy
warning about static assets failing to load does not apply here: there is no
split between files the web server serves and routes the app serves, because
the web server serves none of them.

## One app, once

`apps/_default` is a symlink to `newfire`, and py4web enumerates its apps folder
with `os.listdir`. Both names look like apps, so an unqualified `py4web run apps`
imports the same code **twice**, as two unrelated modules, each starting its own
scheduler. Both schedulers then share a worker identity — pydal derives it from
the host IP (`socket.gethostbyname(socket.gethostname())`) — and `step()` begins
each pass by re-queueing every "assigned" run bearing its own worker name. Two
loops with one name will therefore re-queue each other's just-claimed runs.

Naming the app explicitly is the fix, and it is why `serve.py` passes
`app_names="_default"` and `worker.sh` calls `_default.worker.run_worker`.
Do the same when running it locally:

```bash
.venv/bin/py4web run apps --app_names _default --port 8123
```

The server does not run this command -- it runs `deploy/serve.py`, which passes
the same `app_names="_default"` to py4web's WSGI entry point. Both listen on
8123 by default; the difference is the interface. Development stays on loopback,
where nothing else can reach it. The VPS binds every interface because the proxy
insists on the public one, and pays for that with the 403 guard described above.

## First deploy

**1. Check out and build the virtualenv.** The system Python is fine — the whole
dependency stack declares `>=3.9` and nothing in this app uses a newer stdlib
API. Do not build a Python from source.

Plain `python3 -m venv ~/newfire-venv` fails on this host:

```
The virtual environment was not created successfully because ensurepip is not
available.  On Debian/Ubuntu systems, you need to install the python3-venv
package [...]
```

Debian and Ubuntu split `ensurepip` and its bundled wheels out of the stdlib
into a separate `python3.10-venv` package, and the advice in that message is an
`apt` you do not have. The `venv` module itself is present — only its pip
bootstrap is missing — so build the environment without pip and bootstrap pip
into it afterwards, which needs nothing but the network:

```bash
git clone https://github.com/edcorcoran/new-fire.git ~/newfire

rm -rf ~/newfire-venv          # a failed `python3 -m venv` leaves a stub behind
python3 -m venv --without-pip ~/newfire-venv
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
~/newfire-venv/bin/python /tmp/get-pip.py
~/newfire-venv/bin/pip install setuptools wheel
~/newfire-venv/bin/pip install py4web
```

`get-pip.py` carries a pip wheel inside itself, so it has nothing to bootstrap
from and installs a current pip — no `pip install --upgrade pip` afterwards.
`setuptools` and `wheel` are named explicitly because `--without-pip` skips
those too, and a dependency that ships no wheel for this platform will want
them.

Debian's `virtualenv` is also on the system path, and it seeds pip from its own
wheels rather than from `ensurepip`, so `python3 -m virtualenv ~/newfire-venv`
is a one-line substitute for the four above — *when* the `python3-pip-whl` and
`python3-setuptools-whl` packages happen to be installed beside it. They are not
guaranteed to be, and you cannot add them either. The route above does not care,
so prefer it. (`python3 -m pip install --user virtualenv` is not a way out of
that: pip sees the system copy, reports "Requirement already satisfied",
installs nothing, and leaves you with no `~/.local/bin/virtualenv`.)

`psycopg2-binary` is deliberately absent. It exists only to read a local
MusicBrainz mirror, and this host has neither a mirror nor a route to yours.
`factory.py` does import `PostgresMirrorSource` unconditionally, but that module
reaches Postgres through pydal rather than importing the driver itself, and
`build_mb_runtime` only builds a mirror connection when `MB_SOURCE` is
`postgres` -- so with `webservice` the driver is never loaded. Verified against
an interpreter with no psycopg2 installed: pages render, and the task runtime
builds a `WebServiceSource` with `psycopg2` absent from `sys.modules`.

The consequence is that **seeding cannot be run on this host**, which is fine,
because the mirror it would seed from is not reachable from here either. See
step 3.

A plain `pip install py4web` takes the current release, which is newer than the
one in your development virtualenv. The app runs correctly on both — that is
what the check above was run against — but it does mean the two environments
differ. To make them match, read the version off your Mac:

```bash
.venv/bin/pip show py4web | awk '/^Version:/{print $2}'    # local
```

and pin the server to it:

```bash
~/newfire-venv/bin/pip install "py4web==<that version>"    # on the VPS
```

**2. Write `settings_private.py`.** It is gitignored, so the clone does not
carry it -- and without it the app does not start at all. `MB_SOURCE` defaults
to `webservice`, `MB_USER_AGENT_CONTACT` defaults to `None`, and `common.py`
builds the MusicBrainz runtime at import time, so the missing contact raises
before a single route is registered. What you see is not an error page: py4web
logs the traceback, prints `[FAILED] loading _default`, and then serves 404 for
every path. See step 5.

```bash
cat > ~/newfire/apps/newfire/settings_private.py <<'EOF'
# No mirror on this host; the live web service is the source.
MB_SOURCE = "webservice"
MB_USER_AGENT_CONTACT = "https://newfire.music"

# No SMTP configured yet — see "Before you announce it".
VERIFY_EMAIL = False
LOGIN_AFTER_REGISTRATION = True
EOF
```

Leave `MB_DB_URI` out. The value in the development copy is a LAN address this
host cannot reach, and pointing the app at an unreachable mirror fails at sync
time rather than at startup.

**3. Seed the cache.** From your Mac, against the mirror, and *before* the
app has ever run on the server — a sync prunes releases its source no longer
lists, and the mirror lags the live service, so seeding over a warmed cache
rolls it back.

```bash
# local
export MB_DB_URI="postgres://musicbrainz:musicbrainz@your-mirror:5432/musicbrainz_db"
.venv/bin/python scripts/seed_cache.py --labels scripts/seed_labels.txt --out ./seed
rsync -avz seed/mbcache.db seed/*.table \
    newfire_admin@VPS-HOSTNAME:~/newfire/apps/newfire/databases/
```

`VPS-HOSTNAME` is the server's fully qualified name, the one the DreamHost panel
shows under **Servers & Usage** -- of the form `vpsNNNNN.dreamhostps.com`. It is
*not* the short name in the shell prompt once you are logged in; that resolves
nowhere from outside the box, and `ssh: Could not resolve hostname` is what
using it looks like.

**Copy the `.table` files, not just the database.** `seed_cache.py` leaves pydal
migration metadata beside `mbcache.db` -- one `<hash>_<table>.table` file per
table -- and that metadata is the only way pydal knows these tables already
exist. Ship the database alone and the app opens what it takes to be an empty
database, issues `CREATE TABLE mb_label`, and dies at import with
`sqlite3.OperationalError: table "mb_label" already exists`, which arrives
looking exactly like the 404 in step 2. The hash in those filenames derives from
the database URI rather than the directory holding it, so the files are portable
as they stand.

Do not copy `seed/` wholesale, though: it also holds `mbcache.db-wal` and
`mbcache.db-shm` from the seeding run, and those belong to a database that is no
longer running.

`seed_cache.py` writes to `./seed`, never into `apps/newfire/databases` -- the
cache the app is using is the one thing that must not be seeded over. `seed/` is
gitignored, so the build artifact stays out of the repo and off the server
except by the lines above.

About 108 MB for the 636 labels in `scripts/seed_labels.txt`. Skip it and the
first stranger to search waits on MusicBrainz at one request per second.

**4. Point the domain at the process.** `newfire.music` is its own registered
domain rather than a subdomain of one already here, so DNS comes first: point it
at this VPS, either by moving its nameservers to DreamHost or by setting an A
record at the apex to the VPS's address. Let's Encrypt validates over HTTP, so a
certificate cannot be issued until that resolves -- attempting it early is the
usual reason this step appears to fail.

In the panel: add the domain as fully hosted on this VPS and enable the free
Let's Encrypt certificate. Then **Servers & Usage -> Manage** next to the server,
scroll to **Proxy Server**, pick `newfire.music` from the URL dropdown, enter
`/` as the path so it covers the whole site, enter **8123**, and click
**Add Proxy**. The form rejects an empty path; `/` is how you say "everything".
Do not narrow it to a subdirectory -- py4web serves its own `/static/...` and
every route from the root, so a prefixed proxy would forward the pages and
leave the assets 404ing at Apache.

The port must match `NEWFIRE_PORT` in step 5 and must be in 8000-65535; 80 and
443 are not available to you, and are not needed -- the proxy applies the
certificate to incoming connections and forwards plain HTTP to this port on the
server's own public address.

**5. Start the server.** Cron is the supervisor, and also the guarantee that
only one server exists. Three parts, and the first two are where this goes
wrong.

**Make a log directory you own.** Not `~/logs`: DreamHost keeps that one for
per-domain Apache logs and ships it `dr-xr-x--- newfire_admin dhapache`. You own
it but have no write bit, so creating a file there fails. `chmod u+w` would work
today -- but that directory is Chef's territory, and the mode can come back. Make
your own instead. Get this wrong and cron says nothing: the shell builds the `>>`
redirect *before* running the command, so the line dies every minute without ever
reaching `serve.sh`, and the complaint goes to a mail spool you will never read.

```bash
mkdir -p ~/newfire-logs
touch ~/newfire-logs/newfire.log && echo ok      # must print ok
```

**Install the crontab.** This is the line:

```cron
* * * * * /usr/bin/flock -n /home/newfire_admin/newfire.lock /home/newfire_admin/newfire/deploy/serve.sh >> /home/newfire_admin/newfire-logs/newfire.log 2>&1
```

Two things *not* to do with it:

- **Do not paste it at a shell prompt.** It is a crontab entry, not a command.
  Bash will try to execute it and fail on the redirect, leaving you with no
  crontab and an error that looks like a permissions problem.
- **Do not run `cron`.** That is the system daemon. It is already running, and
  starting one is root's business, not yours -- `cron: can't open or create
  /var/run/crond.pid: Permission denied` is what the attempt looks like.

What to run instead -- appends the line and needs no editor:

```bash
( crontab -l 2>/dev/null; echo '* * * * * /usr/bin/flock -n /home/newfire_admin/newfire.lock /home/newfire_admin/newfire/deploy/serve.sh >> /home/newfire_admin/newfire-logs/newfire.log 2>&1' ) | crontab -
crontab -l                                       # confirm it registered
```

`crontab -e` opens the same list in an editor if you would rather paste it
there. Either way the entry lives in cron's own storage, not in a file you
manage.

While the server holds the lock each minute's attempt exits immediately. The
first minute after it dies -- crash, reboot, or a deliberate restart -- starts a
replacement. No `@reboot` entry is needed; this covers it.

**Confirm it came up**, and that the proxy reaches it. Cron fires at the top of
the minute, so give it up to 60 seconds before believing any of this:

```bash
pgrep -af "deploy/serve.py"
grep -c FAILED ~/newfire-logs/newfire.log     # on the VPS; must print 0
curl -sI http://127.0.0.1:8123/ | head -1     # on the VPS; expect 403
curl -sI -H 'X-Forwarded-For: 127.0.0.1' \
     http://127.0.0.1:8123/ | head -1         # on the VPS; expect 200
curl -sI https://newfire.music/ | head -1     # from anywhere; expect 200
tail -30 ~/newfire-logs/newfire.log           # on the VPS, when any of those fails
```

The two local curls differ only in a header, and that is the point: 403 without
it and 200 with it means the app is up *and* the guard is working. A 200 to the
bare request means the guard is not running -- see step 6, and treat it as
urgent.

A 503 from the proxy with nothing in `pgrep` means Apache is configured
correctly and the process is not running: the log has the reason.

A **404 from the app itself** -- port 8123 answering, but answering 404 --
means the opposite of what it looks like. py4web is healthy; the app failed to
import, so no routes were ever registered, and every path 404s as though the
site were merely empty. py4web says so once, at startup, and then never again:

```
[FAILED] loading _default (table "mb_label" already exists)
```

That is what `grep -c FAILED` is for. The traceback logged above that line names
the real fault, and in a first deploy it is almost always step 2 or step 3.

**6. Check the guard, then the cookie.** Both are `deploy/serve.py`'s doing, and
both are easier to test from your Mac than to reason about. `VPS-IP` is the
address the domain resolves to.

```bash
curl -sI https://newfire.music/ | head -1                 # expect 200
curl -sI --max-time 10 http://VPS-IP:8123/ | head -1      # expect 403
curl -sI https://newfire.music/auth/login | grep -i '^set-cookie.*session'
```

The first two are the guard seen from both sides: the site answers through the
proxy, and the same port answers 403 to anyone dialling the address directly.

**If the bare address returns a page instead of a 403, stop.** The whole site is
being served unencrypted next to the real one, and every session cookie issued
over it is exposed. Either the guard is off (`NEWFIRE_REQUIRE_PROXY`) or this
proxy does not send the headers `PROXY_HEADERS` in serve.py looks for. Read one
of Apache's forwarded requests in `~/newfire-logs/newfire.log` to see which
headers actually arrive, and widen that tuple to match.

The third should show `HttpOnly`, `SameSite=Lax` and `Secure` together. `Secure`
is the one at risk: py4web takes it from the request scheme, and behind TLS
termination the app sees plain HTTP. serve.py's `trust_proxy` supplies
`X-Forwarded-Proto` so ombott reports HTTPS -- which is only honest because the
guard already turned away everything that did not come through the proxy.

## Deploying an update

```bash
cd ~/newfire && git pull
~/newfire-venv/bin/pip install -U py4web  # only when it moved
pkill -f "deploy/serve.py"                # cron restarts it within a minute
```

There is no reload signal and no restart file: the process imported the old
code, so replacing it is the only way to pick up new code. Expect up to a
minute of downtime while cron notices, and note that killing it also stops any
label sync that was running -- the scheduler returns those runs to the queue on
the next start, so nothing is lost but the work is redone.

### Shipping a re-seeded cache

`git pull` does not carry the cache. It is a gitignored build artifact, so when
`scripts/seed_labels.txt` grows the server keeps serving the old one until the
file itself is copied up. Rebuild it locally, ship it beside the live file, and
swap it in:

```bash
# local, against the mirror
export MB_DB_URI="postgres://musicbrainz:musicbrainz@your-mirror:5432/musicbrainz_db"
.venv/bin/python scripts/seed_cache.py --labels scripts/seed_labels.txt --out ./seed
rsync -avz seed/mbcache.db \
    newfire_admin@VPS-HOSTNAME:~/newfire/apps/newfire/databases/mbcache.db.new
```

```bash
# on the VPS
pkill -f "deploy/serve.py"
cd ~/newfire/apps/newfire/databases
rm -f mbcache.db-wal mbcache.db-shm
mv mbcache.db.new mbcache.db
```

The order is the whole procedure. **Copy alongside, then rename**, so the swap
is one atomic `mv` rather than a minute of rsync writing into a database the app
has open. **Kill first**, because `-wal` and `-shm` belong to the file they were
written against: applying the old WAL to the new database is corruption rather
than a stale read, and they can only be removed once nothing holds the database
open. Then leave it alone -- cron restarts the server within a minute, and that
minute is the entire downtime.

The `.table` files stay put, unlike the first deploy: the schema has not moved,
so the metadata already on the server still describes it. The exception is a
re-seed that follows a schema change, where the new database has columns the
server's metadata does not know about -- ship `seed/*.table` alongside
`mbcache.db.new` in that case, and move them into place in the same step.

A replaced cache is the mirror's view, so every label in it rolls back to
whatever the mirror last replicated, which for a typical mirror is months. That
is a regression, not a loss: `tracked_label` lives in `storage.db`, which none
of this touches, and the nightly sweep re-reads any followed label whose remote
count no longer matches its local one -- so followed labels are current again
within a day, and a seeded label nobody follows refreshes the first time someone
opens it.

## Operating notes

- **Session secret.** `apps/.service/session.secret` is generated on first boot
  and is gitignored. It signs session cookies, so treat it as a credential: do
  not copy the development one up, and do not let it back into git. Replacing it
  logs everyone out and breaks nothing else.
- **Sync pickup takes up to ten seconds.** pydal's scheduler polls on a ten
  second `sleep_time`, so a label queued by a page view waits that long before
  its sync begins. Pages never block on it, so this is latency on a background
  job rather than on a request.
- **Task output goes to `/tmp/scheduler`** (pydal's default). Fine, but it is
  `/tmp` — do not expect yesterday's output to still be there.
- **The cache is disposable, `storage.db` is not.** `tracked_label` is the only
  irreplaceable data on the box. Back that one file up; `mbcache.db` rebuilds
  from either source.

## Before you announce it

- **SMTP is not configured**, so password resets silently cannot work — the form
  accepts the request and no mail is sent. Registration is unaffected because
  `VERIFY_EMAIL = False`. Set `SMTP_SERVER`, `SMTP_LOGIN` and `SMTP_SENDER` in
  `settings_private.py` against your DreamHost mailbox, or accept that a
  forgotten password means a manual fix.
- **Registration is open** to anyone with the URL, with no email verification.
  That is the right default for this app, but it is a decision rather than an
  accident.
