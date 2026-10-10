// Bounded, guest-only verification of the published portfolio. All network
// requests are restricted to the supplied HTTPS origin and GET/HEAD methods.
// No administrator credentials, publisher navigation, or database writes.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const supplied = process.env.TRIALDELTA_PUBLIC_URL;
assert.ok(supplied, "Set TRIALDELTA_PUBLIC_URL to the deployed HTTPS URL");
const origin = new URL(supplied).origin;
assert.equal(new URL(origin).protocol, "https:", "Use the deployed HTTPS origin");
const expectedSnapshot = process.env.TRIALDELTA_SNAPSHOT_DATE || "2026-10-09";
const artifactDirectory = path.join(__dirname, "../artifacts/portfolio/live-browser");
const report = {
  result: "running", url: origin, snapshotDate: expectedSnapshot,
  checks: [], data: {}, javascriptErrors: [], failedAPIrequests: [],
  blockedRequests: [], apiRequests: 0, writes: 0, screenshots: [],
};
let browser, watchdog;

(async () => {
  fs.mkdirSync(artifactDirectory, { recursive: true });
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  watchdog = setTimeout(() => {
    report.failedAPIrequests.push({ error: "Overall 240-second verification limit exceeded" });
    browser.close().catch(() => {});
  }, 240000);
  const context = await browser.newContext({ viewport: { width: 1512, height: 1050 }, locale: "en-US", timezoneId: "America/Chicago", serviceWorkers: "block" });
  const page = await context.newPage();
  page.setDefaultTimeout(30000);
  page.setDefaultNavigationTimeout(45000);
  page.on("pageerror", error => report.javascriptErrors.push(error.message));
  page.on("requestfailed", request => {
    const url = new URL(request.url());
    if (url.origin === origin && url.pathname.startsWith("/api/") && request.failure()?.errorText !== "net::ERR_ABORTED") {
      report.failedAPIrequests.push({ path: url.pathname, error: request.failure()?.errorText || "Request failed" });
    }
  });
  page.on("response", response => {
    const url = new URL(response.url());
    if (url.origin === origin && url.pathname.startsWith("/api/") && response.status() >= 400
        && !(url.pathname === "/api/me" && response.status() === 401)) {
      report.failedAPIrequests.push({ path: url.pathname, status: response.status() });
    }
  });
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin || !["GET", "HEAD"].includes(request.method())) {
      if (!["GET", "HEAD"].includes(request.method())) report.writes += 1;
      report.blockedRequests.push({ origin: url.origin, path: url.pathname, method: request.method() });
      return route.abort("blockedbyclient");
    }
    if (url.pathname.startsWith("/api/") && ++report.apiRequests > 100) {
      report.blockedRequests.push({ path: url.pathname, reason: "100 API request limit" });
      return route.abort("blockedbyclient");
    }
    return route.continue();
  });
  const navigation = name => page.getByRole("navigation", { name: "Main navigation" }).getByRole("button", { name, exact: true });
  const button = name => page.getByRole("button", { name, exact: true });
  const tab = name => page.getByRole("tab", { name, exact: true });
  const search = page.locator("#search");
  const panel = page.locator("#home-feed-panel");
  const dialog = page.getByRole("dialog");
  async function apiAfter(pathname, action, query = {}) {
    const pending = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.origin === origin && url.pathname === pathname
        && Object.entries(query).every(([key, value]) => (url.searchParams.get(key) || "") === String(value));
    });
    await action();
    const response = await pending;
    assert.equal(response.status(), 200, `${pathname} succeeds`);
    const json = await response.json();
    await page.waitForFunction(() => !document.querySelector("#main .loading"));
    return { json, response };
  }
  async function closeDialog() { await dialog.getByRole("button", { name: "Close dialog", exact: true }).click(); }
  async function assertGuestControls(scope = page.locator(".visitor-actions")) {
    assert.ok(await scope.locator('[data-auth="login"]').isDisabled());
    assert.ok(await scope.locator('[data-auth="signup"]').isDisabled());
    const notice = scope.locator(".alpha-account-notice");
    assert.equal(await notice.innerText(), "Account creation is disabled during alpha development.");
    assert.ok(await notice.isVisible());
    assert.equal(await notice.evaluate(node => getComputedStyle(node).color), "rgb(180, 35, 24)");
  }
  async function screenshot(name) {
    const filename = path.join(artifactDirectory, name);
    await page.evaluate(() => { window.scrollTo(0, 0); return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); });
    await page.screenshot({ path: filename, fullPage: true });
    report.screenshots.push(filename);
  }
  async function checkHomeFeed(name, pathname, cardSelector, searchValue) {
    const first = await apiAfter(pathname, () => tab(name).click());
    const feed = first.json;
    assert.ok(Array.isArray(feed.items));
    assert.ok(feed.items.length <= 10, `${name} defaults to at most ten records`);
    report.data[name] = { total: feed.total, firstPageRows: feed.items.length };
    const cards = panel.locator(cardSelector);
    assert.equal(await cards.count(), feed.items.length);
    if (feed.total > 10) {
      await apiAfter(pathname, () => panel.getByRole("button", { name: "Next", exact: true }).click(),
        name === "Target action dates" ? { offset: 10 } : { page: 2 });
      assert.match(await panel.locator(".pagination-summary").innerText(), /^Showing 11/);
    }
    const term = searchValue(feed.items[0]);
    if (term) {
      const filtered = await apiAfter(pathname, () => search.fill(term), { q: term });
      assert.ok(filtered.json.total > 0, `${name} search finds the selected record`);
      assert.ok(filtered.json.items.length <= 10);
      await apiAfter(pathname, () => search.fill(""), { q: "" });
    }
    report.checks.push(`${name} bounded feed, search and available pagination`);
    return feed;
  }

  const home = await apiAfter("/api/home/trials", () => page.goto(origin + "/", { waitUntil: "domcontentloaded" }));
  assert.ok(home.json.length > 0 && home.json.length <= 100);
  await assertGuestControls();
  assert.match(await page.locator(".snapshot-notice").innerText(), new RegExp(expectedSnapshot));
  assert.equal(await page.locator(".home-trial-card").count(), Math.min(home.json.length, 10));
  report.checks.push("HTTPS home, guest controls, red alpha notice and snapshot date");
  await screenshot("home-desktop.png");

  // Open saved company trial history and verify the same-origin download UI.
  const studyId = home.json[0].id;
  const study = await apiAfter(`/api/studies/${studyId}`, () => page.locator(`.home-trial-card [data-study="${studyId}"]`).first().click());
  await dialog.waitFor();
  assert.ok(await dialog.getByRole("heading", { name: "Registry source history", exact: true }).isVisible());
  assert.ok(study.json.snapshots.length > 0);
  const downloadLink = dialog.locator('a[href^="/api/snapshots/"]').first();
  const downloadURL = new URL(await downloadLink.getAttribute("href"), origin);
  assert.equal(downloadURL.origin, origin);
  // Read one attachment in memory; no external publisher request or file save.
  report.apiRequests += 1;
  const attachment = await context.request.get(downloadURL.href, { timeout: 30000, maxRedirects: 0 });
  assert.equal(attachment.status(), 200);
  assert.match(attachment.headers()["content-disposition"], /attachment; filename="NCT\d{8}-\d+\.json"/);
  const raw = await attachment.json();
  assert.equal(raw.protocolSection.identificationModule.nctId, studyId);
  assert.ok(study.json.snapshots.every(row => !Object.hasOwn(row, "raw_json") && !Object.hasOwn(row, "normalized")));
  report.data.study = { id: studyId, snapshotCount: study.json.snapshots.length, publicDownload: "verified" };
  report.checks.push("Trial details, registry source history and same-origin JSON attachment");
  await screenshot("trial-history-desktop.png");
  await closeDialog();

  await navigation("Coverage").click();
  const company = page.locator("tbody tr").filter({ has: page.locator("[data-company]") }).first();
  const companyName = await company.locator(".name-button").innerText();
  const companyId = await company.locator(".name-button").getAttribute("data-company");
  const companyStudies = await apiAfter("/api/studies", () => company.locator(".name-button").click(), { company_id: companyId, limit: 25, offset: 0 });
  assert.ok(companyStudies.json.every(row => String(row.company_id) === companyId));
  assert.match(await page.locator(".program-scope").innerText(), new RegExp(companyName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  await button("Explore all programs").click();
  await page.waitForFunction(() => !document.querySelector("#main .loading") && document.querySelector("[aria-label='Clinical programs pagination']"));
  const programs = page.getByRole("navigation", { name: "Clinical programs pagination" });
  assert.ok(await page.locator("tbody tr").count() <= 25);
  if (!(await programs.getByRole("button", { name: "Next", exact: true }).isDisabled())) {
    await apiAfter("/api/studies", () => programs.getByRole("button", { name: "Next", exact: true }).click(), { limit: 25, offset: 25 });
    assert.match(await page.locator(".pagination-summary").innerText(), /^Showing 26/);
  }
  const searchedStudy = await apiAfter("/api/studies", () => search.fill(studyId), { q: studyId, limit: 25, offset: 0 });
  assert.ok(searchedStudy.json.some(row => row.id === studyId));
  await page.locator("[data-follow-study]").first().click();
  await assertGuestControls(dialog);
  await dialog.getByRole("button", { name: "Continue browsing", exact: true }).click();
  report.checks.push("Company-scoped programs, dataset search/pagination and guest invitation controls");

  const changes = await apiAfter("/api/changes", () => navigation("Change log").click(), { followed: "false", limit: 10, offset: 0 });
  assert.ok(Array.isArray(changes.json) && changes.json.length <= 10);
  report.data.changes = { total: Number(changes.response.headers()["x-total-count"]), firstPageRows: changes.json.length };
  if (report.data.changes.total > 10) {
    await apiAfter("/api/changes", () => page.getByRole("navigation", { name: "Change log pagination" }).getByRole("button", { name: "Next", exact: true }).click(), { limit: 10, offset: 10 });
  }
  if (changes.json[0]) {
    const term = changes.json[0].ticker;
    const result = await apiAfter("/api/changes", () => search.fill(term), { q: term, offset: 0 });
    assert.ok(result.json.length > 0);
    await apiAfter("/api/changes", () => search.fill(""), { q: "" });
    const changeId = changes.json[0].id;
    await apiAfter(`/api/changes/${changeId}`, () => page.locator(`[data-change="${changeId}"]`).first().click());
    await dialog.waitFor();
    await closeDialog();
  } else {
    assert.ok(await page.locator("#change-feed-panel .empty").isVisible());
  }
  report.checks.push("Published change log, detail/search/pagination when data exists");
  await navigation("Home").click();
  const news = await checkHomeFeed("News", "/api/home/news/page", ".home-news-card", row => row?.ticker);
  assert.ok(news.total > 0, "Exported healthcare news is available");
  assert.equal(await page.locator(".home-news-card .badge").first().innerText(), "Healthcare");
  for (const row of news.items) assert.ok(row.published_at.slice(0, 10) >= "2026-10-02" && row.published_at.slice(0, 10) <= expectedSnapshot);
  await screenshot("news-desktop.png");
  const filings = await checkHomeFeed("8-Ks", "/api/home/filings", ".home-filing-card", row => row?.ticker);
  assert.ok(filings.total > 0, "Exported SEC filings are available");
  assert.ok(filings.items.every(row => !Object.hasOwn(row, "filing")));
  await screenshot("filings-desktop.png");
  const tadFeed = await checkHomeFeed("Target action dates", "/api/home/tads/page", ".home-tad-card", row => row?.ticker);
  await tab("Calendar").click();
  await page.locator(".tad-calendar").waitFor();
  assert.ok(await page.locator(".calendar-count").isVisible());
  const initialMonth = await page.locator("#calendar-month-label").innerText();
  await page.getByRole("button", { name: "Next month", exact: true }).click();
  await page.locator(".tad-calendar").waitFor();
  assert.notEqual(await page.locator("#calendar-month-label").innerText(), initialMonth);
  await page.getByRole("button", { name: "This month", exact: true }).click();
  await page.locator(".tad-calendar").waitFor();
  if (tadFeed.items[0]?.ticker) {
    const term = tadFeed.items[0].ticker;
    await apiAfter("/api/home/tads/page", () => search.fill(term), { q: term, status: "identified" });
    assert.ok(await page.locator(".tad-calendar").isVisible());
    await apiAfter("/api/home/tads/page", () => search.fill(""), { q: "", status: "identified" });
  }
  if (await page.locator(".calendar-event").count()) {
    await page.locator(".calendar-event").first().click();
    await dialog.waitFor();
    await closeDialog();
  }
  if (await page.locator(".calendar-undated").count()) {
    await page.locator(".calendar-undated > summary").click();
    assert.ok(await page.locator(".calendar-undated .home-tad-card").count() <= 10);
    const unresolvedNext = page.getByRole("navigation", { name: "Unresolved dates pagination" }).getByRole("button", { name: "Next", exact: true });
    if (await unresolvedNext.count() && !(await unresolvedNext.isDisabled())) {
      await apiAfter("/api/home/tads/page", () => unresolvedNext.click(), { status: "unresolved", offset: 10 });
    }
  }
  report.checks.push("Calendar month navigation, event evidence and bounded unresolved pagination");
  await screenshot("calendar-desktop.png");
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await screenshot("calendar-mobile.png");
  await navigation("Home").click();
  await assertGuestControls();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await screenshot("home-mobile.png");

  await page.goto(origin + "/#login", { waitUntil: "domcontentloaded" });
  await page.locator("#login-form").waitFor();
  assert.ok(await page.locator("#login-email").isVisible());
  assert.ok(await page.locator("#login-password").isVisible());
  assert.equal(await page.locator("#login-form").getByRole("button", { name: "Sign in", exact: true }).isDisabled(), false);
  report.checks.push("Direct administrator login form remains available without submitting credentials");
  await screenshot("login-mobile.png");
  assert.deepEqual(report.javascriptErrors, []);
  assert.deepEqual(report.failedAPIrequests, []);
  assert.deepEqual(report.blockedRequests, []);
  assert.equal(report.writes, 0);
  report.result = "passed";
})().catch(error => {
  report.result = "failed";
  report.error = error.message;
  process.exitCode = 1;
}).finally(async () => {
  clearTimeout(watchdog);
  if (browser) await browser.close().catch(() => {});
  fs.mkdirSync(artifactDirectory, { recursive: true });
  fs.writeFileSync(path.join(artifactDirectory, "report.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report));
});
