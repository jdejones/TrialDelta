// Calendar regression against local assets and deterministic API fixtures only.
// Every network request is intercepted; no app server or database is touched.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const hostile = '<img src=x onerror="window.calendarExecuted=true">';
const base = { ticker: "CALF", company: "Calendar Fixture", form_type: "8-K", source_count: 1, status: "identified", precision: "day", reason: "", imported_at: "2027-11-30T12:00:00+00:00" };
const dated = (id, tad, more = {}) => ({ ...base, id, tad, matched_text: tad, context: `The target action date is ${tad}.`, ...more });
const rows = [
  dated(1, "2027-12-01", { company: "Opening Day Research", ticker: "OPEN", context: `The target action date is December 1, 2027. ${hostile}`, accession_no: "0001234567-27-000001", filed_at: "2027-11-29T00:10:00-05:00", filing_link: "https://www.sec.gov/Archives/edgar/data/1234567/000123456727000001/fixture.htm" }),
  dated(2, "2027-12-31", { ticker: "LAST", company: "Year End Research" }),
  dated(3, "2027-12", { ticker: "MON", company: "Month Only Research", precision: "month", matched_text: "December 2027", context: "The target action date is December 2027." }),
  dated(4, "2028-02-29", { ticker: "LEAP", company: "Leap Day Research" }),
  dated(5, "2028-01-01", { ticker: "NEXT", company: "Next Year Research" }),
  dated(6, "2027-12-01", { ticker: "ALSO", company: "Another Opening Day Company" }),
  ...Array.from({ length: 14 }, (_, i) => dated(10 + i, `2027-12-${String(i + 4).padStart(2, "0")}`, { ticker: `DAY${i + 4}` })),
  dated(30, "2027-12-21", { ticker: "MAN", company: "Manual Date Company", entry_method: "manual", evidence_method: "manual", source_count: 0, manual_count: 1, context: `Manually entered date notes. ${hostile}`, filing_link: "https://example.org/company-update?x=1&y=2" }),
  dated(31, "2027-12", { ticker: "MMON", company: "Manual Month Company", precision: "month", entry_method: "manual", evidence_method: "manual", source_count: 0, manual_count: 1, context: "Company reported a month without a day." }),
  dated(32, "2027-12-22", { ticker: "EDIT", company: "Corrected Date Company", is_edited: true, entry_method: "filing", evidence_method: "edited", context: `Correction notes. ${hostile}`, original_tad: "2027-12-03", original_context: `Original filing evidence. ${hostile}`, original_filing_link: "https://www.sec.gov/Archives/fixture.htm", filing_link: "https://example.org/corrected-date" }),
  dated(33, "2027-12", { ticker: "EMON", company: "Corrected Month Company", precision: "month", is_edited: true, entry_method: "manual", evidence_method: "edited", source_count: 0, manual_count: 1, context: "Corrected to month precision.", original_tad: "2027-12-30", original_context: "Original manual entry.", original_filing_link: "javascript:window.calendarExecuted=true", filing_link: null }),
  { ...base, id: 100, tad: null, precision: "unknown", status: "ambiguous", ticker: "AMB", matched_text: "05/06/2028", context: "The target action date is 05/06/2028.", reason: "Both month/day and day/month interpretations are valid." },
  { ...base, id: 101, tad: null, precision: "unknown", status: "unidentifiable", ticker: "NONE", matched_text: "", context: `The target action date will be announced. ${hostile}`, reason: "No recognizable date was found." },
];
let browser;
(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  const context = await browser.newContext({ viewport: { width: 1512, height: 1050 }, locale: "en-US", timezoneId: "America/Los_Angeles", serviceWorkers: "block" });
  const page = await context.newPage();
  await page.clock.setFixedTime(new Date("2027-12-15T12:00:00-08:00"));
  const errors = [], unexpected = [], requests = [];
  let mode = "populated", currentRows = rows, feedGate, releaseFeed;
  page.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
    const respond = (json, status = 200) => route.fulfill({ status, json });
    if (url.origin !== "http://trialdelta.test") { unexpected.push(req.url()); return route.abort(); }
    const files = { "/": ["index.html", "text/html"], "/static/app.js": ["app.js", "text/javascript"], "/static/styles.css": ["styles.css", "text/css"], "/static/base.css": ["base.css", "text/css"], "/static/favicon.svg": ["favicon.svg", "image/svg+xml"] };
    if (files[pathname]) {
      const [file, contentType] = files[pathname];
      return route.fulfill({ status: 200, contentType, body: fs.readFileSync(path.join(__dirname, "../trialdelta/static", file)) });
    }
    requests.push(`${req.method()} ${pathname}`);
    if (req.method() === "GET") {
      if (pathname === "/api/auth/config") return respond({ auth_required: false, public_browsing_enabled: false, registration_enabled: false });
      if (pathname === "/api/me") return respond({ id: 995001, display_name: "Calendar Reader", email: "calendar@example.org", is_admin: false, mode: "local" });
      if (["/api/companies", "/api/studies", "/api/follows", "/api/changes", "/api/home/trials", "/api/home/news"].includes(pathname)) return respond([]);
      if (pathname === "/api/home/filings") return respond({ items: [], page: 1, page_size: 10, total: 0, total_pages: 1 });
      if (pathname === "/api/home/tads") {
        if (mode === "delayed") await feedGate;
        if (mode === "error") return respond({ detail: "Saved dates are temporarily unavailable." }, 503);
        return respond(mode === "empty" ? [] : currentRows);
      }
    }
    unexpected.push(`${req.method()} ${pathname}`);
    return respond({ detail: "Unexpected calendar test request blocked." }, 500);
  });
  const tab = name => page.getByRole("tab", { name, exact: true });
  const button = name => page.getByRole("button", { name, exact: true });
  const heading = name => page.getByRole("heading", { name, exact: true });
  const grid = page.locator(".tad-calendar");
  const panel = page.locator("#home-feed-panel");
  const search = page.locator("#search");
  const event = id => page.locator(`[data-calendar-tad="${id}"]`);
  const cell = day => page.locator(`[data-calendar-day="${day}"]`);
  const tadRequests = () => requests.filter(r => r === "GET /api/home/tads").length;
  const beginDelay = () => { mode = "delayed"; feedGate = new Promise(resolve => { releaseFeed = resolve; }); };
  async function assertEvents(ids) {
    await page.waitForFunction(expected => JSON.stringify([...document.querySelectorAll(".tad-calendar [data-calendar-tad]")].map(el => Number(el.dataset.calendarTad)).sort((a, b) => a - b)) === JSON.stringify(expected), [...ids].sort((a, b) => a - b));
  }
  async function assertMonth(name, days) {
    await heading(name).waitFor();
    assert.equal(await grid.locator("[data-calendar-day]").count(), days);
  }
  const decemberIds = rows.filter(r => r.precision === "day" && r.tad?.startsWith("2027-12")).map(r => r.id);
  await page.goto("http://trialdelta.test", { waitUntil: "networkidle" });
  assert.equal(tadRequests(), 0);
  assert.deepEqual(await page.getByRole("tab").allTextContents(), ["Trials", "News", "Target action dates", "8-Ks", "Calendar"]);
  assert.equal(await tab("8-Ks").isDisabled(), false);
  beginDelay();
  await tab("Trials").focus();
  await page.keyboard.press("End");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  assert.equal(await tab("Calendar").evaluate(node => node === document.activeElement), true);
  await page.getByRole("status").filter({ hasText: "Loading" }).waitFor();
  assert.equal(await button("Refresh dates").isDisabled(), true);
  releaseFeed(); mode = "populated";
  await assertMonth("December 2027", 31);
  await assertEvents(decemberIds);
  assert.deepEqual(await grid.locator("thead th").allTextContents(), ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]);
  assert.equal(await cell("2027-12-01").evaluate(el => el.cellIndex), 3, "December 1, 2027 is Wednesday");
  assert.equal(await cell("2027-12-31").evaluate(el => el.cellIndex), 5, "December 31, 2027 is Friday");
  assert.equal(await grid.locator('[aria-current="date"]').getAttribute("datetime"), "2027-12-15");
  assert.equal(await cell("2027-12-01").locator('[data-calendar-tad="1"]').count(), 1, "December 1 must not shift to November 30");
  assert.equal(await cell("2027-12-01").locator("[data-calendar-tad]").count(), 2, "Multiple reported dates on one day remain accessible");
  assert.equal(await cell("2027-12-31").locator('[data-calendar-tad="2"]').count(), 1);
  assert.equal(await grid.locator('[data-calendar-tad="3"]').count(), 0, "Month precision must never become day 1");
  assert.equal(await page.locator('.calendar-month-only [data-calendar-tad="3"]').count(), 1);
  assert.equal(await cell("2027-12-21").locator('[data-calendar-tad="30"]').count(), 1, "Manual exact date stays on the entered calendar day");
  assert.equal(await grid.locator('[data-calendar-tad="31"]').count(), 0, "Manual month-only date must not acquire a day");
  assert.equal(await page.locator('.calendar-month-only [data-calendar-tad="31"]').count(), 1);
  assert.equal(await cell("2027-12-22").locator('[data-calendar-tad="32"]').count(), 1, "Corrected exact date appears on its effective day");
  assert.equal(await cell("2027-12-03").locator('[data-calendar-tad="32"]').count(), 0, "Original day remains evidence, not a second calendar event");
  assert.equal(await grid.locator('[data-calendar-tad="33"]').count(), 0, "A correction to month precision cannot keep its original day placement");
  assert.equal(await page.locator('.calendar-month-only [data-calendar-tad="33"]').count(), 1);
  assert.equal(await page.locator(".calendar-undated").count(), 1);
  assert.equal(await grid.locator('[data-calendar-tad="100"], [data-calendar-tad="101"]').count(), 0, "Unresolved dates cannot be placed on a calendar day");
  assert.match(await page.locator(".calendar-undated").textContent(), /AMB|Ambiguous/);
  assert.match(await page.locator(".calendar-undated").textContent(), /NONE|unidentifiable/i);

  // Calendar events use the same saved source evidence as the date feed.
  await event(1).click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor();
  assert.equal(await dialog.locator(".home-tad-card h3 time").innerText(), "Dec 1, 2027");
  const details = dialog.locator("details.tad-evidence");
  if ((await details.getAttribute("open")) === null) await details.locator("summary").click();
  assert.equal(await dialog.locator("blockquote").innerText(), rows[0].context);
  const source = dialog.getByRole("link", { name: /Open original filing/ });
  assert.equal(await source.getAttribute("href"), rows[0].filing_link);
  assert.equal(await source.getAttribute("target"), "_blank");
  assert.equal(await source.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await dialog.locator("img,script").count(), 0);
  assert.equal(await page.evaluate(() => window.calendarExecuted), undefined);
  await button("Close dialog").click();
  await page.locator('.calendar-month-only [data-calendar-tad="3"]').click();
  await dialog.waitFor();
  assert.equal(await dialog.locator(".home-tad-card h3 time").innerText(), "December 2027");
  await button("Close dialog").click();

  // Administrator-entered dates retain manual evidence labels in the calendar.
  await event(30).click();
  await dialog.waitFor();
  assert.equal(await dialog.locator(".home-tad-card h3 time").innerText(), "Dec 21, 2027");
  await dialog.getByText("View entry details", { exact: true }).click();
  assert.equal(await dialog.locator("blockquote").innerText(), rows.find(row => row.id === 30).context);
  assert.match(await dialog.locator("details").innerText(), /Notes:[\s\S]*Entered manually\./);
  assert.equal(await dialog.locator(".source-meta").innerText(), "Manual entry");
  assert.doesNotMatch(await dialog.innerText(), /SEC filing|View filing excerpt|Accession:|Filed:/);
  const manualSource = dialog.getByRole("link", { name: /Open source/ });
  assert.equal(await manualSource.getAttribute("href"), rows.find(row => row.id === 30).filing_link);
  assert.equal(await manualSource.getAttribute("target"), "_blank");
  assert.equal(await manualSource.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await dialog.locator("img,script").count(), 0);
  assert.equal(await page.evaluate(() => window.calendarExecuted), undefined);
  await button("Close dialog").click();
  await page.locator('.calendar-month-only [data-calendar-tad="31"]').click();
  await dialog.waitFor();
  assert.equal(await dialog.locator(".home-tad-card h3 time").innerText(), "December 2027");
  assert.equal(await dialog.locator(".home-tad-card h3 time").getAttribute("datetime"), "2027-12");
  assert.equal(await dialog.locator("summary").innerText(), "View entry details");
  assert.equal(await dialog.getByRole("link").count(), 0, "A manual entry without a source URL has no invented source link");
  await button("Close dialog").click();

  // Corrections show their effective calendar date and keep source history in
  // the dialog without presenting the original date as a second event.
  await event(32).click();
  await dialog.waitFor();
  const editedRow = rows.find(row => row.id === 32);
  assert.equal(await dialog.locator(".home-tad-card h3 time").innerText(), "Dec 22, 2027");
  await dialog.getByText("View entry details", { exact: true }).click();
  assert.match(await dialog.innerText(), /Edited entry[\s\S]*Administrator correction\./);
  assert.equal(await dialog.locator("details > blockquote").innerText(), editedRow.context);
  assert.equal(await dialog.locator(".tad-original-evidence blockquote").innerText(), editedRow.original_context);
  assert.match(await dialog.locator(".tad-original-evidence").innerText(), /Original saved date: Dec 3, 2027[\s\S]*Original saved evidence:/);
  assert.equal(await dialog.getByRole("link", { name: /Open source/ }).getAttribute("href"), editedRow.filing_link);
  const originalSource = dialog.getByRole("link", { name: /Open original source/ });
  assert.equal(await originalSource.getAttribute("href"), editedRow.original_filing_link);
  assert.equal(await originalSource.getAttribute("target"), "_blank");
  assert.equal(await originalSource.getAttribute("rel"), "noopener noreferrer");
  assert.equal(await dialog.locator("img,script").count(), 0);
  assert.equal(await page.evaluate(() => window.calendarExecuted), undefined);
  await button("Close dialog").click();
  await page.locator('.calendar-month-only [data-calendar-tad="33"]').click();
  await dialog.waitFor();
  assert.equal(await dialog.locator(".home-tad-card h3 time").innerText(), "December 2027");
  assert.equal(await dialog.locator(".home-tad-card h3 time").getAttribute("datetime"), "2027-12");
  await dialog.getByText("View entry details", { exact: true }).click();
  assert.match(await dialog.locator(".tad-original-evidence").innerText(), /Original saved date: Dec 30, 2027/);
  assert.equal(await dialog.locator(".source-meta").innerText(), "Manual entry");
  assert.equal(await dialog.getByRole("link").count(), 0, "Unsafe preserved links are suppressed as well as current links");
  await button("Close dialog").click();

  // Month navigation crosses years and includes leap day without changing cache.
  await button("Next month").click();
  await assertMonth("January 2028", 31);
  await assertEvents([5]);
  assert.equal(await cell("2028-01-01").locator('[data-calendar-tad="5"]').count(), 1);
  await button("Next month").click();
  await assertMonth("February 2028", 29);
  await assertEvents([4]);
  assert.equal(await cell("2028-02-29").evaluate(el => el.cellIndex), 2, "February 29, 2028 is Tuesday");
  assert.equal(await cell("2028-02-29").locator('[data-calendar-tad="4"]').count(), 1);
  await button("Previous month").click();
  await assertMonth("January 2028", 31);
  await button("This month").click();
  await assertMonth("December 2027", 31);
  await assertEvents(decemberIds);

  await tab("Target action dates").click();
  await page.locator(".home-tad-card").first().waitFor();
  await tab("Calendar").click();
  await assertEvents(decemberIds);
  assert.equal(tadRequests(), 1, "Calendar and TAD views must share one saved-results response");
  await tab("Target action dates").focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("Trials").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");

  // Search filters the saved collection before grouping; it leaves month choice intact.
  await search.fill("Year End Research");
  await assertEvents([2]);
  await heading("December 2027").waitFor();
  await search.fill("Month Only Research");
  await assertEvents([]);
  assert.equal(await page.locator('.calendar-month-only [data-calendar-tad="3"]').count(), 1);
  await search.fill("nonexistent-program-sentinel");
  await assertEvents([]);
  await page.waitForFunction(() => document.querySelectorAll("[data-calendar-tad]").length === 0);
  await search.fill("");
  await assertEvents(decemberIds);
  await button("Next month").click();
  await search.fill("NEXT");
  await assertEvents([5]);
  await heading("January 2028").waitFor();
  await search.fill("");
  await button("This month").click();
  await assertEvents(decemberIds);

  const screenshotDir = path.join(__dirname, "../artifacts");
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: path.join(screenshotDir, "calendar-desktop.png"), fullPage: true });
  for (const width of [360, 390, 768, 1024, 1512]) {
    await page.setViewportSize({ width, height: 950 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Calendar must fit ${width}px`);
    if (width === 390) await page.screenshot({ path: path.join(screenshotDir, "calendar-mobile.png"), fullPage: true });
  }

  mode = "empty";
  await button("Refresh dates").click();
  await page.getByText("No reported target action dates for this month.", { exact: true }).waitFor();
  await assertEvents([]);
  await page.waitForFunction(() => document.querySelectorAll("[data-calendar-tad]").length === 0);
  mode = "error";
  await button("Refresh dates").click();
  await page.getByRole("heading", { name: /Unable to load/ }).waitFor();
  assert.match(await panel.innerText(), /Saved dates are temporarily unavailable/);
  mode = "populated";
  await button("Try again").click();
  await assertMonth("December 2027", 31);
  await assertEvents(decemberIds);
  const beforeRefresh = tadRequests();
  currentRows = [dated(200, "2027-12-25", { ticker: "FRESH", company: "Refreshed Calendar Company" })];
  await button("Refresh dates").click();
  await assertEvents([200]);
  assert.equal(tadRequests(), beforeRefresh + 1);
  await tab("Target action dates").click();
  await page.waitForFunction(() => document.querySelectorAll("#home-feed-panel .home-tad-card").length === 1);
  assert.match(await panel.locator(".home-tad-card").innerText(), /Refreshed Calendar Company/);
  assert.equal(tadRequests(), beforeRefresh + 1, "Refreshing Calendar updates the shared TAD view");
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  assert.equal(requests.some(r => r.startsWith("POST ") || /\/api\/(?:sync|jobs|review|inbox|home\/calendar)/.test(r)), false, "Calendar reads saved TAD results only");
  console.log(JSON.stringify({ result: "passed", checks: "calendar tab and keyboard order, current local month, exact dates without shifts, multiple events per day, all rows beyond list page size, month-only separation, manual dates and notes/source dialog, corrections preserve original evidence, unresolved separation, evidence/source dialog, year transition and leap day, shared cache/refresh, search before grouping, loading/empty/error/retry, responsive layout", liveDatabaseReads: 0, liveDatabaseWrites: 0 }));
  await browser.close();
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exitCode = 1; });
