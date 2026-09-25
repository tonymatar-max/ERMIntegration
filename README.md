# ERM Project Ledger (hosted, multi-user)

A hosted version of the Redmine Project Ledger dashboard. Unlike the original
script-based pipeline (Windows Task Scheduler + static HTML), this runs as a
web app that any number of users can log into — everyone shares **one**
Redmine connection (configured once under App Settings), **one** cached copy
of the fetched Redmine data, and **one** shared PP Report per month (locks,
drafts, everything). The only thing that's genuinely per-user is each
account's own login and their personal display preferences (My Team,
Project Manager id) — there's no per-user data to keep in sync or refresh
separately; whoever clicks "Refresh from Redmine" or locks a PP Report
month, everyone sees the result immediately.

## How it works

- **FastAPI** backend, **SQLite** storage (`data/erm.db`) — one DB.
- There's no public self-registration — an admin creates every account
  under **Users** (`/admin/users`), username/password (bcrypt-hashed). A
  signed session cookie keeps a user logged in once they have one.
- **App Settings** (`/app-settings`) holds the single shared Redmine URL,
  API key, and "all statuses" query id — stored **encrypted at rest**
  (Fernet, keyed by an app secret auto-generated and persisted to
  `data/secret.key` on first run; see Environment variables below) in the
  `app_settings` table. Every user's "Refresh from Redmine" uses this same
  connection. **Admin-only** — see Roles below.
- An **admin** account is auto-created on first startup if none exists yet
  (username `admin`, a random password printed once to the console/stdout
  — not logged to `data/app.log` — capture it immediately and change it
  after logging in). Admins see and can use **everything** — the Dashboard,
  PP Report, and Settings just like a regular user, plus the admin-only
  screens: **App Settings**, **Countries**, **PP Report Data**, **Users**,
  and the **Log** viewer. Regular users get only the former (redirected
  away from the admin screens, or a 403 for the Log's raw endpoint), and
  are unrestricted by default — see **Project Manager restriction** below.
  There's only ever one path to becoming admin — the auto-seeded account,
  or another admin granting the role to a new account under **Users** —
  since public registration doesn't exist. Admins can also reset any
  user's password or delete an account (the last remaining admin account
  can't be deleted, so there's always a way in) from the same **Users**
  page.
- **Project Manager restriction** (`/admin/users`, admin-only): each
  regular user can be assigned a single Redmine Project Manager id. Once
  assigned, that user is hard-restricted to that manager's projects
  everywhere — Dashboard, Time Analysis, and PP Report (rows, totals,
  country summary, and the Excel export) — other managers' data never
  reaches their browser, and the project-edit API refuses direct requests
  for a project outside it too. Leave it blank for "all projects" (today's
  behavior, and the default for every existing/new user until an admin
  sets one). Admins are never restricted, regardless of what's stored.
