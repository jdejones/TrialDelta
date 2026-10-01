# Therapeutic-area scope audit

Reviewed 2026-09-26 against the current working copy, including the bulk downloader. The check covered the browser and administrator UI, discovery, collection, normalization, storage, API reads, watchlists, evidence, change publication, digests, worker, configuration, and deployment paths.

## Restrictions removed

| Path | Previous behavior | Current behavior |
| --- | --- | --- |
| Browser metadata, page headings, workspace label | Described the entire app as oncology-only | Describes clinical research across therapeutic areas |
| Company directory and sidebar | Presented the historical blanket `area` default as each company's research focus; hidden defaults also affected company search | Shows actual captured-trial counts; company search uses names and tickers; existing classification metadata is preserved |
| New company rows and seed directory | Defaulted to `Oncology` | Default to `Healthcare`; seed entries are a starting directory, not an allowlist |
| Ordinary Collection and scheduled worker | Requested oncology conditions and interventional studies only | Requests every condition and study type for each covered lead sponsor |
| Ordinary collector pagination | Failed after 30 pages (up to 3,000 results) | Follows every next-page cursor, including on empty pages; detects invalid or repeated cursors and preserves the previous good company state on failure |
| Intervention normalization | Indexed only drugs, biological products, and genetic interventions | Includes names from all intervention types, including devices, procedures, behavioral interventions, dietary supplements, and diagnostic tests, using the compatible `drug` field |
| Bulk download form | Offered an oncology-interventional restriction | New UI runs retrieve all conditions and study types |
| Phase presentation | Missing or `NA` phases could appear blank or as a raw code | Missing phases say `Phase not reported`; `NA` says `Not applicable` |
| Fictional demo | Used only oncology examples | Also includes diabetes and rheumatoid arthritis examples |

Actual oncology company names, conditions, and clinical data remain valid records. Removing a scope restriction does not remove those records.

## Infrastructure findings

| Component | Finding |
| --- | --- |
| Healthcare discovery | Uses the entire Healthcare sector from both reference files; no disease or biotechnology-only company whitelist. Medical-device companies are eligible. Missing or ambiguous reference identities are rejected explicitly. |
| Bulk retrieval | Already defaulted to all conditions and study types. Uses bounded sponsor batches, page transactions, saved cursors, cancellation/resume, retries, and the shared collection lock. |
| Legacy bulk runs | The internal `oncology_only` option remains for compatibility. Resuming an old filtered checkpoint preserves its query and cursor semantics; it does not falsely mark full company coverage. The administrator log explains the retained legacy filter. Start a fresh run for expanded coverage. |
| Normalization | Conditions are source text, not a disease enum. Observational studies and absent phases/interventions are accepted. Observational design attributes already participate in design comparisons. |
| Storage | No oncology constraint or disease foreign-key allowlist. Study conditions/targets and snapshot evidence use text fields. No schema migration is needed for this scope change. |
| Existing company classifications | `Company.area` is metadata only. It does not gate acquisition, API reads, matching, or publication. Existing values are preserved because the database has no provenance that distinguishes an old default from a deliberate classification. |
| API and search | Companies, programs, trial details, snapshots, and changes have no therapeutic-area filter. Stored non-oncology records are available through the same endpoints and visitor permissions. |
| Home trials | Draws from saved trials and their first snapshot across all conditions. Refreshing a trial does not change its first-seen ordering. |
| Follows and targets | Company, program, intervention, target, and indication follows are generic. Theme matching uses exact terms ignoring case; targets are free-form curated tags. |
| Evidence and changes | Evidence validation, review, source chronology, and deterministic registry comparisons have no oncology requirement. Baselines create no changes; unchanged raw snapshots are not duplicated. |
| Digests and accounts | Visibility, watchlist matching, export, and delivery selection have no specialty gate. Personal account and administrator boundaries are unchanged. |
| Worker and deployment | Worker selection covers all saved companies or an explicitly selected company ID. Launchers, environment examples, Compose, and deployment configuration contain no hidden disease filter. |
| Source inbox | Reads news for the saved company directory without a disease filter. It remains a read-only bridge to the source database. |

