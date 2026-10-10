// Portfolio regressions use only local assets and mocked, bounded API responses.
// No application database, AWS resource, external service, or personal data is used.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const companies = [
  { id: 991, ticker: "ALFA", name: "Alpha Healthcare Fixture", study_count: 620, followed: false },
  { id: 992, ticker: "BETA", name: "Beta Device Fixture", study_count: 3, followed: false },
];
const studies = Array.from({ length: 623 }, (_, index) => ({
  id: `NCT${String(99100000 + index).padStart(8, "0")}`, company_id: index < 620 ? 991 : 992,
  ticker: index < 620 ? "ALFA" : "BETA", company: index < 620 ? companies[0].name : companies[1].name,
  title: `Clinical program ${index + 1}`, drug: index === 600 ? "Remote intervention" : `Intervention ${index + 1}`,
  targets: ["MET"], conditions: ["Cardiac condition"], status: "RECRUITING", phase: "PHASE2", primary_completion: "2028-06", added_at: "2026-10-09T12:00:00Z",
}));
const news = Array.from({ length: 23 }, (_, index) => ({ ticker: index < 20 ? "BETA" : "ALFA", company: "Device news fixture", title: `Healthcare headline ${index + 1}`, source: "Fixture", published_at: "2026-10-09T12:00:00Z", url: `https://example.org/article/${index}` }));
const changes = Array.from({ length: 33 }, (_, index) => ({ id: index + 1, company: companies[0].name, ticker: "ALFA", category: index > 20 ? "enrollment" : "timeline", title: `Saved change ${index + 1}`, before_value: "120", after_value: "180", source: "registry", detected_at: "2026-10-09T12:00:00Z" }));
const tads = Array.from({ length: 528 }, (_, index) => ({ id: index + 1, ticker: `T${index + 1}`, company: "Target-date fixture", status: index < 503 ? "identified" : "unidentifiable", tad: index < 502 ? "2026-10-20" : index === 502 ? "2026-10" : null, precision: index < 502 ? "day" : index === 502 ? "month" : "unknown", context: `Saved evidence ${index + 1}`, reason: "Reported target action date", source_count: 1, filing_link: "https://example.org/filing", form_type: "8-K", accession_no: `fixture-${index + 1}` }));
tads[501] = { ...tads[501], evidence_method: "edited", is_edited: true, original_tad: "2026-09-20", original_context: "Original preserved evidence", context: "Administrator corrected evidence", original_filing_link: "https://example.org/original" };
let browser;
(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  const context = await browser.newContext({ viewport: { width: 1512, height: 1100 }, locale: "en-US", timezoneId: "America/Chicago", serviceWorkers: "block" });
  const page = await context.newPage(), errors = [], unexpected = [], requests = [];
  let administrator = false, releaseOld, oldStarted;
  const oldGate = new Promise(resolve => { releaseOld = resolve; });
  let markOld;
  oldStarted = new Promise(resolve => { markOld = resolve; });
  page.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
    const respond = (json, status = 200, headers = {}) => route.fulfill({ status, json, headers });
    if (url.origin !== "http://trialdelta.test") { unexpected.push(request.url()); return route.abort(); }
    const assets = { "/": ["index.html", "text/html"], "/static/app.js": ["app.js", "text/javascript"], "/static/styles.css": ["styles.css", "text/css"], "/static/base.css": ["base.css", "text/css"], "/static/favicon.svg": ["favicon.svg", "image/svg+xml"] };
    if (assets[pathname]) {
      const [file, contentType] = assets[pathname];
      return route.fulfill({ status: 200, contentType, body: fs.readFileSync(path.join(__dirname, "../trialdelta/static", file)) });
    }
    requests.push({ path: pathname, params: Object.fromEntries(url.searchParams), method: request.method() });
    const query = (url.searchParams.get("q") || "").toLowerCase();
    if (pathname === "/api/auth/config") return respond({ auth_required: true, public_browsing_enabled: true, registration_enabled: false, portfolio_snapshot_date: "2026-10-09", password_min_length: 14 });
    if (pathname === "/api/me") return administrator ? respond({ id: 77, email: "admin@example.org", display_name: "Fixture Administrator", is_admin: true, mode: "hosted" }) : respond({ detail: "Please sign in" }, 401);
    if (pathname === "/api/login") { administrator = true; return respond({ ok: true }); }
    if (pathname === "/api/logout") { administrator = false; return respond({ ok: true }); }
    if (pathname === "/api/companies") return respond(companies);
    if (pathname === "/api/follows") return respond([]);
    if (pathname === "/api/home/trials") return respond(studies.slice(0, 10));
    if (pathname === "/api/studies") {
      const companyId = Number(url.searchParams.get("company_id")), limit = Number(url.searchParams.get("limit")), offset = Number(url.searchParams.get("offset"));
      assert.ok(limit > 0 && limit <= 500 && offset >= 0, "Study requests must be bounded");
      const rows = studies.filter(s => (!companyId || s.company_id === companyId) && (!query || JSON.stringify(s).toLowerCase().includes(query)));
      return respond(rows.slice(offset, offset + limit), 200, { "X-Total-Count": String(rows.length) });
    }
    if (pathname === "/api/changes") {
      const limit = Number(url.searchParams.get("limit")), offset = Number(url.searchParams.get("offset")), category = url.searchParams.get("category");
      assert.equal(limit, 10);
      const rows = changes.filter(c => (!query || c.title.toLowerCase().includes(query)) && (!category || c.category === category));
      return respond(rows.slice(offset, offset + limit), 200, { "X-Total-Count": String(rows.length) });
    }
    if (pathname === "/api/home/news/page") {
      const size = Number(url.searchParams.get("page_size"));
      assert.equal(size, 10);
      let rows = news.filter(n => !query || JSON.stringify(n).toLowerCase().includes(query));
      if (query === "old") { markOld(); await oldGate; rows = [{ ...news[0], title: "Old response must not replace latest results" }]; }
      if (query === "new") rows = [{ ...news[0], title: "Newest requested results" }];
      const totalPages = Math.max(1, Math.ceil(rows.length / size)), current = Math.min(Number(url.searchParams.get("page")), totalPages);
      return respond({ items: rows.slice((current - 1) * size, current * size), page: current, page_size: size, total: rows.length, total_pages: totalPages });
    }
    if (pathname === "/api/home/tads/page") {
      const limit = Number(url.searchParams.get("limit")), offset = Number(url.searchParams.get("offset")), month = url.searchParams.get("month"), status = url.searchParams.get("status");
      assert.ok(limit > 0 && limit <= 500 && offset >= 0);
      const rows = tads.filter(row => (!query || JSON.stringify(row).toLowerCase().includes(query)) && (!month || row.tad?.startsWith(month)) && (!status || (status === "unresolved" ? row.status !== "identified" : row.status === status)));
      return respond({ items: rows.slice(offset, offset + limit), total: rows.length, identified: rows.filter(r => r.status === "identified").length, month: rows.filter(r => r.precision === "month").length, unresolved: rows.filter(r => r.status !== "identified").length, limit, offset });
    }
    if (pathname === "/api/home/filings") return respond({ items: [], total: 0, total_pages: 1, page: 1, page_size: 10 });
    if (pathname === "/api/themes") return respond(query ? ["Remote intervention"] : ["Cardiac monitoring device", "Remote intervention"]);
    unexpected.push(`${request.method()} ${pathname}`);
    return respond({ detail: "Unexpected portfolio test request blocked" }, 500);
  });
  const nav = name => page.getByRole("navigation", { name: "Main navigation" }).getByRole("button", { name, exact: true });
  const button = name => page.getByRole("button", { name, exact: true });
  const search = page.locator("#search");
  await page.goto("http://trialdelta.test", { waitUntil: "networkidle" });
  await page.locator(".home-trial-card").first().waitFor();
  assert.equal(requests.filter(r => ["/api/studies", "/api/changes"].includes(r.path)).length, 0, "Boot must not load the clinical universe or change history");
  assert.ok(await page.locator('.visitor-actions [data-auth="login"]').isDisabled());
  assert.ok(await page.locator('.visitor-actions [data-auth="signup"]').isDisabled());
  assert.ok(await page.getByText("Account creation is disabled during alpha development.", { exact: true }).isVisible());
  assert.equal(await page.locator(".alpha-account-notice").evaluate(node => getComputedStyle(node).color), "rgb(180, 35, 24)");
  assert.match(await page.locator(".snapshot-notice").innerText(), /2026-10-09/);
  await nav("Coverage").click();
  await page.locator("[data-follow-company]").first().click();
  assert.ok(await page.getByRole("dialog").locator('[data-auth="login"]').isDisabled());
  assert.ok(await page.getByRole("dialog").locator('[data-auth="signup"]').isDisabled());
  assert.ok(await page.getByRole("dialog").locator(".alpha-account-notice").isVisible());
  await page.getByRole("dialog").getByRole("button", { name: "Continue browsing", exact: true }).click();
  await button("Clinical programs").click();
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 25);
  assert.match(await page.locator(".pagination-summary").innerText(), /of 623 clinical programs/);
  await page.getByRole("navigation", { name: "Clinical programs pagination" }).getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Clinical program 26", exact: true }).waitFor();
  await search.fill("Remote intervention");
  await page.getByRole("button", { name: "Clinical program 601", exact: true }).waitFor();
  assert.equal(await page.locator("tbody tr").count(), 1);
  await search.fill("");
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 25);
  await button("Companies").click();
  await button("Beta Device Fixture").click();
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 3);
  assert.match(await page.locator(".program-scope").innerText(), /3 clinical programs/);
  assert.ok(requests.some(r => r.path === "/api/studies" && r.params.company_id === "992" && !r.params.q));
  await nav("Change log").click();
  await page.waitForFunction(() => document.querySelectorAll(".change-card").length === 10);
  assert.match(await page.locator("#change-feed-panel .pagination-summary").innerText(), /of 33 changes/);
  await page.getByRole("navigation", { name: "Change log pagination" }).getByRole("button", { name: "Next" }).click();
  await page.getByRole("heading", { name: "Saved change 11", exact: true }).waitFor();
  await button("Enrollment").click();
  await page.getByRole("heading", { name: "Saved change 22", exact: true }).waitFor();
  assert.ok(requests.some(r => r.path === "/api/changes" && r.params.category === "enrollment" && r.params.offset === "0" && r.params.followed === "false"));
  await nav("Home").click();
  await page.getByRole("tab", { name: "News", exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll(".home-news-card").length === 10);
  assert.match(await page.locator(".home-news-card .badge").first().innerText(), /Healthcare/);
  await page.getByRole("navigation", { name: "News pagination" }).getByRole("button", { name: "Go to page 3" }).click();
  await page.getByRole("link", { name: "Healthcare headline 23", exact: true }).waitFor();
  assert.equal(await page.locator(".home-news-card").count(), 3);
  await search.fill("old");
  await oldStarted;
  await search.fill("new");
  await page.getByRole("link", { name: "Newest requested results", exact: true }).waitFor();
  releaseOld();
  await page.waitForTimeout(200);
  assert.equal(await page.getByText("Old response must not replace latest results").count(), 0);
  await search.fill("");
  await page.waitForFunction(() => document.querySelectorAll(".home-news-card").length === 10);
  await page.getByRole("tab", { name: "Target action dates", exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll(".home-tad-card").length === 10);
  assert.match(await page.locator(".pagination-summary").innerText(), /of 528 results/);
  await page.getByRole("tab", { name: "Calendar", exact: true }).click();
  await page.locator(".calendar-month-only .calendar-event").waitFor();
  assert.equal(await page.locator(".calendar-event").count(), 503, "Calendar must include results past both legacy100 and500 limits");
  assert.match(await page.locator(".calendar-count").innerText(), /502 exact dates · 1 month-only date/);
  assert.ok(requests.some(r => r.path === "/api/home/tads/page" && r.params.month === "2026-10" && r.params.offset === "500"));
  await page.locator('[data-calendar-tad="502"]').click();
  await page.getByRole("dialog").locator("summary").click();
  assert.match(await page.getByRole("dialog").innerText(), /Administrator corrected evidence/);
  assert.match(await page.getByRole("dialog").innerText(), /Original preserved evidence/);
  await button("Close dialog").click();
  await page.locator(".calendar-undated > summary").click();
  assert.equal(await page.locator(".calendar-undated .home-tad-card").count(), 10);
  await page.getByRole("navigation", { name: "Unresolved dates pagination" }).getByRole("button", { name: "Next" }).click();
  await page.waitForFunction(() => document.querySelector(".calendar-undated .ticker")?.textContent === "T514");
  assert.ok(await page.locator(".calendar-undated").evaluate(node => node.open));
  assert.equal(await page.locator(".calendar-event").count(), 503);
  await page.goto("http://trialdelta.test/#login", { waitUntil: "networkidle" });
  await page.locator("#login-form").waitFor();
  await page.locator("#login-email").fill("admin@example.org");
  await page.locator("#login-password").fill("Fixture administrator password 2026!");
  await page.locator("#login-form").getByRole("button", { name: "Sign in", exact: true }).click();
  await page.locator(".shell:not(.visitor-shell)").waitFor();
  assert.ok(await nav("Account").isVisible());
  assert.ok(await page.locator(".top-signout").isVisible());
  await nav("Change log").click();
  await button("Add evidence").click();
  await page.waitForFunction(() => document.querySelectorAll("#study option").length === 621);
  assert.ok(requests.some(r => r.path === "/api/studies" && r.params.company_id === "991" && r.params.offset === "500"));
  await page.locator("#company").selectOption("992");
  await page.waitForFunction(() => document.querySelectorAll("#study option").length === 4);
  await button("Close dialog").click();
  await nav("Coverage").click();
  await button("Follow a theme").click();
  await page.waitForFunction(() => document.querySelectorAll("#theme-options option").length === 2);
  await page.locator("#follow-value").fill("Remote");
  await page.waitForFunction(() => document.querySelectorAll("#theme-options option").length === 1);
  assert.ok(requests.some(r => r.path === "/api/themes" && r.params.q === "Remote" && r.params.limit === "50"));
  await button("Close dialog").click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".top-signout").click();
  await page.locator(".visitor-shell").waitFor();
  assert.ok(await page.locator('.visitor-actions [data-auth="signup"]').isVisible());
  assert.ok(await page.locator(".alpha-account-notice").isVisible());
  fs.mkdirSync(path.join(__dirname, "../artifacts"), { recursive: true });
  await page.screenshot({ path: path.join(__dirname, "../artifacts/portfolio-mobile.png"), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "Portfolio guest controls must fit mobile screens");
  await page.setViewportSize({ width: 1512, height: 1100 });
  await page.screenshot({ path: path.join(__dirname, "../artifacts/portfolio-desktop.png"), fullPage: true });
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  console.log(JSON.stringify({ result: "passed", checks: "guest alpha notice and snapshot, preserved administrator login/account/logout, lazy bounded study/change reads, dataset search and exact company counts, healthcare news paging and stale responses, full503-date calendar and paged unresolved evidence, complete administrator company selector and dataset themes", liveDatabaseReads: 0, liveDatabaseWrites: 0 }));
  await browser.close();
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exitCode = 1; });
