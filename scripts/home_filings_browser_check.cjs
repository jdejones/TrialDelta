// Isolated homepage filings regression: assets come from this checkout and all
// API calls are mocked. No app server, external network, or database is used.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const hostile = '<img src=x onerror="window.filingExecuted=true"> & <script>window.filingExecuted=true</script>';
const rows = Array.from({ length: 63 }, (_, index) => ({
  ticker: `F${String(index + 1).padStart(3, "0")}`,
  company: index >= 15 && index < 48 ? "Research cohort company " + (index + 1) : "Filing company " + (index + 1),
  form_type: "8-K",
  accession_no: `0001234567-26-${String(index + 1).padStart(6, "0")}`,
  filed_at: new Date(Date.UTC(2026, 8, 26 - index, 12)).toISOString(),
  filing_link: `https://www.sec.gov/Archives/edgar/data/1234567/fixture-${index + 1}.htm?x=1&y=2`,
}));
rows[1] = { ...rows[1], company: hostile, accession_no: hostile, filing_link: "javascript:window.filingExecuted=true" };
rows[2] = { ...rows[2], filing_link: null };
let browser;
(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  const context = await browser.newContext({ viewport: { width: 1512, height: 1050 }, locale: "en-US", timezoneId: "America/Los_Angeles", serviceWorkers: "block" });
  const page = await context.newPage();
  const errors = [], unexpected = [], requests = [];
  let mode = "populated", delayQuery = null, releaseFeed, feedGate, delayStarted, markDelayStarted;
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
    if (req.method() === "GET") {
      if (pathname === "/api/auth/config") return respond({ auth_required: false, public_browsing_enabled: false, registration_enabled: false });
      if (pathname === "/api/me") return respond({ id: 996001, display_name: "Filings Reader", email: "filings@example.org", is_admin: false, mode: "local" });
      if (pathname === "/api/companies") return respond([{ id: 996063, ticker: "F063", name: "Filing company 63", study_count: 0, followed: false }]);
      if (["/api/studies", "/api/follows", "/api/changes", "/api/home/trials", "/api/home/news", "/api/home/tads"].includes(pathname)) return respond([]);
      if (pathname === "/api/home/tads/page") return respond({ items: [], total: 0, identified: 0, month: 0, unresolved: 0, offset: Number(url.searchParams.get("offset") || 0), limit: Number(url.searchParams.get("limit") || 100) });
      if (pathname === "/api/home/filings") {
        const query = url.searchParams.get("q") || "";
        const pageNumber = Number(url.searchParams.get("page"));
        const pageSize = Number(url.searchParams.get("page_size"));
        requests.push({ page: pageNumber, page_size: pageSize, q: query });
        assert.ok(Number.isInteger(pageNumber) && pageNumber >= 1, "Every request must specify a positive page");
        assert.ok([10, 25, 50, 100].includes(pageSize), "Every request must have a bounded page size");
        const capturedMode = mode;
        const filtered = capturedMode === "empty" ? [] : rows.filter(row => !query || [row.ticker, row.company, row.accession_no, row.form_type].some(value => value.toLowerCase().includes(query.toLowerCase())));
        if (capturedMode === "delayed" && (delayQuery === null || delayQuery === query)) { markDelayStarted(); await feedGate; }
        if (capturedMode === "error") return respond({ detail: "Saved filings are temporarily unavailable." }, 503);
        const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
        const currentPage = Math.min(pageNumber, totalPages);
        return respond({ items: filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize), page: currentPage, page_size: pageSize, total: filtered.length, total_pages: totalPages });
      }
    }
    unexpected.push(`${req.method()} ${pathname}`);
    return respond({ detail: "Unexpected filings regression request blocked." }, 500);
  });
  const tab = name => page.getByRole("tab", { name, exact: true });
  const button = name => page.getByRole("button", { name, exact: true });
  const panel = page.locator("#home-feed-panel");
  const cards = page.locator(".home-filing-card");
  const search = page.locator("#search");
  const pageSize = page.getByRole("combobox", { name: "Filings per page", exact: true });
  const pagination = page.getByRole("navigation", { name: "8-Ks pagination", exact: true });
  const beginDelay = query => { mode = "delayed"; delayQuery = query ?? null; feedGate = new Promise(resolve => { releaseFeed = resolve; }); delayStarted = new Promise(resolve => { markDelayStarted = resolve; }); };
  async function assertRows(expected, currentPage, size, total = expected.length) {
    await page.waitForFunction(expectedDates => JSON.stringify([...document.querySelectorAll(".home-filing-card time")].map(node => node.getAttribute("datetime"))) === JSON.stringify(expectedDates), expected.map(row => row.filed_at));
    assert.equal(await cards.count(), expected.length);
    for (let index = 0; index < expected.length; index++) {
      const text = await cards.nth(index).innerText();
      assert.ok(text.includes(expected[index].ticker), "The filing ticker is visible");
      assert.ok(text.includes(expected[index].company), "The filing company stays plain text");
      assert.ok(text.includes(expected[index].accession_no), "The accession identifies the original filing");
    }
    assert.equal(await pageSize.inputValue(), String(size));
    if (total > 0) {
      assert.equal(await pagination.locator('[aria-current="page"]').innerText(), String(currentPage));
      assert.equal(await pagination.getByRole("button", { name: "Previous", exact: true }).isDisabled(), currentPage === 1);
      assert.equal(await pagination.getByRole("button", { name: "Next", exact: true }).isDisabled(), currentPage === Math.ceil(total / size));
      assert.equal(await pagination.evaluate(nav => nav.closest(".feed-pagination") === document.querySelector("#home-feed-panel").lastElementChild), true, "Pagination is below the filings");
    }
  }
  async function waitRequest(expected) {
    await page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === "/api/home/filings" && Object.entries(expected).every(([key, value]) => (url.searchParams.get(key) || "") === String(value));
    });
  }
  await page.goto("http://trialdelta.test", { waitUntil: "networkidle" });
  assert.equal(requests.length, 0, "Filings must not load until their tab is selected");
  assert.equal(await tab("8-Ks").isDisabled(), false);
  beginDelay();
  await tab("8-Ks").click();
  await panel.getByRole("status").waitFor();
  assert.equal(await cards.count(), 0);
  assert.equal(requests.length, 1, "Activation requests one bounded page");
  assert.deepEqual(requests[0], { page: 1, page_size: 10, q: "" });
  releaseFeed(); mode = "populated";
  await assertRows(rows.slice(0, 10), 1, 10, rows.length);
  assert.equal(await pageSize.getAttribute("id"), "home-filings-page-size");
  assert.deepEqual(await pageSize.locator("option").evaluateAll(options => options.map(option => option.value)), ["10", "25", "50", "100"]);
  assert.equal(await cards.locator("img,script").count(), 0);
  assert.equal(await page.evaluate(() => window.filingExecuted), undefined);
  const source = cards.first().getByRole("link");
  assert.equal(await source.getAttribute("href"), rows[0].filing_link);
  assert.equal(await source.getAttribute("target"), "_blank");
  assert.match(await source.getAttribute("rel"), /noopener/);
  assert.match(await source.getAttribute("rel"), /noreferrer/);
  assert.equal(await cards.nth(1).getByRole("link").count(), 0, "Executable source URLs are suppressed");
  assert.equal(await cards.nth(2).getByRole("link").count(), 0, "Missing URLs do not produce invented links");
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await assertRows(rows.slice(10, 20), 2, 10, rows.length);
  assert.deepEqual(requests.at(-1), { page: 2, page_size: 10, q: "" });
  assert.equal(await panel.evaluate(node => node === document.activeElement), true, "Page changes focus the feed");
  await pagination.getByRole("button", { name: "Go to page 7", exact: true }).click();
  await assertRows(rows.slice(60), 7, 10, rows.length);
  await pageSize.selectOption("25");
  await assertRows(rows.slice(0, 25), 1, 25, rows.length);
  assert.deepEqual(requests.at(-1), { page: 1, page_size: 25, q: "" }, "Changing page size resets to page 1");
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await assertRows(rows.slice(25, 50), 2, 25, rows.length);
  await search.fill("Research cohort");
  const cohort = rows.filter(row => row.company.includes("Research cohort"));
  await assertRows(cohort.slice(0, 25), 1, 25, cohort.length);
  assert.deepEqual(requests.at(-1), { page: 1, page_size: 25, q: "Research cohort" }, "Search is sent to the server and resets pagination");
  await pagination.getByRole("button", { name: "Next", exact: true }).click();
  await assertRows(cohort.slice(25), 2, 25, cohort.length);
  assert.deepEqual(requests.at(-1), { page: 2, page_size: 25, q: "Research cohort" });
  await search.fill(rows.at(-1).accession_no);
  await assertRows(rows.slice(-1), 1, 25, 1);
  assert.deepEqual(requests.at(-1), { page: 1, page_size: 25, q: rows.at(-1).accession_no }, "Search can find a filing outside loaded pages");
  await search.fill("");
  await assertRows(rows.slice(0, 25), 1, 25, rows.length);
  await pageSize.selectOption("50");
  await assertRows(rows.slice(0, 50), 1, 50, rows.length);
  await pageSize.selectOption("100");
  await assertRows(rows, 1, 100, rows.length);
  await pageSize.selectOption("10");
  await assertRows(rows.slice(0, 10), 1, 10, rows.length);

  // A slow earlier search cannot overwrite the most recently selected query.
  beginDelay("Filing company 4");
  await search.fill("Filing company 4");
  await delayStarted;
  await panel.getByRole("status").filter({ hasText: "Loading" }).waitFor();
  mode = "populated";
  await search.fill("F063");
  await assertRows(rows.slice(-1), 1, 10, 1);
  const oldResponse = waitRequest({ q: "Filing company 4" });
  releaseFeed();
  await oldResponse;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await assertRows(rows.slice(-1), 1, 10, 1);
  assert.equal(await search.inputValue(), "F063");

  // Empty and retry states retain page-size controls and stay read only.
  await search.fill("no-such-filing-sentinel");
  await panel.getByRole("heading", { name: /No matching|No 8-K|No filings/i }).waitFor();
  assert.equal(await cards.count(), 0);
  assert.equal(await pageSize.inputValue(), "10");
  mode = "error";
  await search.fill("Filing company");
  await panel.getByRole("heading", { name: /Unable to load/ }).waitFor();
  assert.match(await panel.innerText(), /Saved filings are temporarily unavailable/);
  mode = "populated";
  await button("Try again").click();
  const matching = rows.filter(row => row.company.includes("Filing company"));
  await assertRows(matching.slice(0, 10), 1, 10, matching.length);
  await search.fill("");
  await assertRows(rows.slice(0, 10), 1, 10, rows.length);

  mode = "empty";
  await page.reload({ waitUntil: "networkidle" });
  await tab("8-Ks").click();
  await panel.getByRole("heading", { name: "No 8-K filings yet.", exact: true }).waitFor();
  assert.equal(await cards.count(), 0);
  assert.equal(await pageSize.inputValue(), "10");
  mode = "populated";
  await page.reload({ waitUntil: "networkidle" });
  await tab("8-Ks").click();
  await assertRows(rows.slice(0, 10), 1, 10, rows.length);

  // Company navigation changes the global query; Back must request that query.
  const beforeCompanyNavigation = requests.length;
  await page.getByRole("button", { name: "View Filing company 63 trials", exact: true }).click();
  await page.getByRole("heading", { name: "Your research universe.", exact: true }).waitFor();
  await page.goBack();
  await assertRows(rows.slice(-1), 1, 10, 1);
  assert.equal(requests.length, beforeCompanyNavigation + 1);
  assert.deepEqual(requests.at(-1), { page: 1, page_size: 10, q: "F063" }, "Back must not reuse cached filings from the previous query");
  await search.fill("");
  await assertRows(rows.slice(0, 10), 1, 10, rows.length);

  // The newly enabled tab participates in the existing roving keyboard order.
  await tab("Target action dates").click();
  await tab("Target action dates").focus();
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  assert.equal(await tab("8-Ks").evaluate(node => node === document.activeElement), true);
  await assertRows(rows.slice(0, 10), 1, 10, rows.length);
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  await assertRows(rows.slice(0, 10), 1, 10, rows.length);

  const screenshotDir = path.join(__dirname, "../artifacts");
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: path.join(screenshotDir, "home-filings-desktop.png"), fullPage: true });
  for (const width of [360, 390, 768, 1024, 1512]) {
    await page.setViewportSize({ width, height: 950 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Filings must fit ${width}px`);
    if (width === 390) await page.screenshot({ path: path.join(screenshotDir, "home-filings-mobile.png"), fullPage: true });
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  console.log(JSON.stringify({ result: "passed", checks: "lazy bounded filings requests, default and selectable page sizes, server pagination and search/reset, final page, loading/empty/error/retry, stale response protection and navigation invalidation, safe source links and escaped metadata, keyboard navigation, responsive layout", liveDatabaseReads: 0, liveDatabaseWrites: 0, filingRequests: requests.length }));
  await browser.close();
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exitCode = 1; });
