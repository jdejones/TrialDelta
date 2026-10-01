// Isolated target-action-date browser regression. Every asset is served from
// this checkout and every API request is mocked. No server or database is used.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const hostile = '<img src=x onerror="window.filingExecuted=true"> & <script>window.filingExecuted=true</script>';
const base = { ticker: "TADF", company: "Target Date Fixture", form_type: "8-K", source_count: 2, status: "identified", precision: "day", reason: "", imported_at: "2026-09-26T12:00:00+00:00" };
const rows = [
  { ...base, id: 1, tad: "2027-12-01", accession_no: "0001234567-26-000001", filed_at: "2026-09-25T00:10:00-04:00", filing_link: "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/fixture.htm?x=1&y=2", context: `The target action date is December 1, 2027. ${hostile}`, matched_text: "December 1, 2027" },
  { ...base, id: 2, tad: "2027-11", precision: "month", accession_no: hostile, filed_at: hostile, form_type: hostile, filing_link: "javascript:window.filingExecuted=true", context: "The target action date is November 2027.", matched_text: "November 2027" },
  ...Array.from({ length: 19 }, (_, i) => ({ ...base, id: i + 3, tad: `2026-12-${String(19 - i).padStart(2, "0")}`, matched_text: `December ${19 - i}, 2026`, context: `The target action date for ${i === 18 ? "unique-program-sentinel" : "cohort"} is December ${19 - i}, 2026.` })),
  { ...base, id: 100, tad: null, precision: "unknown", status: "ambiguous", matched_text: "05/06/2027", context: "The target action date is 05/06/2027.", reason: "Both month/day and day/month interpretations are valid." },
  { ...base, id: 101, tad: null, precision: "unknown", status: "unidentifiable", matched_text: "", context: "The target action date will be announced later.", reason: `No recognizable date was found. ${hostile}` },
];
let browser;
(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  const context = await browser.newContext({ viewport: { width: 1512, height: 1050 }, locale: "en-US", timezoneId: "America/Los_Angeles", serviceWorkers: "block" });
  const page = await context.newPage();
  const errors = [], unexpected = [], requests = [];
  let mode = "populated", signedIn = true, releaseFeed, feedGate, currentRows = rows;
  page.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
    const respond = (json, status = 200) => route.fulfill({ status, json });
    if (url.origin !== "http://trialdelta.test") { unexpected.push(req.url()); return route.abort(); }
    const files = {
      "/": ["index.html", "text/html"], "/static/app.js": ["app.js", "text/javascript"],
      "/static/styles.css": ["styles.css", "text/css"], "/static/base.css": ["base.css", "text/css"],
      "/static/favicon.svg": ["favicon.svg", "image/svg+xml"],
    };
    if (files[pathname]) {
      const [file, contentType] = files[pathname];
      return route.fulfill({ status: 200, contentType, body: fs.readFileSync(path.join(__dirname, "../trialdelta/static", file)) });
    }
    requests.push(`${req.method()} ${pathname}`);
    if (req.method() === "GET") {
      if (pathname === "/api/auth/config") return respond({ auth_required: true, public_browsing_enabled: false, registration_enabled: false, password_min_length: 14 });
      if (pathname === "/api/me") return signedIn ? respond({ id: 994001, display_name: "Date Reader", email: "dates@example.org", is_admin: false, mode: "local" }) : respond({ detail: "Sign in required." }, 401);
      if (["/api/companies", "/api/studies", "/api/follows", "/api/changes", "/api/home/trials", "/api/home/news"].includes(pathname)) return respond([]);
      if (pathname === "/api/home/filings") return respond({ items: [], page: 1, page_size: 10, total: 0, total_pages: 1 });
      if (pathname === "/api/home/tads") {
        const capturedRows = currentRows;
        if (mode === "delayed") await feedGate;
        if (mode === "error") return respond({ detail: "Saved dates are temporarily unavailable." }, 503);
        return respond(mode === "empty" ? [] : capturedRows);
      }
    }
    if (req.method() === "POST" && pathname === "/api/logout") { signedIn = false; return respond({ ok: true }); }
    if (req.method() === "POST" && pathname === "/api/login") { signedIn = true; return respond({ ok: true }); }
    unexpected.push(`${req.method()} ${pathname}`);
    return respond({ detail: "Unexpected target date test request blocked." }, 500);
  });
  const tab = name => page.getByRole("tab", { name, exact: true });
  const button = name => page.getByRole("button", { name, exact: true });
  const heading = name => page.getByRole("heading", { name, exact: true });
  const search = page.locator("#search");
  const cards = page.locator(".home-tad-card");
  const pagination = page.getByRole("navigation", { name: "Target action dates pagination", exact: true });
  const tadRequests = () => requests.filter(r => r === "GET /api/home/tads").length;
  const waitCards = n => page.waitForFunction(count => document.querySelectorAll(".home-tad-card").length === count, n);
  const beginDelay = () => { mode = "delayed"; feedGate = new Promise(resolve => { releaseFeed = resolve; }); };
  async function assertPage(expectedRows, current = 1) {
    const expected = expectedRows.slice((current - 1) * 10, current * 10);
    await waitCards(expected.length);
    await page.waitForFunction(expected => JSON.stringify([...document.querySelectorAll(".home-tad-card .tad-evidence blockquote")].map(n => n.textContent)) === JSON.stringify(expected), expected.map(r => r.context));
    if (expectedRows.length > 10) assert.equal(await pagination.locator('[aria-current="page"]').innerText(), String(current));
    else assert.equal(await pagination.count(), 0);
  }
  async function signIn() {
    await page.locator('#login-form input[name="email"]').fill("dates@example.org");
    await page.locator('#login-form input[name="password"]').fill("date-fixture-password");
    await page.locator('#login-form button[type="submit"]').click();
    await heading("Home.").waitFor();
  }
  await page.goto("http://trialdelta.test", { waitUntil: "networkidle" });
  assert.equal(tadRequests(), 0, "Dates load only when selected");
  assert.equal(await tab("8-Ks").isDisabled(), false);
  beginDelay();
  await tab("Trials").focus();
  await page.keyboard.press("End");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("Target action dates").getAttribute("aria-selected"), "true");
  await page.getByRole("status").filter({ hasText: "Loading the latest updates" }).waitFor();
  assert.equal(await button("Refresh dates").isDisabled(), true);
  releaseFeed(); mode = "populated";
  await assertPage(rows);
  assert.equal(await cards.first().locator("h3 time").innerText(), "Dec 1, 2027", "Exact day must not shift to the previous day in western time zones");
  assert.equal(await cards.nth(1).locator("h3 time").innerText(), "November 2027", "A month-only date must not acquire a day");
  assert.equal(await cards.nth(1).locator("h3 time").getAttribute("datetime"), "2027-11");
  assert.match(await cards.nth(1).innerText(), /Month reported/);
  assert.match(await page.locator(".home-feed-note").innerText(), /may have changed in a later filing/);
  await cards.first().locator("summary").click();
  assert.equal(await cards.first().locator("blockquote").innerText(), rows[0].context);
  assert.equal(await cards.first().locator("img,script").count(), 0, "Stored HTML-like content stays text");
  const sourceLink = cards.first().getByRole("link", { name: "Open original filing", exact: false });
  assert.equal(await sourceLink.getAttribute("href"), rows[0].filing_link);
  assert.equal(await sourceLink.getAttribute("target"), "_blank");
  assert.equal(await sourceLink.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await sourceLink.getAttribute("title"), "Open original SEC filing");
  assert.match(await cards.first().innerText(), /Accession: 0001234567-26-000001/);
  assert.equal(await cards.first().locator("details time").innerText(), "Sep 25, 2026", "Source filing calendar date is preserved");
  assert.equal(await cards.first().locator("details time").getAttribute("datetime"), rows[0].filed_at);
  assert.equal(await cards.first().locator("details time").getAttribute("title"), rows[0].filed_at);
  assert.match(await cards.first().innerText(), /2 source filings/);
  assert.doesNotMatch(await cards.first().innerText(), /Sep 26, 2026/, "Import time must not become a filing date");
  await cards.nth(1).locator("summary").click();
  assert.equal(await cards.nth(1).getByRole("link").count(), 0, "Executable source URLs must be suppressed");
  assert.equal(await cards.nth(1).locator("img,script").count(), 0, "Filing metadata stays escaped text");
  assert.equal(await cards.nth(1).locator("details time").innerText(), hostile);
  assert.equal(await cards.nth(1).locator("details time").getAttribute("datetime"), hostile);
  assert.match(await cards.nth(1).locator("details").innerText(), /Accession: <img/);
  assert.equal(await cards.nth(2).getByRole("link").count(), 0, "Missing metadata must not create a source link");
  assert.equal(await cards.nth(2).locator("details time").count(), 0, "Missing filing time must not use the import time");
  await cards.nth(1).locator("summary").click();
  assert.equal(await page.evaluate(() => window.filingExecuted), undefined);

  // Enabled-tab keyboard traversal includes both dates and filings.
  await tab("Target action dates").focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("Trials").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("Target action dates").getAttribute("aria-selected"), "true");
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("News").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  await assertPage(rows);
  assert.equal(tadRequests(), 1, "Returning to a tab reuses its saved response");

  await pagination.getByRole("button", { name: "Go to page 3", exact: true }).click();
  await assertPage(rows, 3);
  const ambiguous = cards.filter({ hasText: "Ambiguous date" });
  assert.match(await ambiguous.innerText(), /Both month\/day and day\/month/);
  await ambiguous.locator("summary").click();
  assert.match(await ambiguous.innerText(), /05\/06\/2027/);
  assert.equal(await ambiguous.locator("time").count(), 0);
  const unknown = cards.filter({ hasText: "Date unidentifiable" });
  assert.match(await unknown.innerText(), /No recognizable date/);
  assert.equal(await unknown.locator("img,script,time").count(), 0);
  await tab("News").click();
  await tab("Target action dates").click();
  await assertPage(rows, 3);

  // Search applies to all records before pagination, including source evidence.
  await search.fill("unique-program-sentinel");
  await assertPage(rows.filter(r => r.context.includes("unique-program-sentinel")));
  await search.fill("cohort");
  await assertPage(rows.filter(r => r.context.includes("cohort")));
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await assertPage(rows.filter(r => r.context.includes("cohort")), 2);
  await search.fill("2027-11");
  await assertPage([rows[1]]);
  await search.fill("nothing-matches-this-date");
  await heading("No matching target action dates").waitFor();
  await waitCards(0);
  await search.fill("");
  await assertPage(rows);

  const screenshotDir = path.join(__dirname, "../artifacts");
  fs.mkdirSync(screenshotDir, { recursive: true });
  await cards.first().locator("summary").click();
  await page.screenshot({ path: path.join(screenshotDir, "tads-desktop.png"), fullPage: true });
  for (const width of [360, 390, 768, 1024, 1512]) {
    await page.setViewportSize({ width, height: 950 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Date feed must fit ${width}px`);
    if (width === 390) await page.screenshot({ path: path.join(screenshotDir, "tads-mobile.png"), fullPage: true });
  }

  // Manual evidence keeps its provenance, including a grouped date that also
  // has filing sources. Notes and optional source URLs use the same safeguards.
  const manualRows = [
    { ...base, id: 300, tad: "2027-12-01", entry_method: "manual", evidence_method: "manual", source_count: 0, manual_count: 1, context: `manual-notes-sentinel ${hostile}`, matched_text: "2027-12-01", filing_link: "https://example.org/company-update?x=1&y=2", entered_by: "private-admin@example.org" },
    { ...base, id: 301, tad: "2027-11", precision: "month", entry_method: "manual", source_count: 0, manual_count: 1, context: "", filing_link: "javascript:window.filingExecuted=true", accession_no: "must-not-be-shown", filed_at: "2026-09-25", filing_sha256: "must-not-be-shown" },
    { ...base, id: 302, tad: "2027-10-15", entry_method: "mixed", evidence_method: "manual", source_count: 2, manual_count: 1, context: "Manual evidence for a date also reported in two filings.", filing_link: "https://example.org/target-date" },
    { ...rows[0], id: 303, entry_method: "mixed", evidence_method: "filing", source_count: 1, manual_count: 2 },
  ];
  currentRows = manualRows;
  await button("Refresh dates").click();
  await waitCards(manualRows.length);
  const manualDay = cards.nth(0), manualMonth = cards.nth(1);
  assert.equal(await manualDay.locator("h3 time").innerText(), "Dec 1, 2027");
  assert.equal(await manualMonth.locator("h3 time").innerText(), "November 2027");
  assert.equal(await manualMonth.locator("h3 time").getAttribute("datetime"), "2027-11");
  await manualDay.getByText("View entry details", { exact: true }).click();
  assert.equal(await manualDay.locator("blockquote").innerText(), manualRows[0].context);
  assert.match(await manualDay.locator("details").innerText(), /Notes:[\s\S]*Entered manually\./);
  assert.equal(await manualDay.locator(".source-meta").innerText(), "Manual entry");
  assert.doesNotMatch(await manualDay.innerText(), /SEC filing|source filings|Date expression:|private-admin@example.org/);
  assert.equal(await manualDay.locator("img,script").count(), 0);
  const manualSource = manualDay.getByRole("link", { name: "Open source", exact: false });
  assert.equal(await manualSource.getAttribute("href"), manualRows[0].filing_link);
  assert.equal(await manualSource.getAttribute("target"), "_blank");
  assert.equal(await manualSource.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await manualSource.getAttribute("title"), "Open entry source");
  await manualMonth.getByText("View entry details", { exact: true }).click();
  assert.match(await manualMonth.innerText(), /No notes were saved for this entry\./);
  assert.equal(await manualMonth.getByRole("link").count(), 0);
  assert.equal(await manualMonth.locator("details time").count(), 0);
  assert.doesNotMatch(await manualMonth.innerText(), /Accession:|Filed:|SEC filing|must-not-be-shown/);
  assert.equal(await cards.nth(2).locator("summary").innerText(), "View entry details");
  assert.equal(await cards.nth(2).locator(".source-meta").innerText(), "2 source filings · 1 manual entry");
  assert.equal(await cards.nth(2).getByRole("link", { name: "Open source", exact: false }).count(), 1);
  assert.equal(await cards.nth(3).locator("summary").innerText(), "View filing excerpt");
  assert.equal(await cards.nth(3).locator(".source-meta").innerText(), "1 source filing · 2 manual entries");
  assert.equal(await cards.nth(3).getByRole("link", { name: "Open original filing", exact: false }).count(), 1);
  await search.fill("manual-notes-sentinel");
  await assertPage([manualRows[0]]);
  assert.equal(await page.evaluate(() => window.filingExecuted), undefined);
  await search.fill("");
  await waitCards(manualRows.length);

  // Corrections display effective values while preserving the original saved
  // date and evidence separately. Neither source URL can execute page code.
  const editedRows = [
    { ...rows[0], id: 400, ticker: "EDIT", tad: "2027-12-02", is_edited: true, entry_method: "filing", evidence_method: "edited", context: `Edited date notes. ${hostile}`, matched_text: "stale parser match must not appear", original_tad: "2027-11", original_context: `original-evidence-sentinel ${hostile}`, original_filing_link: rows[0].filing_link, filing_link: "https://example.org/correction?x=1&y=2", edited_by: "private-editor@example.org" },
    { ...manualRows[0], id: 401, tad: "2028-01", precision: "month", is_edited: true, evidence_method: "edited", context: "", original_tad: "2027-12-01", original_context: "Original manual notes.", original_filing_link: "javascript:window.filingExecuted=true", filing_link: "javascript:window.filingExecuted=true" },
    { ...manualRows[2], id: 402, is_edited: true, evidence_method: "edited", context: "A date was supplied after reviewing the original mention.", original_tad: null, original_context: "", original_filing_link: null, filing_link: null },
  ];
  currentRows = editedRows;
  await button("Refresh dates").click();
  await waitCards(editedRows.length);
  const editedDay = cards.nth(0), editedMonth = cards.nth(1), editedMixed = cards.nth(2);
  assert.equal(await editedDay.locator("h3 time").innerText(), "Dec 2, 2027");
  assert.equal(await editedDay.locator(".ticker").innerText(), "EDIT");
  assert.equal(await editedMonth.locator("h3 time").innerText(), "January 2028");
  assert.equal(await editedMonth.locator("h3 time").getAttribute("datetime"), "2028-01");
  await editedDay.getByText("View entry details", { exact: true }).click();
  assert.match(await editedDay.innerText(), /Edited entry[\s\S]*Administrator correction\./);
  assert.equal(await editedDay.locator("details > blockquote").innerText(), editedRows[0].context);
  assert.equal(await editedDay.locator(".tad-original-evidence blockquote").innerText(), editedRows[0].original_context);
  assert.match(await editedDay.locator(".tad-original-evidence").innerText(), /Original saved date: November 2027[\s\S]*Original saved evidence:/);
  assert.match(await editedDay.locator(".source-meta").innerText(), /2 source filings/);
  assert.doesNotMatch(await editedDay.innerText(), /stale parser match|Date expression:|private-editor@example.org/);
  const editedSource = editedDay.getByRole("link", { name: "Open source", exact: false });
  const originalSource = editedDay.getByRole("link", { name: "Open original source", exact: false });
  assert.equal(await editedSource.getAttribute("href"), editedRows[0].filing_link);
  assert.equal(await originalSource.getAttribute("href"), editedRows[0].original_filing_link);
  assert.equal(await originalSource.getAttribute("target"), "_blank");
  assert.equal(await originalSource.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await originalSource.getAttribute("title"), "Open original saved source");
  assert.equal(await editedDay.locator("img,script").count(), 0);
  await editedMonth.getByText("View entry details", { exact: true }).click();
  assert.match(await editedMonth.innerText(), /No notes were saved for this entry\./);
  assert.match(await editedMonth.locator(".tad-original-evidence").innerText(), /Original saved date: Dec 1, 2027[\s\S]*Original manual notes\.[\s\S]*Entered manually\./);
  assert.equal(await editedMonth.locator(".source-meta").innerText(), "Manual entry");
  assert.equal(await editedMonth.getByRole("link").count(), 0);
  assert.doesNotMatch(await editedMonth.innerText(), /SEC filing|private-admin@example.org/);
  await editedMixed.getByText("View entry details", { exact: true }).click();
  assert.match(await editedMixed.locator(".tad-original-evidence").innerText(), /Original saved date: Not identified[\s\S]*No original evidence was saved\./);
  assert.equal(await editedMixed.locator(".source-meta").innerText(), "2 source filings · 1 manual entry");
  await search.fill("original-evidence-sentinel");
  await waitCards(1);
  assert.equal(await cards.locator("h3 time").innerText(), "Dec 2, 2027", "Preserved evidence remains searchable without changing the effective date");
  assert.equal(await page.evaluate(() => window.filingExecuted), undefined);
  await search.fill("");
  await waitCards(editedRows.length);
  currentRows = rows;
  await button("Refresh dates").click();
  await assertPage(rows);

  mode = "empty";
  await button("Refresh dates").click();
  await heading("No target action dates yet.").waitFor();
  mode = "error";
  await button("Refresh dates").click();
  await heading("Unable to load target action dates").waitFor();
  assert.match(await page.locator("#home-feed-panel").innerText(), /Saved dates are temporarily unavailable/);
  mode = "populated";
  await button("Try again").click();
  await assertPage(rows);

  // A late response from the signed-out session must never refill its cache.
  beginDelay();
  await button("Refresh dates").click();
  await page.getByRole("status").filter({ hasText: "Loading the latest updates" }).waitFor();
  await button("Sign out").click();
  await page.locator("#login-form").waitFor();
  releaseFeed(); mode = "populated";
  await page.waitForLoadState("networkidle");
  currentRows = [{ ...rows[0], id: 200, ticker: "NEW", company: "New session company", context: "New session target action date is December 1, 2027." }];
  await signIn();
  await tab("Target action dates").click();
  await assertPage(currentRows);
  assert.match(await cards.innerText(), /New session company/);

  // Demo navigation must not fetch or display saved date records.
  await button("Change log").click();
  await button("Explore demo").click();
  const beforeDemo = tadRequests();
  await page.evaluate(() => { location.hash = "home"; });
  await heading("Home.").waitFor();
  await tab("Target action dates").click();
  await heading("Target action dates are not included in the demo.").waitFor();
  assert.equal(await cards.count(), 0);
  assert.equal(tadRequests(), beforeDemo);
  await button("Home").click();
  await tab("Target action dates").click();
  await assertPage(currentRows);

  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  assert.equal(requests.some(r => /\/api\/(?:sync|jobs|review|inbox|tads\/import)/.test(r)), false, "Reader actions never collect filings or run imports");
  console.log(JSON.stringify({ result: "passed", checks: "lazy saved feed, day/month precision, escaped evidence, manual entry notes and safe source links, corrections preserve original evidence, mixed provenance, unresolved reasons, keyboard tabs, filtering before pagination, page memory, refresh/loading/empty/error/retry, responsive layout, stale-session reset, demo separation", liveDatabaseReads: 0, liveDatabaseWrites: 0 }));
  await browser.close();
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exitCode = 1; });
