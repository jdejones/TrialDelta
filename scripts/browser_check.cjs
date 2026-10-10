const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const company = { id: 991001, ticker: "SMBK", name: "Smoke Biotech", area: "Oncology", study_count: 23 };
const trials = [
  { id: "NCT09910002", title: "Latest trial of SB-202 for advanced solid tumors", drug: "SB-202", phase: "PHASE2", status: "RECRUITING", conditions: ["Solid tumors"], primary_completion: "2028-06", added_at: "2026-09-24T12:00:00+00:00" },
  { id: "NCT09910001", title: "Earlier trial of SB-101 for melanoma", drug: "SB-101", phase: "PHASE1", status: "NOT_YET_RECRUITING", conditions: ["Melanoma"], primary_completion: "2027-12", added_at: "2026-09-23T09:00:00+00:00" },
].map((study) => ({ ...study, company_id: company.id, company: company.name, ticker: company.ticker, targets: [] }));
const news = [
  { id: "news-new", title: "Smoke Biotech announces its latest clinical update", url: "https://example.org/biotech/latest", published_at: "2026-09-24T14:00:00+00:00" },
  { id: "news-old", title: "Earlier biotechnology research announcement", url: "javascript:alert('unsafe source')", published_at: "2026-09-23T14:00:00+00:00" },
].map((item) => ({ ...item, company: company.name, ticker: company.ticker, source: "Biotech Research Wire" }));
// Include three pages, a multi-page search subset, and a unique single result.
trials.push(...Array.from({ length: 21 }, (_, index) => ({
  ...trials[1],
  id: "NCT" + String(9910000 - index).padStart(8, "0"),
  title: (index < 14 ? "Pagination cohort" : index === 20 ? "Single result sentinel" : "Additional") + " trial " + (index + 3),
  added_at: new Date(Date.UTC(2026, 8, 22 - index, 9)).toISOString(),
})));
news.push(...Array.from({ length: 21 }, (_, index) => ({
  ...news[0],
  id: "news-extra-" + index,
  title: (index < 14 ? "Pagination cohort" : index === 20 ? "Single result sentinel" : "Additional") + " news " + (index + 3),
  url: "https://example.org/biotech/article-" + index,
  published_at: new Date(Date.UTC(2026, 8, 22 - index, 14)).toISOString(),
})));
let browser;
(async () => {
  browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "msedge" });
  const page = await browser.newPage({ viewport: { width: 1512, height: 1100 } });
  const errors = [];
  const operationalRequests = [];
  const unexpectedRequests = [];
  const homeRequests = [];
  const fixtureOnly = process.env.FIXTURE_ONLY === "1";
  let fixtureMode = fixtureOnly;
  let readerMode = false;
  let follows = [];
  let submittedEvidence;
  const feedModes = { trials: "populated", news: "populated" };
  let releaseFeed;
  let feedGate;
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (request) => {
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (/^\/api\/(overview|sync|jobs|review|inbox)(\/|$)/.test(pathname)) operationalRequests.push(pathname);
  });
  // Check the live landing API first, then use deterministic API fixtures.
  // No mutation is ever allowed to reach the running app or its database.
  if (fixtureOnly) await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://trialdelta.test") return route.abort();
    const files = { "/": ["index.html", "text/html"], "/static/app.js": ["app.js", "text/javascript"], "/static/styles.css": ["styles.css", "text/css"], "/static/base.css": ["base.css", "text/css"], "/static/favicon.svg": ["favicon.svg", "image/svg+xml"] };
    if (!files[url.pathname]) return route.fallback();
    const [file, contentType] = files[url.pathname];
    return route.fulfill({ status: 200, contentType, body: fs.readFileSync(path.join(__dirname, "../trialdelta/static", file)) });
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const method = request.method();
    const respond = (json, status = 200) => route.fulfill({ status, json });
    if (!fixtureMode && method === "GET") return route.continue();
    if (fixtureMode) {
      if (method === "GET" && pathname === "/api/home/tads") return respond([]);
      if (method === "GET" && pathname === "/api/home/tads/page") return respond({ items: [], total: 0, identified: 0, month: 0, unresolved: 0, limit: Number(url.searchParams.get("limit")), offset: Number(url.searchParams.get("offset")) });
      if (method === "GET" && pathname === "/api/home/filings") return respond({ items: [], page: 1, page_size: 10, total: 0, total_pages: 1 });
      if (method === "GET" && /^\/api\/home\/(trials|news\/page)$/.test(pathname)) {
        const tab = pathname === "/api/home/trials" ? "trials" : "news";
        homeRequests.push(tab);
        const mode = feedModes[tab];
        if (mode === "delayed") await feedGate;
        if (mode === "error") return respond({ detail: "The feed is temporarily unavailable." }, 503);
        if (tab === "trials") return respond(mode === "empty" ? [] : trials);
        const q = (url.searchParams.get("q") || "").toLowerCase(), size = Number(url.searchParams.get("page_size"));
        assert.equal(size, 10, "News requests must use bounded pages");
        const rows = mode === "empty" ? [] : news.filter(row => !q || JSON.stringify(row).toLowerCase().includes(q));
        const totalPages = Math.max(1, Math.ceil(rows.length / size)), current = Math.min(Number(url.searchParams.get("page")), totalPages);
        return respond({ items: rows.slice((current - 1) * size, current * size), page: current, page_size: size, total: rows.length, total_pages: totalPages });
      }
      if (method === "GET" && pathname === "/api/auth/config")
        return respond({ auth_required: false, public_browsing_enabled: false, registration_enabled: true, email_delivery_available: false, password_min_length: 14 });
      if (method === "GET" && ["/api/me", "/api/account"].includes(pathname))
        return respond({ id: 991001, email: "smoke@example.org", display_name: "Smoke Researcher", is_admin: !readerMode, mode: "local", digest_enabled: true, auth_required: false, email_verified: false, email_delivery_available: false, created_at: "2026-09-24T00:00:00+00:00" });
      if (method === "GET" && pathname === "/api/companies")
        return respond([{ ...company, followed: follows.some((f) => f.kind === "company" && f.value === String(company.id)) }]);
      if (method === "GET" && pathname === "/api/studies") {
        const q = (url.searchParams.get("q") || "").toLowerCase(), companyId = Number(url.searchParams.get("company_id"));
        const rows = trials.filter(row => (!companyId || row.company_id === companyId) && (!q || JSON.stringify(row).toLowerCase().includes(q)));
        const limit = Number(url.searchParams.get("limit") || 100), offset = Number(url.searchParams.get("offset") || 0);
        return route.fulfill({ status: 200, json: rows.slice(offset, offset + limit), headers: { "X-Total-Count": String(rows.length) } });
      }
      if (method === "GET" && pathname === "/api/changes") return respond([]);
      if (method === "GET" && pathname === "/api/follows") return respond(follows);
      if (method === "GET" && pathname === "/api/digest")
        return respond({ day: "2026-09-24", changes: [], follow_count: follows.length, note: "Reviewed updates from your watchlist." });
      const study = trials.find((s) => pathname === "/api/studies/" + s.id);
      if (method === "GET" && study)
        return respond({ study: { ...study, targets: "[]" }, reconciliation: { status: "current", note: "Timing is uncertain." }, snapshots: [], guidance: [], changes: [] });
      if (method === "POST" && pathname === "/api/follows") {
        const follow = { ...request.postDataJSON(), id: 991001, user_id: 991001 };
        follows.push(follow);
        return respond(follow);
      }
      if (method === "DELETE" && /^\/api\/follows\/\d+$/.test(pathname)) {
        follows = follows.filter((f) => String(f.id) !== pathname.split("/").pop());
        return respond({ ok: true });
      }
      if (method === "POST" && pathname === "/api/evidence") {
        submittedEvidence = request.postDataJSON();
        return respond({ ...submittedEvidence, id: 999999, review_status: "pending" });
      }
    }
    unexpectedRequests.push(method + " " + pathname);
    return respond({ detail: "Unmocked smoke-test request blocked." }, 500);
  });
  const tab = (name) => page.getByRole("tab", { name, exact: true });
  const button = (name) => page.getByRole("button", { name, exact: true });
  const heading = (name) => page.getByRole("heading", { name, exact: true });
  const pagination = (name) => page.getByRole("navigation", { name: name + " pagination", exact: true });
  const search = page.getByRole("textbox", { name: "Search companies, programs, news, or changes", exact: true });
  const screenshot = (name) => page.screenshot({ path: path.join(__dirname, "../artifacts/" + name + ".png"), fullPage: true });
  async function assertReaderInterface() {
    assert.equal(await page.locator(".stats").count(), 0, "Database metrics must not appear");
    assert.equal(await page.getByRole("button", { name: /Refresh sources|Source inbox|Review queue|Import source candidates/ }).count(), 0, "Backend operations must not appear");
    assert.equal(await page.getByText(/Companies in coverage|Recent collection runs|Collection schedule/).count(), 0, "Operational status must stay out of the web app");
    // Public company rows may show their saved trial count. Global database
    // metrics are still excluded by the .stats check above.
    const trialLabels = page.getByText("Trials captured", { exact: true });
    assert.equal(await trialLabels.count(), await page.getByRole("columnheader", { name: "Trials captured", exact: true }).count(), "Trial counts belong only in the company table");
  }
  async function assertTrialsSelected() {
    assert.equal(await tab("Trials").getAttribute("aria-selected"), "true");
    assert.equal(await tab("News").getAttribute("aria-selected"), "false");
    assert.equal(await tab("Target action dates").getAttribute("aria-selected"), "false");
    assert.equal(await tab("Calendar").getAttribute("aria-selected"), "false");
  }
  async function assertFeedPage(name, currentPage, rows = name === "Trials" ? trials : news) {
    const selector = name === "Trials" ? ".home-trial-card h3" : ".home-news-card h3";
    const expected = rows.slice((currentPage - 1) * 10, currentPage * 10).map((row) => row.title);
    await page.waitForFunction(({ selector, expected }) =>
      JSON.stringify([...document.querySelectorAll(selector)].map((node) => node.textContent)) === JSON.stringify(expected),
      { selector, expected },
    );
    if (rows.length > 10) {
      assert.equal(await pagination(name).locator('[aria-current="page"]').innerText(), String(currentPage));
      assert.equal(await pagination(name).getByRole("button", { name: "Previous", exact: true }).isDisabled(), currentPage === 1);
      assert.equal(await pagination(name).getByRole("button", { name: "Next", exact: true }).isDisabled(), currentPage === Math.ceil(rows.length / 10));
    } else {
      assert.equal(await pagination(name).count(), 0, "Single-page and empty feeds need no pagination controls");
    }
  }
  if (!fixtureOnly) {
  const liveIdentity = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/me");
  await page.goto(process.env.APP_URL || "http://127.0.0.1:8765", { waitUntil: "networkidle" });
  const identity = await liveIdentity;
  fs.mkdirSync(path.join(__dirname, "../artifacts"), { recursive: true });
  const realTrials = await page.request.get(new URL("/api/home/trials", page.url()).href);
  if (identity.status() === 401 && realTrials.status() === 401) {
    await page.locator("#login-form").waitFor();
    await screenshot("home-live-sign-in");
  } else {
    assert.ok([200, 401].includes(identity.status()), "The current identity must be signed in or anonymous");
    assert.equal(realTrials.status(), 200, "The public or signed-in landing feed must be available");
    assert.ok(Array.isArray(await realTrials.json()), "The trials API must return an array");
    if (identity.status() === 401) {
      const config = await (await page.request.get(new URL("/api/auth/config", page.url()).href)).json();
      assert.equal(config.public_browsing_enabled, true, "Anonymous research access must be explicitly configured");
      await page.locator(".visitor-shell").waitFor();
    }
    await heading("Home.").waitFor();
    assert.equal(await button("Home").getAttribute("aria-current"), "page");
    await assertTrialsSelected();
    assert.equal(await tab("8-Ks").isDisabled(), false);
    await heading("Companies to explore").waitFor();
    await assertReaderInterface();
    await screenshot("home-live-desktop");
  }
  }

  fixtureMode = true;
  fs.mkdirSync(path.join(__dirname, "../artifacts"), { recursive: true });
  if (fixtureOnly) await page.goto("http://trialdelta.test", { waitUntil: "networkidle" });
  else await page.reload({ waitUntil: "networkidle" });
  await page.locator(".home-trial-card").first().waitFor();
  assert.deepEqual(homeRequests, ["trials"], "News must load only when selected");
  assert.deepEqual(await page.locator(".home-trial-card h3").allTextContents(), trials.slice(0, 10).map((s) => s.title));
  assert.deepEqual(await page.locator(".home-trial-card time").evaluateAll((items) => items.map((item) => item.dateTime)), trials.slice(0, 10).map((s) => s.added_at));
  await page.locator(".home-trial-card").first().getByRole("button", { name: "Open trial" }).click();
  await page.getByRole("dialog").getByRole("heading", { name: trials[0].id, exact: true }).waitFor();
  await button("Close dialog").click();
  await tab("Trials").focus();
  await page.keyboard.press("ArrowRight");
  await page.locator(".home-news-card").first().waitFor();
  assert.equal(await tab("News").getAttribute("aria-selected"), "true");
  assert.equal(await tab("News").evaluate((node) => node === document.activeElement), true);
  assert.deepEqual(await page.locator(".home-news-card h3").allTextContents(), news.slice(0, 10).map((n) => n.title));
  assert.deepEqual(await page.locator(".home-news-card time").evaluateAll((items) => items.map((item) => item.dateTime)), news.slice(0, 10).map((n) => n.published_at));
  const articleLink = page.locator(".home-news-card").first().getByRole("link", { name: "Read article" });
  assert.equal(await articleLink.getAttribute("href"), news[0].url);
  assert.equal(await articleLink.getAttribute("target"), "_blank");
  assert.match(await articleLink.getAttribute("rel"), /noopener/);
  assert.match(await articleLink.getAttribute("rel"), /noreferrer/);
  assert.equal(await page.locator(".home-news-card").nth(1).getByRole("link").count(), 0, "Unsafe source URLs must not be clickable");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("Target action dates").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowRight");
  await assertTrialsSelected();
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("Calendar").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("8-Ks").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("Target action dates").getAttribute("aria-selected"), "true");
  await page.keyboard.press("ArrowLeft");
  assert.equal(await tab("News").getAttribute("aria-selected"), "true");
  // Both feeds paginate the filtered collection and remember their own page.
  for (const name of ["Trials", "News"]) {
    await tab(name).click();
    await assertFeedPage(name, 1);
    await pagination(name).getByRole("button", { name: "Next", exact: true }).click();
    await assertFeedPage(name, 2);
    assert.equal(await page.getByRole("tabpanel").evaluate((node) => node === document.activeElement), true, "Page changes focus the feed");
    await pagination(name).getByRole("button", { name: "Go to page 3", exact: true }).click();
    await assertFeedPage(name, 3);
    await pagination(name).getByRole("button", { name: "Previous", exact: true }).click();
    await assertFeedPage(name, 2);
    await pagination(name).getByRole("button", { name: "Go to page 1", exact: true }).click();
    await assertFeedPage(name, 1);
  }
  await tab("Trials").click();
  await pagination("Trials").getByRole("button", { name: "Go to page 2", exact: true }).click();
  await tab("News").click();
  await pagination("News").getByRole("button", { name: "Go to page 3", exact: true }).click();
  await tab("Trials").click();
  await assertFeedPage("Trials", 2);
  await tab("News").click();
  await assertFeedPage("News", 3);
  assert.equal(homeRequests.filter(tab => tab === "trials").length, 1, "The recent trial collection stays cached");
  assert.ok(homeRequests.filter(tab => tab === "news").length > 1, "News pages must be requested from the server");

  await search.fill("Pagination cohort");
  for (const name of ["News", "Trials"]) {
    await tab(name).click();
    const filtered = (name === "Trials" ? trials : news).filter((row) => row.title.includes("Pagination cohort"));
    await assertFeedPage(name, 1, filtered);
    await pagination(name).getByRole("button", { name: "Next", exact: true }).click();
    await assertFeedPage(name, 2, filtered);
  }
  await search.fill("Single result sentinel");
  for (const name of ["Trials", "News"]) {
    await tab(name).click();
    const filtered = (name === "Trials" ? trials : news).filter((row) => row.title.includes("Single result sentinel"));
    await assertFeedPage(name, 1, filtered);
  }
  await search.fill("unmatched-pagination-query");
  for (const name of ["News", "Trials"]) {
    await tab(name).click();
    await heading("No matching " + name.toLowerCase()).waitFor();
    await assertFeedPage(name, 1, []);
  }
  await search.fill("");
  for (const name of ["Trials", "News"]) {
    await tab(name).click();
    await assertFeedPage(name, 1);
  }

  for (const name of ["Go to Home", "TrialDelta home", "Home"]) {
    for (const feed of ["Trials", "News"]) {
      await tab(feed).click();
      await pagination(feed).getByRole("button", { name: "Go to page 2", exact: true }).click();
    }
    await button(name).click();
    await assertTrialsSelected();
    await assertFeedPage("Trials", 1);
    await tab("News").click();
    await assertFeedPage("News", 1);
  }
  await tab("Trials").click();
  await heading("Dates mean different things.").waitFor();
  await screenshot("home-desktop");

  // Loading, empty, and retry states are reproducible without database data.
  for (const feed of ["trials", "news"]) {
    const card = page.locator(feed === "trials" ? ".home-trial-card" : ".home-news-card");
    feedModes[feed] = "delayed";
    feedGate = new Promise((resolve) => { releaseFeed = resolve; });
    await page.reload({ waitUntil: "domcontentloaded" });
    await heading("Home.").waitFor();
    if (feed === "news") await tab("News").click();
    await page.getByRole("tabpanel").getByRole("status").waitFor();
    releaseFeed();
    await card.first().waitFor();
    feedModes[feed] = "empty";
    await page.reload({ waitUntil: "networkidle" });
    if (feed === "news") await tab("News").click();
    await heading(feed === "trials" ? "New trials will appear here." : "No healthcare news yet.").waitFor();
    await assertFeedPage(feed === "trials" ? "Trials" : "News", 1, []);
    feedModes[feed] = "error";
    await page.reload({ waitUntil: "networkidle" });
    if (feed === "news") await tab("News").click();
    await heading("Unable to load " + feed).waitFor();
    feedModes[feed] = "populated";
    await button("Try again").click();
    await card.first().waitFor();
  }

  await button("Change log").click();
  await heading("The change log.").waitFor();
  await button("Add evidence").waitFor();
  await screenshot("desktop");
  await button("Explore demo").click();
  await heading("Readout moves to the second half of 2026").waitFor();
  await page.getByRole("button", { name: "View evidence" }).first().click();
  await page.getByRole("dialog").waitFor();
  await button("Close dialog").click();
  await button("Coverage").click();
  await heading("Your research universe.").waitFor();
  await assertReaderInterface();
  const follow = page.locator("[data-follow-company]").first();
  const companyId = await follow.getAttribute("data-follow-company");
  const wasFollowed = (await follow.innerText()) === "Following ✓";
  const companyFollow = page.locator('[data-follow-company="' + companyId + '"]');
  await follow.click();
  await companyFollow.filter({ hasText: wasFollowed ? "+ Follow" : "Following ✓" }).waitFor();
  // Writes and the refreshed watchlist exist only in the route fixtures.
  await companyFollow.click();
  await companyFollow.filter({ hasText: wasFollowed ? "Following ✓" : "+ Follow" }).waitFor();
  assert.deepEqual(follows, [], "The fixture watchlist must return to its initial state");
  await button("Daily digest").click();
  await heading("Your daily brief.").waitFor();
  await assertReaderInterface();
  await button("Settings").click();
  await heading("Workspace settings.").waitFor();
  await assertReaderInterface();
  await button("Change log").click();

  // Exercise the same user interface with a regular reader's role.
  readerMode = true;
  await page.reload({ waitUntil: "networkidle" });
  // Account routing now preserves the current research page across reloads.
  await heading("The change log.").waitFor();
  assert.equal(new URL(page.url()).hash, "#log");
  await assertReaderInterface();
  await button("Add evidence").click();
  await page.getByLabel("Original source URL").waitFor();
  await page.getByLabel("Source publication date").fill("2026-01-01");
  await page.getByLabel("Source title and page reference").fill("Smoke test source");
  await page.getByLabel("Original source URL").fill("https://example.org/research");
  await page.getByLabel("Exact supporting excerpt").fill("Results are expected in the second half of 2026.");
  await page.getByLabel("Supported value / timing window").fill("H2 2026");
  await button("Submit for review").click();
  await page.getByText("Evidence submitted for review", { exact: true }).waitFor();
  assert.equal(submittedEvidence.value, "H2 2026");
  assert.equal(await page.getByRole("dialog").count(), 0);
  await heading("The change log.").waitFor();
  await assertReaderInterface();
  await button("Settings").click();
  await heading("Workspace settings.").waitFor();
  assert.equal(await heading("Invite a researcher").count(), 0);
  await button("Change log").click();
  await page.locator("#toast").waitFor({ state: "hidden" });
  await page.setViewportSize({ width: 390, height: 844 });
  await screenshot("mobile");
  const overflows = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  if (overflows) errors.push("Mobile document has horizontal overflow");
  await button("Home").click();
  await page.locator(".home-trial-card").first().waitFor();
  await screenshot("home-mobile");
  const homeOverflows = [];
  for (const width of [390, 768, 1024]) {
    await page.setViewportSize({ width, height: 844 });
    for (const name of ["Trials", "News"]) {
      await tab(name).click();
      for (const currentPage of [1, 3]) {
        await pagination(name).getByRole("button", { name: "Go to page " + currentPage, exact: true }).click();
        await assertFeedPage(name, currentPage);
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
        if (overflow) homeOverflows.push(name + " page " + currentPage + " at " + width + "px");
      }
    }
  }
  assert.deepEqual(homeOverflows, [], "Home feeds must fit mobile and tablet viewports");
  assert.deepEqual(unexpectedRequests, [], "Smoke checks must never send unmocked writes or depend on live fixture data");
  assert.deepEqual(operationalRequests, [], "Web must never request backend operations");
  console.log(JSON.stringify({
    browserErrors: errors,
    mobileOverflow: overflows,
    operationalRequests,
    homeFeedChecks: "landing, order, trial detail, lazy news, safe links, tabs, pagination, search, page memory, navigation, loading, empty, retry, responsive",
    homeOverflows,
    liveWrites: 0,
    manualEvidence: "submitted for review",
    url: page.url(),
  }));
  await browser.close();
  if (errors.length) process.exitCode = 1;
})().catch(async (e) => {
  console.error(e);
  if (browser) await browser.close();
  process.exitCode = 1;
});
