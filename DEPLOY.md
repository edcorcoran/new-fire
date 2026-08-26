# Deploying new fire

Written against the DreamHost VPS this runs on (Ubuntu 22.04, Python 3.10.12).
Three things about that host shape everything below:

- **No root.** `newfire_admin` has no sudo, and neither does any other account
  on the box -- DreamHost grants it on Dedicated and DreamCompute, not VPS. So
  no `apt`, no systemd unit, no editing anything under `/etc`.
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
  the internet ──443──> Apache mod_proxy ────> 127.0.0.1:8123
                        (DreamHost panel,       py4web run
                         terminates TLS)        web + scheduler
                                                      ^
                                cron + flock ─────────┘
                                restarts it if it dies
```

The proxy is the only way in. `serve.py` binds loopback, so the port is not
reachable from outside and there is no unencrypted copy of the site sitting
beside the real one.

Because we own this process rather than having it spawned on demand, the
scheduler runs **inside it**, as it does in development -- the arrangement the
app was designed for, and the one a full label sync was tested under.
`RUN_SCHEDULER` and `deploy/worker.sh` still exist if you ever want the queue in
its own process; nothing here needs them.

There is nothing custom in the web tier: `deploy/serve.sh` runs plain
`py4web run`. One thing about it does need checking once the site is live, and
it is in step 6 -- behind a TLS-terminating proxy the app sees plain HTTP, which
can cost the session cookie its `Secure` flag.

## Server layout

```
~/newfire/                       git checkout; nothing here is web-served
  apps/newfire/                    the app
  apps/_default -> newfire         tracked symlink; mounts the app at "/"
  apps/newfire/databases/          storage.db, mbcache.db — NOT in git
  deploy/serve.sh                  the web tier; what cron runs
  deploy/serve.py                  contingency, see step 6
~/newfire-venv/                  virtualenv on the system Python 3.10
~/logs/newfire.log               server output
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
8123 by default, one on your machine and one on the VPS's loopback interface,
where only the proxy can reach it.

## First deploy

**1. Check out and build the virtualenv.** The system Python is fine — the whole
dependency stack declares `>=3.9` and nothing in this app uses a newer stdlib
API. Do not build a Python from source.

```bash
git clone https://github.com/edcorcoran/new-fire.git ~/newfire
python3 -m venv ~/newfire-venv
~/newfire-venv/bin/pip install --upgrade pip
~/newfire-venv/bin/pip install py4web
```

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

**2. Write `settings_private.py`.** It is gitignored, so the clone does not carry
it and the app will run with development defaults until it exists.

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
rsync -avz seed/mbcache.db newfire_admin@vps:~/newfire/apps/newfire/databases/
```

Note which file is copied. `seed_cache.py` writes to `./seed`, never into
`apps/newfire/databases` -- the cache the app is using is the one thing that
must not be seeded over. `seed/` is gitignored, so the build artifact stays out
of the repo and off the server except by the line above.

About 80 MB for the 387 labels in `scripts/seed_labels.txt`. Skip it and the
first stranger to search waits on MusicBrainz at one request per second.

**4. Point the domain at the process.** `newfire.music` is its own registered
domain rather than a subdomain of one already here, so DNS comes first: point it
at this VPS, either by moving its nameservers to DreamHost or by setting an A
record at the apex to the VPS's address. Let's Encrypt validates over HTTP, so a
certificate cannot be issued until that resolves -- attempting it early is the
usual reason this step appears to fail.

In the panel: add the domain as fully hosted on this VPS and enable the free
Let's Encrypt certificate. Then **Servers & Usage -> Manage** next to the server,
scroll to **Proxy Server**, pick `newfire.music` from the URL dropdown, leave the
path blank so it covers the whole site, enter **8123**, and click **Add Proxy**.

The port must match `NEWFIRE_PORT` in step 5 and must be in 8000-65535; 80 and
443 are not available to you, and are not needed -- the proxy applies the
certificate to incoming connections and forwards plain HTTP to the loopback
port.

**5. Start the server.** One crontab line (`crontab -e`), which is both the
supervisor and the guarantee that only one exists:

```cron
* * * * * /usr/bin/flock -n /home/newfire_admin/newfire.lock /home/newfire_admin/newfire/deploy/serve.sh >> /home/newfire_admin/logs/newfire.log 2>&1
```

While the server holds the lock each minute's attempt exits immediately. The
first minute after it dies -- crash, reboot, or a deliberate restart -- starts a
replacement. No `@reboot` entry is needed; this covers it.

Confirm it came up, and that the proxy reaches it:

```bash
pgrep -af "py4web run"
curl -sI http://127.0.0.1:8123/ | head -1     # on the VPS
curl -sI https://newfire.music/ | head -1     # from anywhere
```

**6. Check the session cookie.** py4web sets the `Secure` flag from the request
scheme, and behind the proxy the app itself sees plain HTTP. ombott reads
`X-Forwarded-Proto` before falling back to the WSGI scheme, so if DreamHost's
proxy forwards that header this is already right -- and if it does not, the flag
is silently absent. Ask:

```bash
curl -sI https://newfire.music/auth/login | grep -i '^set-cookie.*session'
```

`HttpOnly`, `SameSite=Lax` and `Secure` should all be present. If `Secure` is
missing, point `serve.sh` at `deploy/serve.py` instead, which forces the header:

```sh
exec "$VENV/bin/python" "$CHECKOUT/deploy/serve.py"
```

then `pkill -f py4web` and wait a minute for cron. Assuming HTTPS there is safe
only because the socket is loopback: nothing reaches it except the proxy, which
is where the certificate lives.

## Deploying an update

```bash
cd ~/newfire && git pull
~/newfire-venv/bin/pip install -U py4web  # only when it moved
pkill -f "py4web run"                     # cron restarts it within a minute
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
rsync -avz seed/mbcache.db newfire_admin@vps:~/newfire/apps/newfire/databases/mbcache.db.new
```

```bash
# on the VPS
pkill -f "py4web run"
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
