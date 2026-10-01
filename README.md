# TrialDelta

A local-first research workspace for tracking **what changed** in oncology companies and clinical programs. TrialDelta preserves the evidence behind a change and keeps registry milestones separate from company readout guidance.

**Stack:** Python 3.13, FastAPI, SQLAlchemy, MySQL or SQLite, and browser-native JavaScript/CSS. The application needs no frontend build step, paid API, or language-model service.

## Run locally

From this repository on Windows:

```powershell
.\start.ps1 -WithWorker -ImportNews
```

Open **http://127.0.0.1:8765**. The launcher creates a virtual environment when needed, starts the app and optional daily worker in hidden processes, and writes logs under `data/`. To stop these processes:

```powershell
.\stop.ps1
```

For a fresh checkout without the existing workstation database, copy `.env.example` to `.env`. SQLite works without database credentials. For explicit foreground commands on any supported platform:

```bash
python -m venv .venv
# Activate .venv, then:
python -m pip install -r requirements.lock
python -m uvicorn trialdelta.app:app --host 127.0.0.1 --port 8765 --no-proxy-headers
# In a second terminal:
python -m trialdelta.worker
```

The checked-out workstation configuration uses `trialdelta` on local MySQL, using the **existing `DATABASE_PASSWORD` environment variable**. No password is copied into source or `.env`. `USE_LOCAL_MYSQL=true` selects this connection; `MYSQL_USER`, `MYSQL_HOST`, and `MYSQL_PORT` supply its nonsecret connection settings. A generic `DATABASE_URL` supports a separate host. MySQL startup refuses a database name other than `trialdelta`.

## Use the product

1. **Coverage:** follow a company, an NCT trial, a drug/intervention, a curated target, or an exact registry indication. Each hosted account has an independent watchlist.
2. **Refresh sources:** capture current ClinicalTrials.gov records for the 25-company oncology directory. First collection creates baselines, not alerts.
3. **Source inbox:** optionally import discovery candidates from your existing `news.stock_news` data. Headlines are explicitly unverified. Inspect the primary source, then use **Record evidence** to preserve an exact excerpt, publication date, title/page reference, and supported claim.
4. **Review queue:** approve or reject new company statements and registry differences. Only approved changes appear in the live log and digest. Approval records the reviewer and time.
5. **View evidence:** compare previous/current snapshots or quoted statements, open original sources, and download preserved registry JSON.
6. **Daily digest:** view or download the day's reviewed changes for your watchlist. Browse prior dates with the date picker.

**Explore demo** shows fictional companies and evidence so a portfolio visitor can understand the experience immediately. Demo records are client-side examples and never enter the live database, review queue, or digest.

## What is automated—and what requires review

Registry acquisition, pagination, exact sponsor matching, snapshot preservation, field comparisons, source discovery, watchlist matching, and digest compilation are automated. The app detects recruitment status, enrollment, primary/study completion, phase, trial design, primary outcome, and result-publication changes.

Company guidance is **manually sourced and reviewed** in this version. There is no unvalidated LLM extraction or unattended PDF interpretation. A reviewer links statements to the same NCT trial and category to establish a comparable history. The first statement is a baseline. Unlinked company-level statements are archived but do not automatically generate before/after events, since they might concern different programs. Same-day or older statements cannot replace a newer approved baseline through the ordinary workflow.

The optional news bridge performs a parameterized **SELECT only** against `news.stock_news`; all imported candidate metadata is written into `trialdelta`. It reads at most the latest 500 matched headlines in a 30-day window. It is a discovery aid, not exhaustive source coverage. It never writes to your reference databases or repositories. Set `SOURCE_DATABASE_URL` to use a separate read-only connection, or use the local MySQL configuration already present. Run manually:

```bash
python -m trialdelta.worker --once --import-news
```

## Evidence and time semantics

- Raw registry snapshots and submitted source excerpts are append-only through the public API. Corrections require a new sourced statement, never an edit to old evidence.
- Snapshots contain the NCT ID, canonical source record, retrieval time, registry update date, normalized fields, and SHA-256 hash. Repeat fetches update last-check time without duplicating identical versions.
- Registry primary completion, study completion, company expected readout, source publication date, and registry results publication are separate fields.
- `2028-06` stays a month. `H2 2026` is stored with half-year precision and its original wording; interval boundaries are not displayed as precise catalyst forecasts. Unsupported wording remains **uncertain**.
- Failed or incomplete fetches preserve the previous good state. Missing trials are not classified as deleted, terminated, or withdrawn.
- No change is published until a reviewer approves it. A rejected observation stays in the database for audit, but is excluded from the timeline and digest.
- "No reason stated" requires an affirmative source check. Missing information is labeled unreviewed or uncertain.
- Sponsor search can find collaborators, so results are retained only when the normalized lead sponsor matches. Coverage is intentionally conservative and excludes unmatched subsidiaries and collaborator-only trials.
- Target tags are curated per trial; company-level target lists are not indiscriminately assigned to every program. Theme follows match exact terms, ignoring case, to avoid errors such as confusing MET and RET.
- The directory is a research coverage list, **not** a live security master. Tickers do not assert current listing status, ownership, or availability for trading.