Read-only inspection of the configured MySQL `trialdelta` database found 25 companies, all carrying the historical `Oncology` label, and 638 saved studies at the time of review. Study conditions and targets are `TEXT`, intervention names are `VARCHAR(500)`, and raw snapshots are `LONGTEXT`. No live research data was modified or downloaded during this audit; tests use isolated data.

## Remaining coverage and capacity limits

These are separate from oncology scope and should not be mistaken for exhaustive registry or healthcare coverage:

- **Lead-sponsor coverage:** acquisition remains tied to covered companies and normalized exact lead-sponsor matches. Collaborator-only matches, unmatched subsidiaries, ambiguous sponsors, and organizations absent from the directory are not automatically assigned. Discovery uses the Healthcare reference-file universe; it is not a download of the entire ClinicalTrials.gov registry.
- **News product scope:** Home News intentionally remains filtered to the `Biotechnology` industry from the security reference table, including biotechnology companies outside local coverage. It covers all therapeutic areas within that industry but excludes medical-device and other healthcare industries. Its existing UI describes this narrower news scope.
- **Feed windows:** Home Trials displays the latest 100 captured trials, Home News returns at most 100 articles after scanning 500 rows, and the source inbox import scans at most 500 matching articles in a 30-day window. Coverage and study APIs expose the broader stored trial set.
- **Intervention index length:** the compatibility `drug` field still has a 500-character aggregate limit. Names beyond that cutoff can be absent or partial in search/theme-follow matching, although full source and normalized snapshot evidence retain them. Individual theme follows remain limited to 200 characters. Expanding these limits requires a coordinated schema/API migration.
- **Large-universe performance:** several company/study/change API responses and browser filters operate on whole stored lists. Ordinary collection retains a company's pages in memory before committing; bulk download commits page by page. Larger imports can require pagination and performance work, independent of disease support.
- **Tracked differences:** study type, intervention names, conditions, and title remain preserved source data rather than dedicated alert categories. The existing tracked categories cover status, enrollment, dates, phase/design, primary outcomes, and results publication.
- **Prospective history:** previously excluded trials need a fresh unrestricted import. Opening the updated UI does not backfill data, refresh stored intervention indexes, or start a collector. An ordinary refresh of unchanged raw data can repair the intervention index without adding duplicate snapshots or alerts.

## Verification

Regression coverage includes non-oncology interventional and observational records, every intervention category, ordinary refresh after bulk import, pagination beyond 30 pages, invalid/repeated cursors, exact sponsor exclusions, and legacy checkpoint resume. API tests exercise diabetes, Alzheimer's disease, rheumatoid arthritis, influenza, and heart failure through public browsing, history, search, all five follow kinds, arbitrary target tags, digest/export, and evidence publication. Browser verification covers broad-scope labels, non-oncology discovery/search, phase presentation, and desktop/mobile layouts.

Results on this workstation:

- `python -m pytest -q --ignore=tests/test_bulk_ui.py`: **249 passed**, with the existing Starlette/httpx deprecation warning.
- `tests/test_bulk_ui.py`: all **17 desktop cases passed across runs**; a transient Tk theme-loading skip passed on its isolated retry. Desktop tests were run separately after the initial mixed API/Tk run hit a native Windows Tk garbage-collection failure.
- `node scripts/healthcare_browser_check.cjs`: passed using intercepted fixtures and local assets only.
- `node scripts/visitor_browser_check.cjs`: passed using a temporary SQLite application, including widths 360, 390, 768, and 1440; zero JavaScript errors or live database writes.
- `node scripts/browser_check.cjs`: passed research navigation, feeds, pagination, search, evidence controls, and mobile checks; zero JavaScript errors, overflow, operational requests, or live writes.
- Collector tests were rerun serially after initial parallel test processes contended for the existing shared lock; the serial collector/integrity/bulk group passed all **72 tests**. The new collector-scope tests isolate their lock directory.
- `git diff --check`: passed.

Restart an already-open administrator console or worker to load collector changes. Reload the browser for the updated static UI. Use a fresh unrestricted bulk run to collect previously excluded studies; old filtered checkpoints deliberately retain their saved scope.
