# ZKTeco Attendance — self-hosted dashboard, reports and API

Pulls attendance from a **ZKTeco fingerprint terminal** (pyzk), stores it in a database, and serves a
dashboard, a printable report, PDF downloads and a JSON API. Runs anywhere Docker runs; the device only
has to be reachable from the machine running the collector. Brand it with your own `COMPANY_NAME`.

```
┌──────────────┐  every 5 min   ┌───────────┐   SQL / HTTP ingest   ┌─────────┐   HTTPS   ┌──────────┐
│ ZKTeco 4370  │ ─────────────► │ collector │ ────────────────────► │   DB    │ ◄──────── │   web    │
│ (LAN only)   │                │ (LAN box) │                       │ SQLite/ │           │ FastAPI  │
└──────────────┘                └───────────┘                       │ Postgres│           └──────────┘
                                                                    └─────────┘   n8n → GET /report
```

* **collector** — polls the device on a schedule, pulls all users + attendance logs, upserts them
  (idempotent on `(user_id, timestamp)`), survives the device being offline (logs, records the error,
  retries next tick). Writes straight to the DB (`COLLECTOR_TARGET=db`) or pushes to a remote web
  instance's `/api/ingest` (`COLLECTOR_TARGET=http`).
* **web** — dashboard, print view, PDF, JSON API. Reads only from the database. Can be hosted anywhere.

Dashboard features: live view of the current shift day (who is checked in, who hasn't checked out yet,
hours ticking in real time, a progress bar against a daily hours target), dark mode, English/Arabic
with RTL, employee search, departments (defined here, not on the device) with per-department
breakdowns and filters, display-name overrides, an employee profile page with day-by-day history,
CSV export, and Arabic-capable PDFs.

Why: the dashboard keeps working when the device is off, history outlives the device's own log
capacity, page loads never wait on a 5–10 s device pull, and the web app can live in the cloud while
the device stays on the LAN.

---

## Contents