- **Settings** (`/settings`) holds the one remaining per-user preference:
  **My Team** (comma-separated Redmine user ids, powers the Time Analysis
  tab's "My Team" quick filter). Project Manager is admin-assigned only
  (see above) — Settings shows it read-only when one is set.
- **Countries** (`/admin/countries`, admin-only) holds a Daily Rate (plus
  currency) per country — seeded with KSA/Kuwait/Lebanon/UAE, but not a
  fixed list; add more anytime. Used by the PP Report's profitability
  columns (see below) — each project's rate comes from whichever country
  is configured for it.
- The "Refresh from Redmine" button fetches projects/issues/time-entries
  from Redmine using the shared App Settings key (logic ported from the
  original `redmine_status.py`) and caches the result in a single shared
  `fetch_cache` — **any** user can click it, and the result is immediately
  visible to **everyone**, not just whoever clicked it. The refresh-in-
  progress status is shared too (`fetch_status`, one row): if Alice starts
  a refresh and Bob loads the page a second later, Bob sees "Fetching from
  Redmine... (started by alice)" rather than a stale "never refreshed". If
  the app crashes or restarts mid-fetch, the next startup automatically
  detects and clears the stuck "running" status (otherwise every future
  refresh would be silently blocked forever); if a fetch instead just
  hangs while the app keeps running, App Settings shows a "Clear stuck
  refresh" button for an admin to unblock it manually.
- **Auto-refresh** (App Settings, admin-only): schedule "Refresh from
  Redmine" to run automatically — off, every N minutes, or daily/weekly/
  monthly at a chosen time — so nobody has to remember to click it. Runs
  as a background task for the life of the server process; all configured
  times are the **server's** local time zone, not the configuring admin's
  browser.
- The dashboard itself reuses the original `dashboard_template.html`
  (Projects + Time Analysis tabs), lightly extended, fed the shared
  cached data instead of baked-in JSON from a CSV.
- A **Project Manager dropdown** at the top of both the Dashboard's Projects
  tab and the Time Analysis tab lets you filter to any manager's projects,
  defaulting to your own configured Project Manager id.
- Clicking any project row on the Dashboard opens its edit panel directly;
  a **View** button under the ID column opens that project in Redmine
  instead (in a new tab), using App Settings' configured Redmine URL.
- The edit panel updates **Risk**, **SAP Order**, and **Last rev.
  summary/comments** — saving writes all of it back to Redmine as custom
  fields on the project in one request, and always stamps **Last rev.
  date** with today's date; the date is never user-editable.
- "Refresh from Redmine" runs as a background task server-side (see
  `/api/refresh` + `/api/status` in `app/main.py`) — the browser polls for
  progress, and a page reload while a refresh is in flight picks the
  polling back up instead of losing track of it.
- An application log at `data/app.log` (rotated, 5×2MB) records logins,
  settings changes, refreshes, and review saves — including full
  tracebacks on errors. View it in-browser at `/admin/log` (any logged-in
  user can see it — see Notes below) or open the file directly.
- **PP Report** menu (`/pp-report`, `app/pp_report.py`) — a hosted
  equivalent of the manual monthly "Earned Revenue Workings" workbook,
  shared by every user like everything else above: one set of drafts,
  one lock state per month — not a separate copy per user. Opening the
  page with no month specified defaults to the **earliest month not yet
  locked** (the next thing actually needing attention), not necessarily
  today's calendar month.
- Per calendar month, per project: Estimated/Spent come from cached
  Redmine data; **Total Project Revenue**, **Risk**, **Notes**, and
  **Project Completion** are editable per project per month (Risk here is
  a **local override** for this report only — it does not write back to
  Redmine, unlike the Dashboard's Risk edit). Project Completion has an
  Auto (⚙, formula-derived)/Manual (✎, hand-typed) toggle per row —
  switching to Manual with nothing typed yet pre-fills last month's value
  as a starting point. Everything else — Accumulated, Amount to be Taken,
  and a set of profitability columns (Sales Sold/Expected Profit, Project
  Time/Amount Profitability, Cost till Date, Expected Profitability by
  end of project) — is computed with the same formulas as the workbook's
  `Aug26` sheet, verified line-for-line against real cached values from
  that file; negative profit figures highlight in red. The
  profitability columns use the **Daily Rate configured for the
  project's country** (see Countries below), falling back to a generic
  per-month rate for a project whose country isn't configured there.
  A trend arrow next to **Amount to Take** compares it against last
  month's locked figure for the same project. Filterable by search text,
  **Country** (case-insensitive — "Lebanon"/"LEBANON" are the same
  country everywhere in this report), **Fully Earned**, and **Earned
  this month** (Amount to Take > 0).
- **2026-07 is the "opening balance" month** — the one month with no
  Redmine-tracked history to compute from. Its Risk/Total Revenue/
  Accumulated/Amount to Take come from a hand-maintained
  `opening_balance.csv` at the site root instead, so the earned-revenue
  chain has somewhere to start. Editing and saving a project's Total
  Revenue or Risk that month "graduates" it to normal computation from
  then on; editing only Project Completion/Notes does not.
- Edits to Total Revenue/Risk/Notes/Project Completion are **not**
  auto-saved — click **Save draft** to persist them (a true draft:
  reloading the page later shows whatever was last saved). **Lock this
  month** auto-saves whatever's currently on screen first (so it's safe
  to edit and lock in one go), then permanently commits those figures;
  the next month automatically reads a project's frozen "Accumulated" as
  its own "Previous Accumulated", replacing the workbook's manual
  month-to-month carry-forward — Notes always carry forward the same way,
  and Project Completion carries forward only if it was a genuine manual
  override (a purely computed value keeps recomputing fresh each month).
  **Reopen** unfreezes a locked month with its last draft still in place
  (nothing to retype) while every computed figure recalculates fresh from
  whatever's currently cached from Redmine.
- **Months lock and reopen strictly in order** — locking requires the
  previous month to already be locked (except the opening-balance month)
  and refuses if a later month is already locked; reopening refuses if
  any later month is still locked. Skipping this would leave a month's
  "Previous Accumulated" silently reading 0. Reopening a month shows a
  banner on the *next* month naming who reopened it and when, since that
  month's figures are provisional until it's relocked — polled live, so
  it updates without a manual reload if another user changes something.
- **PP Report Data** (`/admin/pp-report-months`, admin-only): lists every
  month with any PP Report state (locked or just a saved draft) and lets
  an admin **delete a month's data outright** — its locked snapshot and
  every draft — so it starts completely fresh, subject to the same
  strict-order rule as reopening. The same screen has an **Excluded
  projects** field (comma-separated Redmine project ids) to leave
  specific projects out of the PP Report entirely — rows, totals, country
  summary, and the Excel export.

## Environment variables

None are required — the app is zero-config out of the box. These are all
optional overrides:

| Variable | Purpose |
|---|---|
| `ERM_SECRET_KEY` | Overrides the auto-generated/persisted app secret (see below). Only needed if you want to manage the key yourself — e.g. a secrets manager, or sharing one key across multiple instances of this app behind a load balancer. |
| `ERM_KEY_PATH` | Overrides where the auto-generated key is saved (default `data/secret.key`). |
| `ERM_DB_PATH` | Overrides the SQLite file (default `data/erm.db`). Use this for local dev/testing so you never touch real user data. |
| `ERM_LOG_PATH` | Overrides the log file (default `data/app.log`). Same reasoning as above. |

## First-time setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

That's it — no secret key to generate or export. On first run, the app
creates its own app secret and saves it to `data/secret.key`, which
protects encrypted API keys at rest and signs session cookies. It's
reused automatically on every subsequent start; back it up along with
`data/erm.db` (losing it makes every stored Redmine API key undecryptable,
forcing every user to re-enter theirs in Settings).

> **Upgrading from an earlier version that required `$env:ERM_SECRET_KEY`?**
> If your server currently has that environment variable set, keep setting
> it exactly as before when you run the new version — it still takes
> precedence over the auto-generated file, so nothing changes. Only stop
> setting it if you're fine with every user re-entering their Redmine API
> key afterward (the newly auto-generated key can't decrypt values
> encrypted under the old one).

## Running

```powershell
.\run.ps1
```

Then open `http://localhost:9020` (or the server's hostname/port once
hosted) and log in as the auto-seeded `admin` account (see above for the
password). The **first** thing to do is go to **App Settings** and add the
shared Redmine URL/API key/query id, then go to **Users** and create an
account for everyone who needs one (there's no self-registration). Each
person logs in with the credentials they were given, optionally sets their
own **My Team**/**Project Manager id** under **Settings**, and clicks
**Refresh from Redmine** on the Dashboard.

## Running as a Windows Service

For a real persistent deployment (starts at boot, restarts automatically
if it crashes, runs with nobody logged in), use the scripts in `service/`
with [NSSM](https://nssm.cc/download) (a small, widely-used free tool for
wrapping any command as a Windows Service):

```powershell
# One-time: download NSSM, put nssm.exe on PATH (or pass -NssmPath below)

# Run as Administrator:
.\service\install_service.ps1              # installs + configures the service
nssm start ERMProjectLedger                # starts it
```

Logs land in `data\service-stdout.log` / `service-stderr.log` (process-level)
and `data\app.log` (application-level, same as running manually). To
remove the service later: `.\service\uninstall_service.ps1` (run as
Administrator) — this only removes the service registration, never
`data\erm.db` or `data\secret.key`.

## Notes / limitations of this first cut

- The Redmine "all statuses" saved-query workaround (needed to include
  closed projects) now needs just **one** saved query, owned by whichever
  Redmine account App Settings' API key belongs to — enter its numeric id
  there. (Previously each user needed their own; that's gone now that the
  connection is shared.)
- **Upgrading from the per-user-API-key version?** Each user's old
  Redmine URL/API key (in the `redmine_settings` table) is no longer read
  by anything — it's inert, harmless leftover data. Go to **App Settings**
  once and enter the shared connection details; nothing migrates
  automatically since there's no single "right" key to promote among
  what may have been several different per-user keys.
- **Upgrading from the per-user-cache version** (before Redmine data and
  the PP Report became shared)? This happens automatically on first
  startup after upgrading: cached Redmine data and refresh status aren't
  preserved (trivially regenerated by the next "Refresh from Redmine"),
  but PP Report data **is** — for each month (and each project within a
  month, for saved drafts), whichever user's row was most recently
  updated is kept as the new single shared row. Verified against a
  simulated two-user database before release; if you want to confirm your
  own data merged as expected, check `data/app.log` after the first
  restart, or query `pp_report_months`/`pp_report_overrides` directly.
- Data refresh can be on-demand (button click) or scheduled (Auto-refresh
  under App Settings — see above); either way the cache is shared, so one
  refresh covers everyone.
- Per-project exclusion from the original tool's `excluded_projects.csv`
  is now covered by the **PP Report Data** admin screen's **Excluded
  projects** and **Excluded Project Managers** settings — comma-separated
  project ids (or Project Manager ids, to exclude everything a manager
  owns), configured in the app rather than a CSV file. Both apply
  everywhere — Dashboard, Time Analysis, and PP Report (rows, totals,
  country summary, and the Excel export) — not just the PP Report.
  `my_team.csv` is covered by each user's own **My Team** field under
  Settings.
- **App Settings**, **Countries**, **PP Report Data**, **Users**, and
  **`/admin/log`** are all admin-only (see Roles above). There's only one
  role beyond "admin" — every other account is a regular user with
  identical rights to every other regular user (no per-user granularity,
  e.g. no way to make one regular user special).
- If `data/secret.key` is ever lost or replaced (e.g. an external process
  deletes it), the app doesn't crash — it logs a clear error and treats
  the now-undecryptable stored Redmine API key as blank, so App Settings
  and every page that depends on it stay usable; you just need to
  re-enter the API key once.
- To grant a second admin, create the account under **Users** with the
  "Grant admin" checkbox ticked — there's no separate promote/demote action
  for existing accounts (delete and recreate, or edit `is_admin` directly
  in `data/erm.db`, if a role needs to change after creation).
- Writing project fields back to Redmine requires App Settings' shared API
  key to have **Edit project** permission in Redmine, not just read access
  — a read-only key will get a clear 403 error in the edit modal.
- Code changes require restarting the running server process to take
  effect (no auto-reload in `run.ps1`); `data/erm.db`, `data/app.log`, and
  `data/secret.key` persist across restarts since they're just files on disk.
- The app itself serves plain HTTP with no built-in TLS — for access
  beyond your local network, put a reverse proxy in front of it on a real
  domain (e.g. [Caddy](https://caddyserver.com), which gets you automatic
  HTTPS via Let's Encrypt with a few lines of config: a domain pointed at
  your server plus `reverse_proxy localhost:9020` in a Caddyfile). Note
  automatic HTTPS needs an actual domain name — Let's Encrypt won't issue
  a certificate for a bare IP address.
- `data/secret.key` is as sensitive as a password — anyone with it (plus
  read access to `data/erm.db`) can decrypt every user's stored Redmine
  API key. Back it up somewhere safe alongside the database, and don't
  commit it to source control (already covered by `.gitignore`).
- The PP Report doesn't replicate the workbook's "Sap Company" filter
  (e.g. showing only Kuwait projects) — it lists every project in your
  cached Redmine data except whichever ones are explicitly excluded (see
  Excluded projects above). It also doesn't replicate the workbook's
  discrepancy-check sheets (MissingProjects, DiffSpentTime, etc.). It
  does support **Export to Excel** (a `.xlsx` per month, same columns and
  totals as the on-screen table) — just not a CSV export specifically.
- On a machine where local antivirus/endpoint-security software does
  HTTPS inspection (observed: Norton's Web/Mail Shield, which re-signs
  outbound connections with its own root certificate) — calls to Redmine
  can fail with `SSLCertVerificationError` even though the connection is
  genuinely fine, because Python's `requests` library trusts its own
  bundled CA list, not Windows' certificate store where that AV root is
  installed. `pip-system-certs` (in `requirements.txt`) fixes this by
  making `requests` trust the OS store instead — a no-op on machines
  without such software.