API reference: [ClinicalTrials.gov v2 OpenAPI specification](https://clinicaltrials.gov/api/oas/v2). The API supplies current versions; TrialDelta accumulates history prospectively. It does not claim complete pre-installation history.

## Daily worker and email

The retained worker collects on startup, then every `SYNC_INTERVAL_HOURS` (default 24) while the process and computer are running. Use `--once` with your own scheduler instead if preferred. A database advisory lock (MySQL) or shared filesystem lock (SQLite) prevents overlapping collections.

In-app digests include records **approved on the selected UTC date**, including delayed reviews. The optional email command sends the previous completed UTC day, so approvals later in the current day are not lost:

```bash
python -m trialdelta.worker --send-email
```

No email is sent by default. Configure SMTP, opt the recipient account in under Settings, and explicitly launch with `--send-email`. Local placeholder accounts are never emailed. SMTP uses STARTTLS. Empty digests are not mailed.

Email reservations prevent automatic duplicate delivery after a crash. Pre-send connection/authentication failures release the reservation for a safe retry. A failure during sending leaves an uncertain reservation (`td_digest_deliveries.sent_at IS NULL`) for operator reconciliation with mail-provider logs; do not blindly delete it and resend. Recipient failures do not stop registry collection. This is suitable for a small private pilot; a larger deployment should use a transactional delivery provider and queue.

## Hosting and accounts

**Local mode** accepts only loopback clients and does not require sign-in. The launcher binds to `127.0.0.1` and disables proxy-header trust.

**Hosted mode** requires HTTPS origin, secure cookies, and an initial administrator email/password of at least 14 characters. The reserved local administrator is unavailable in hosted mode. Settings lets an administrator create reader accounts; readers can maintain watchlists and digests but cannot ingest, review, or publish evidence. Authentication uses salted PBKDF2-SHA256 hashes and opaque, server-side expiring sessions. Mutations require a custom request header and same-origin checks; no cross-origin API access is enabled.

The app is prepared for an **invite-only consumer pilot or portfolio demonstration**. Public self-signup, subscription billing, password-reset email, enterprise audit exports, and unattended company-document extraction are not included. There are no payment or AWS resources provisioned by this repository.

See [AWS deployment instructions](deploy/AWS.md) for the included Docker Compose + Caddy setup. Application data persists in a named volume or your dedicated MySQL database. Do not expose the local no-login mode to the internet.

## Development and verification

```bash
python -m pytest -q
python -m compileall -q trialdelta
node --check trialdelta/static/app.js
```

Tests create an isolated temporary SQLite database. They never drop or modify your actual MySQL tables. Coverage includes baseline idempotence, material changes, metadata noise, source immutability, date precision, failed refresh retention, exact theme matching, approval chronology, authentication, per-user data isolation, CSRF/origin protection, and digest behavior.

Optional real-browser checks (requires Node.js and Edge or Chromium):

```bash
pnpm install --frozen-lockfile
pnpm test:browser
# Set BROWSER_CHANNEL=chrome when Chrome is installed instead of Edge.
```

The browser script checks navigation, fictional evidence detail, follow/unfollow persistence, review controls, desktop/mobile rendering, and JavaScript errors. Screenshots are saved under ignored `artifacts/`.

FastAPI's interactive documentation is available at `/api/docs` in local mode. Browser clients send `X-TrialDelta-Client: web` on mutation requests. Read endpoints and authorization are enforced on the server, not only in the UI.

## Repository layout

```text
trialdelta/
  app.py          HTTP API, validation, review endpoints
  auth.py         accounts, password hashing, sessions
  db.py           schema and database initialization
  registry.py     acquisition, snapshots, material field diffs
  services.py     date precision, reconciliation, evidence approval
  inbox.py        optional read-only news bridge
  digest.py       in-app brief, export, opt-in delivery
  worker.py       daily collector / one-shot scheduler entry
  seed.py         25-company oncology directory
  static/         complete browser application
tests/            isolated API and integrity tests
deploy/           AWS instructions and HTTPS proxy configuration
start.ps1         local Windows launcher
stop.ps1          stop only launcher-owned processes
```

Schema tables use the `td_` prefix. This initial release creates missing tables and its named uniqueness index non-destructively; it does not alter or drop existing user tables. Before future schema changes, introduce explicit versioned migrations and back up `trialdelta`. Back up SQLite while the processes are stopped, or use its online backup API; for MySQL, use your existing database backup process.

Reference repositories were inspected read-only: `Market_Data_Processing`, `News_Tracker`, and `Healthcare_Data`. TrialDelta has no runtime import dependency on any of them.

Optional WebMCP tools expose live reviewed-change reads and idempotent company following when the browser implements the proposed interface. The ordinary UI and HTTP API work independently. The local QA browser did not expose WebMCP, so native registration/execution has not been verified in a supporting browser.