1. [Quick start — single box on the LAN (SQLite)](#1-quick-start--single-box-on-the-lan-sqlite)
2. [Split deploy — collector on the LAN, web in the cloud](#2-split-deploy--collector-on-the-lan-web-in-the-cloud)
3. [Postgres instead of SQLite](#3-postgres-instead-of-sqlite)
4. [Reverse proxy with HTTPS (Caddy or nginx)](#4-reverse-proxy-with-https-caddy-or-nginx)
5. [Daily email with n8n (or any scheduler)](#5-daily-email-with-n8n-or-any-scheduler)
6. [Backing up the SQLite volume](#6-backing-up-the-sqlite-volume)
7. [HTTP API](#7-http-api)
8. [Business rules](#8-business-rules)
9. [Configuration](#9-configuration)
10. [Operations: first run, backfill, logs, health, troubleshooting](#10-operations)
11. [Development & tests](#11-development--tests)
12. [Repo layout](#12-repo-layout)

---

## 1. Quick start — single box on the LAN (SQLite)

One Linux box that can reach the device (same VLAN or routed). Both services run from one
`docker-compose.yml`; the database is a SQLite file on the `data` volume.

```bash
git clone <this repo> attendance && cd attendance
cp .env.example .env
```

Edit `.env` — at minimum:

| Variable | Set to |
|---|---|
| `ZK_IP` | the terminal's LAN IP (device menu: Comm > Ethernet) |
| `TZ` | the device's local timezone, e.g. `Europe/Berlin` |
| `API_KEY` | 32 random chars: `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | your sign-in; the first admin account is created from these on startup |
| `DAY_START_HOUR` | `7` (already the default) |

Then:

```bash
docker compose up -d --build
docker compose logs -f collector       # first pull happens immediately on start
curl http://localhost:5000/health      # last_sync + record_count should fill in after the first pull
```

Open `http://<server-ip>:5000/` and sign in with `ADMIN_EMAIL` / `ADMIN_PASSWORD`. Add HR users from the **Users** page.

What happens on start: the web container runs the Alembic migrations, becomes healthy, then the
collector starts, pulls the device and every `POLL_INTERVAL_MINUTES` after that. The **Sync now** button
on the dashboard triggers an immediate pull (see [Sync now](#sync-now)).

Data lives in the named volume `data` (`/data/attendance.db` + generated PDFs under `/data/reports`).

## 2. Split deploy — collector on the LAN, web in the cloud

The device is only reachable from the LAN, so run the collector there and push snapshots to a web
instance hosted anywhere. The DB is never exposed; the collector only needs outbound HTTPS.

**Cloud host** (web only, no device access):

```bash
cp .env.example .env
# ZK_IP=                  <- leave EMPTY: this instance never touches the device
# API_KEY=<secret>        <- shared with the collector
# ADMIN_EMAIL=... / ADMIN_PASSWORD=...
# DATABASE_URL=sqlite:////data/attendance.db   (or Postgres, see §3)
docker compose up -d --build web
```

Put it behind HTTPS (see §4) at, say, `https://attendance.example.com`.

**LAN box** (collector only):

```bash
cp .env.example .env
# ZK_IP=192.0.2.10
# TZ=Europe/Berlin          <- same TZ on BOTH sides
# COLLECTOR_TARGET=http
# INGEST_URL=https://attendance.example.com
# API_KEY=<same secret as the cloud host>
docker compose up -d --build collector
```

The collector POSTs `{users, records}` to `INGEST_URL/api/ingest` with `X-API-Key` every poll. The web
instance upserts it exactly like a local collector would. Because `get_attendance()` always returns
the device's full log, every push is a full snapshot and the upsert is idempotent — nothing duplicates
if a push is retried or the collector restarts.

In the split deploy the cloud web instance has no `ZK_IP`, so **Sync now** on the dashboard queues a
request in the DB. A collector with `COLLECTOR_TARGET=db` would pick it up within ~10 s; an `http`
collector cannot see the DB, so the next scheduled poll is the effective "sync now". Lower
`POLL_INTERVAL_MINUTES` on the LAN box if you want the cloud dashboard fresher.

The LAN collector can also run without Docker: a venv with `pip install -r requirements.txt` and
`python -m collector.main` under systemd works the same way (it needs the repo on `PYTHONPATH`).

## 3. Postgres instead of SQLite

Any SQLAlchemy Postgres URL works. For the bundled server:

```bash
# .env
DATABASE_URL=postgresql://attendance:change-me@postgres:5432/attendance
POSTGRES_PASSWORD=change-me
docker compose --profile postgres up -d --build
```

For an external/managed Postgres just set `DATABASE_URL` (`postgres://`, `postgresql://` and
`postgresql+psycopg://` are all accepted). Migrations run automatically on start (`AUTO_MIGRATE=true`)
or manually with `docker compose exec web alembic upgrade head`.

## 4. Reverse proxy with HTTPS (Caddy or nginx)

The app listens on plain HTTP (port 5000) and already enforces sign-in on every page and an
API key on the API routes (`/health` is open). Terminate TLS in front of it and only expose 80/443.

**Caddy** (automatic Let's Encrypt) — `deploy/Caddyfile`:

```
attendance.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:5000
}
```

```bash
docker run -d --name caddy --restart unless-stopped --network host \
  -v "$PWD/deploy/Caddyfile:/etc/caddy/Caddyfile" -v caddy_data:/data caddy:2
```

**nginx** — `deploy/nginx.conf` (certs via certbot). Copy to `/etc/nginx/sites-available/attendance`,
symlink into `sites-enabled`, `nginx -t && systemctl reload nginx`.

With either proxy, bind the app to localhost only by changing the compose port mapping to
`"127.0.0.1:5000:5000"`. Uvicorn is started with `proxy_headers=True`, so `X-Forwarded-*` is honoured.

## 5. Daily email with n8n (or any scheduler)

The API is designed to be driven by an automation tool. A typical daily-report workflow in n8n:

1. **Schedule Trigger** — every day a few minutes after `DAY_START_HOUR` (with the default 07:00, run at
   07:10 so the collector has already pulled the last punches of the previous shift day).
2. **HTTP Request** — `GET https://attendance.example.com/report`, query `date=yesterday`, header
   `X-API-Key: <API_KEY>`, response format *File*. The response is `attendance_YYYY-MM-DD.pdf`
   (`Content-Type: application/pdf`, `Content-Disposition: attachment`).
3. **Send Email** (Gmail, SMTP, Outlook…) — attach the binary from step 2.

For a per-department mail add `&department=<id>` (ids are visible in Settings), for Arabic add `&lang=ar`,
and use `GET /summary?date=yesterday` when you want JSON to build a Slack/Teams message instead.
Anything that can call an HTTP endpoint on a schedule works the same way (cron + curl, Zapier, Make).

## 6. Backing up the SQLite volume

The DB uses WAL mode, so copy it with SQLite's online backup API rather than `cp`:

```bash
scripts/backup_sqlite.sh ./backups        # -> backups/attendance-YYYYmmdd-HHMMSS.db.gz (keeps last 30)
```

Cron it nightly, e.g. `15 3 * * * cd /opt/attendance && scripts/backup_sqlite.sh /srv/backups/attendance`.

Restore:

```bash
docker compose stop
gunzip -c backups/attendance-20260914-031500.db.gz > /tmp/attendance.db
docker run --rm -v attendance_data:/data -v /tmp:/src alpine \
  sh -c 'rm -f /data/attendance.db-wal /data/attendance.db-shm && cp /src/attendance.db /data/attendance.db'
docker compose start
```

(`attendance_data` is the volume name compose generates from the project directory name; check with
`docker volume ls`.) Alternatively copy the whole volume directory while the stack is stopped.

Recovery without a backup: the device keeps its own log, so `scripts/backfill.py` (§10) rebuilds the
history the device still holds.

## 6b. Dashboard features

| Feature | Where | Notes |
|---|---|---|
| **Live day** | `/` on the current shift day | "Live" badge; present rows show *Hasn't checked out yet* (single punch so far) or *Checked out HH:MM*; hours for open sessions tick every 30 s and the page refreshes itself every 60 s. A *Currently in* card counts open sessions. |
| **Hours:Minutes** | everywhere in the UI, print view, PDFs | durations display as `H:MM` (5.99 h shows `5:59`; minutes are floored so a target never shows as met early). The API keeps decimal `hours` and adds `hours_hm` / `total_hours_hm`; CSV exports carry both. |
| **Target bar** | present table, employee page | progress toward `TARGET_HOURS` (default 6). Reports and the API still use the completed first/last pair only; live hours are dashboard-only. |
| **Late / Early badges** | present table, employee page | by first-punch time of day: **Late** from `LATE_AFTER_TIME` (18:30) until `LATE_EARLY_TURN_TIME` (01:00, wraps midnight), **Early** from the turn point until `EARLY_BEFORE_TIME` (13:00). Set a value empty to disable that badge. |
| **Dark mode** | top bar toggle | follows the OS preference by default; the choice is remembered per browser. |
| **English / Arabic** | top bar toggle, `?lang=ar` | full RTL layout, Arabic dates; remembered in a cookie; `DEFAULT_LANG` sets the default. PDFs accept `?lang=ar` (`/report?date=...&lang=ar`). |
| **Search** | dashboard, settings | filters by name, ID or department as you type. |
| **Schedules: days off & online days** | `/employee/<id>` (calendar), `/settings` (bulk) | Each employee has a **weekly pattern** (e.g. every Friday off, every Tuesday online) and a month **calendar** where any date can be set to Day off / Online / Working day. A calendar change always wins over the weekly pattern, and "Move this day off to…" swaps a worked day off for another date in one step. Settings has a bulk tool ("every Friday = day off" for everyone or one department). Scheduled days off and online days are **not absences**: they get their own sections on the dashboard, print view and PDF, and the attendance rate is `(present + online) / (days − days off)`. With no schedules defined, nothing changes. `WEEK_START` sets the calendar's first column. |
| **Vacations** | `/employee/<id>` (Vacations box, or "Vacation" in the day dialog), `/settings` (bulk) | Date ranges with an optional note, shown in purple on the calendar and removable from the list. The bulk tool adds the same period for everyone or one department (public holidays). Priority is single-date change > vacation > weekly pattern. Vacation days are not absences and are excluded from expected days; someone who punches in anyway shows as present with a "Worked during vacation" badge. |
| **Manual punch corrections** | pencil icon on any dashboard / profile row, or "Edit punches" in the calendar dialog → `/employee/<id>/punches?day=` | For a forgotten fingerprint or a day the device was offline: add a punch, or ignore a wrong device punch. A reason is mandatory. **Device records are never modified** — corrections live in their own table, are layered on at read time, can be undone, and survive re-syncs. Corrected days carry an "Edited" badge; the dashboard, API (`corrected`), CSV, print view and PDFs all use the corrected times. Every correction is in the audit log with who, when and why. |
| **Trends** | `/trends?from=&to=&department=` | Attendance rate per day, average hours, arrival-time histogram, weekday pattern, late / early counts, missing check-outs, most-late and most-forgotten-check-out rankings, and a per-employee table with average arrival time. Defaults to the last 30 days. |
| **Backup download** | Settings → Backup (admins only), `GET /backup` | Builds a consistent snapshot while the system runs (SQLite online backup; a JSON export on Postgres) and downloads a zip to the computer you are browsing from: the database, the audit log file and `RESTORE.txt`. Each download is audited. |
| **Sign-in & users** | `/login`, `/users`, `/account` | Real accounts with hashed (scrypt) passwords and server-side sessions. The first **admin** comes from `ADMIN_EMAIL` / `ADMIN_PASSWORD`; add **HR** users later on the Users page (or `scripts/create_user.py`). HR can use the dashboard, schedules, vacations and settings; only admins manage users, branding and see the audit log. Disabling a user or changing a password signs them out everywhere. Repeated failed logins are locked out for a few minutes; cross-site POSTs are rejected. |
| **Audit log** | `/audit` (admins only), `/data/audit.log` | Every sign-in (and failed attempt), sign-out, department / employee edit, weekly pattern, day off / online / vacation change, move, bulk action, manual sync, export, user and branding change: who, when, IP, and before/after values. Filter, search, export CSV. The same lines are appended to a plain-text file: `docker compose exec web tail -f /data/audit.log` (also visible in `docker compose logs web \| grep '"logger": "audit"'`). Passwords are never logged. |
| **Branding** | `/branding` (admins only) | Company name, login tagline, logo upload (PNG/JPG/WEBP/GIF, stored in the database), accent colours for light and dark mode, and the default theme. Applied to the dashboard, login page, browser tab icon, print view and PDFs. |
| **Departments** | `/settings` | create/rename/delete departments and assign employees. Stored only in this system's DB; the device is never modified. Dashboard, print view, PDF, CSV and `/summary` accept `?department=<id>`. A "By department" table appears once employees are assigned. |
| **Display name** | `/settings` | optional override of the device name (e.g. full Arabic name); used everywhere in this system. |
| **Employee profile** | `/employee/<id>` | stats + every shift day in a range, absences included, with target bars. |
| **CSV export** | `/export.csv?date=` or `?from=&to=` | UTF-8 with BOM, opens cleanly in Excel. |

## 7. HTTP API

All routes except `/health`, `/` and `/print` require `X-API-Key: <API_KEY>` (or `?api_key=`).
Web pages require a signed-in account (session cookie from `/login`); browsers are redirected there, other clients get 401.

| Route | Returns |
|---|---|
| `GET /health` | `{status, device, company, last_sync, last_attempt, last_error, sync_status: ok\|stale\|never, record_count, user_count, ...}` — `stale` = no successful sync for 3× the poll interval |
| `GET /report?date=today\|yesterday\|YYYY-MM-DD` | PDF download `attendance_YYYY-MM-DD.pdf`, `application/pdf` |
| `GET /report?from=YYYY-MM-DD&to=YYYY-MM-DD` | range PDF `attendance_<from>_to_<to>.pdf` |
| `GET /summary?date=...` | `{date, total, present, absent, total_hours, employees:[{id, name, first_in, last_out, hours, attended}]}` |
| `GET /summary?from=..&to=..` | `{from, to, days_total, total, avg_present_per_day, total_hours, employees:[{id, name, days_present, days_total, days_absent, total_hours, avg_hours_per_attended_day, attendance_rate}]}` |
| `POST /api/ingest` | body `{users:[{id,name}], records:[{user_id,timestamp,status,punch}]}` → upsert; returns `{users_created, users_updated, users_deactivated, records_received, records_inserted, records_skipped, record_count, last_sync}` |
| `POST /api/sync` | immediate collector run: `{mode: "direct", ...ingest counts}` when this instance has `ZK_IP`, else `{mode: "queued"}` |
| `GET /` | dashboard (signed in); `?department=<id>`, `?lang=ar` |
| `GET /employee/<id>?from=&to=` | employee profile (signed in) |
| `GET /settings` | departments + employee assignment (signed in) |
| `GET /export.csv?date=\|from=&to=` | CSV export (signed in) |
| `GET /print?from=&to=` | printable A4 report (signed in); `&auto=1` opens the print dialog on load |
| `POST /sync` | the dashboard's **Sync now** button (signed in); redirects back to the dashboard |

`timestamp` values are naive local time (`2026-05-24T03:00:00`); an offset, if sent, is stripped.

## 8. Business rules

Implemented in `app/rules.py` and covered by `tests/test_rules.py`.

**Shift day.** `DAY_START_HOUR` (0–23, default 7). Shift day **D** covers
`[D DAY_START_HOUR:00, D+1 DAY_START_HOUR:00)`; a punch maps to
`(timestamp - timedelta(hours=DAY_START_HOUR)).date()`. With `0` it is the plain calendar day.
So with `DAY_START_HOUR=7`, `/summary?date=2026-05-23` **includes** a punch at `2026-05-24 03:00` and
**excludes** one at `2026-05-24 07:00`.

**Per employee, per shift day.** `punches` = that employee's punches inside the window, ascending;
`attended = len(punches) >= 1`; `first_in = punches[0]`; `last_out = punches[-1]` only if there are at
least 2 punches, else `None`; `hours_worked = last_out - first_in` in hours (0 if `last_out` is `None`).
Punch type/status is ignored; breaks are not subtracted; only the first and last punch matter.

**Single day.** Every *active* enrolled user is listed. Present rows sorted by name, then absent rows
sorted by name. Summary: total employees, present, absent, total hours.

**Range (inclusive).** Per employee: `days_present`, `days_total`, `days_absent`, `total_hours`,
`avg_hours_per_attended_day = total_hours / days_present` (0 if none),
`attendance_rate = days_present / days_total`. Sorted by `days_present` desc, then name.
Summary: `days_total`, employees, `avg_present_per_day = Σ days_present / days_total`, `total_hours`.

**Users.** Employee list = users enrolled on the device (`id`, `name`; fallback `User {id}`). A user
that disappears from the device is marked inactive — never deleted — and drops out of reports; they
are reactivated automatically if they reappear. Their punches are kept forever.

**Time.** Device timestamps are naive local time. They are stored as-is; the container `TZ` must be
the device's zone, and all shift-day math is local. Nothing is converted to UTC.

## 9. Configuration

See `.env.example` for the full list with comments.

| Variable | Default | Notes |
|---|---|---|
| `ZK_IP`, `ZK_PORT`, `ZK_PASSWORD` | —, `4370`, `0` | device address and comm key; leave `ZK_IP` empty on a web-only host |
| `ZK_FORCE_UDP`, `ZK_OMIT_PING`, `ZK_TIMEOUT` | `false`, `false`, `10` | some firmwares need UDP / drop ICMP |
| `COMPANY_NAME` | `Attendance` | your company name: dashboard header, letterhead, PDF title |
| `DAY_START_HOUR` | `7` | shift day start |
| `TARGET_HOURS` | `6` | daily hours target for the progress bars |
| `LATE_AFTER_TIME` | `18:30` | first punch at/after this time-of-day is "Late" (empty = off) |
| `EARLY_BEFORE_TIME` | `13:00` | first punch before this time-of-day is "Early" (empty = off) |
| `LATE_EARLY_TURN_TIME` | `01:00` | late window ends / early window begins here (wraps past midnight) |
| `DEFAULT_LANG` | `en` | `en` or `ar`; users can switch in the UI |
| `WEEK_START` | `monday` | first column of the employee calendar: `monday`, `sunday` or `saturday` |
| `TZ` | `UTC` | **must equal the device's timezone** |
| `API_KEY` | — | required; protects `/report`, `/summary`, `/api/*` |
| `DATABASE_URL` | `sqlite:////data/attendance.db` | or a Postgres URL |
| `POLL_INTERVAL_MINUTES` | `5` | collector cadence |
| `COLLECTOR_TARGET` | `db` | `db` (write direct) or `http` (push to `INGEST_URL`) |
| `INGEST_URL` | — | base URL of the web instance when `COLLECTOR_TARGET=http` |
| `SERVER_PORT` | `5000` | web listen port |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | — | first admin account, created on startup if missing (never overwritten) |
| `SESSION_DAYS`, `COOKIE_SECURE` | `7`, `false` | sign-in lifetime; force the Secure cookie flag (automatic behind HTTPS) |
| `AUDIT_LOG_FILE` | `/data/audit.log` | plain-text copy of the audit log; empty disables the file |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | `admin`, — | legacy HTTP Basic login for scripts (HR role); leave the password empty to disable |
| `OUTPUT_DIR` | `/data/reports` | PDFs are written here, then served |
| `LOG_FORMAT`, `LOG_LEVEL` | `json`, `INFO` | structured logs on stdout |
| `AUTO_MIGRATE` | `true` | run Alembic on start |

## 10. Operations

### First run / recovery: backfill

The collector's first poll already pulls everything the device holds, but you can run a one-off pull
explicitly (e.g. after restoring an old backup):

```bash
docker compose run --rm collector python scripts/backfill.py            # honours COLLECTOR_TARGET
docker compose run --rm collector python scripts/backfill.py --dry-run  # just show what the device has
```

### Sync now

Dashboard button or `POST /api/sync`. If the web process has `ZK_IP` (single-box deploy) it pulls the
device itself and returns when done. Otherwise it sets a flag in `sync_state` that a `db`-target
collector honours within ~10 s. Only one pull runs at a time per process; if the collector and a manual
sync collide, the device rejects the second connection and that run is simply logged and retried.

### Logs & health

```bash
docker compose logs -f web collector          # JSON lines on stdout
curl -s localhost:5000/health | jq
```

`sync_status` turns `stale` when there has been no successful pull for 3× `POLL_INTERVAL_MINUTES`;
`last_error` carries the last device error. The web container's healthcheck hits `/health`; the
collector's checks a heartbeat file the scheduler touches every tick, so a wedged process is restarted
by Docker while an offline device is *not* treated as a collector failure.

### Device unplugged

The web app keeps serving the last-known data; the collector logs `collector run failed`, records the
error in `sync_state`, and retries on the next tick. Nothing crashes, nothing is lost, and when the
device comes back the full snapshot is upserted with no duplicates.

### Troubleshooting the device connection

* `connect()` times out → try `ZK_OMIT_PING=true` (firmware drops ICMP) and/or `ZK_FORCE_UDP=true`.
* `Unauthenticated` → `ZK_PASSWORD` must be the device's Comm Key (0 = none).
* Only one client can talk to the terminal at a time — close ZKAccess/BioTime sessions while testing.
* `python scripts/backfill.py --dry-run` is the quickest connectivity check.

## 11. Development & tests

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest                                              # rules, ingest idempotency, API contracts, auth, perf
```

Run locally without Docker (SQLite in `./data`):

```bash
export DATABASE_URL=sqlite:///./data/attendance.db OUTPUT_DIR=./data/reports API_KEY=dev \
       DASHBOARD_PASSWORD=dev ZK_IP=192.0.2.10 TZ=Europe/Berlin
python -m app.main            # http://localhost:5000
python -m collector.main      # in another shell
alembic revision --autogenerate -m "describe change"   # after editing app/models.py
```

Tests cover: shift-day mapping across midnight and the `DAY_START_HOUR=0` fallback, single punch =
present with 0 h, first/last with middle punches ignored, range aggregation and sorting, the
`2026-05-23` / `03:00` / `07:00` acceptance case through `/summary`, ingest idempotency on restart,
inactive users, API-key and Basic-Auth enforcement, PDF headers, and a 40 users × 90 days dashboard
render bound (measured ≈ 60 ms on SQLite).

## 12. Repo layout

```
app/            web service: config, models, db, rules (pure), service (queries/ingest), api, web, pdf, auth
collector/      device.py (pyzk), sync.py (pull → db/http), main.py (APScheduler loop)
templates/      dashboard.html, print.html (Jinja2, no JS framework)
alembic/        migrations (0001_initial)
tests/          pytest suite
scripts/        backfill.py, backup_sqlite.sh, collector_healthcheck.py
deploy/         Caddyfile, nginx.conf
Dockerfile      python:3.12-slim, non-root user, healthcheck
docker-compose.yml   web + collector (+ optional `postgres` profile)
.env.example
```
