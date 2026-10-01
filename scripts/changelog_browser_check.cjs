// Change log regression checks. Every /api request is mocked or blocked; this
// script never reads or mutates the running app's database or invokes collectors.
// Run with APP_URL (default http://127.0.0.1:8765) and Playwright on NODE_PATH.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const company = { id: 992001, ticker: "ALFA", name: "Alpha Fixture Therapeutics", area: "Oncology", study_count: 2 };
const otherCompany = { id: 992002, ticker: "BETA", name: "Beta Fixture Bio", area: "Oncology", study_count: 2 };
const unrelatedCompany = { id: 992003, ticker: "GAMM", name: "Gamma Fixture Sciences", area: "Oncology", study_count: 1 };
const companies = [company, otherCompany, unrelatedCompany];
const trial = (id, owner, drug) => ({
  id, company_id: owner.id, company: owner.name, ticker: owner.ticker,
  title: `Study of ${drug} in solid tumors`, drug, phase: "PHASE2", status: "RECRUITING",
  conditions: ["Solid tumors"], targets: [], primary_completion: "2028-06", added_at: "2026-09-20T12:00:00+00:00",
});
const studies = [
  trial("NCT09920001", company, "AF-101"), trial("NCT09920002", company, "AF-202"),
  trial("NCT09920003", otherCompany, "BF-303"), trial("NCT09920004", otherCompany, "BF-404"),
  trial("NCT09920005", unrelatedCompany, "GF-505"),
];
const initialFollows = [
  { id: 992001, user_id: 992001, kind: "company", value: String(company.id) },
  { id: 992002, user_id: 992001, kind: "program", value: studies[2].id },
];
const change = (id, owner, study, category, title, day, extra = {}) => ({
  id, company_id: owner.id, company: owner.name, ticker: owner.ticker,
  study_id: study?.id || null, drug: study?.drug || "", category, title,
  before_value: "180 participants (estimated)", after_value: "240 participants (estimated)",
  source: "registry", review_status: "pending", publication_status: "automatic",
  detected_at: new Date(Date.UTC(2026, 8, day, 12)).toISOString(), ...extra,
});
const initialChanges = [
  change(992101, company, null, "timeline", "Alpha company readout guidance updated", 24, { source: "company", review_status: "approved", publication_status: "reviewed", before_value: "H1 2027", after_value: "H2 2027" }),
  change(992102, company, studies[1], "trial_design", "Alpha second trial design updated", 23, { before_value: "Two arms", after_value: "Three arms" }),
  change(992103, otherCompany, studies[2], "development_status", "Individually followed Beta trial now recruiting", 22, { before_value: "Not yet recruiting", after_value: "Recruiting" }),
  ...Array.from({ length: 14 }, (_, index) => change(992110 + index, company, studies[0], "enrollment", `Enrollment cohort update ${String(index + 1).padStart(2, "0")}`, 21 - index)),
  change(992130, company, studies[1], "registry_date", "Alpha second trial completion date updated", 7, { before_value: "2027-06", after_value: "2027-09" }),
  change(992131, company, studies[1], "development_status", "Alpha second trial recruitment completed", 6, { before_value: "Recruiting", after_value: "Active, not recruiting" }),
  change(992132, company, studies[0], "trial_design", "Alpha first trial outcome description updated", 5),
  change(992133, company, studies[1], "registry_date", "Alpha study completion becomes actual", 4),
  change(992134, otherCompany, studies[2], "registry_date", "Followed Beta trial primary completion updated", 3),
  change(992135, company, studies[0], "development_status", "Alpha first trial recruitment restarted", 2),
  change(992136, company, studies[1], "trial_design", "Alpha second trial phase updated", 1),
  // These records must be excluded from the followed view. Following one Beta
  // trial must not implicitly follow its other trials or company-level changes.
  change(992201, otherCompany, studies[3], "enrollment", "Unfollowed Beta sibling trial enrollment", 27),
  change(992202, otherCompany, null, "timeline", "Unfollowed Beta company guidance", 26, { source: "company", review_status: "approved", publication_status: "reviewed" }),
  change(992203, unrelatedCompany, studies[4], "trial_design", "Unrelated Gamma trial design", 25),
];
const chronological = rows => [...rows].sort((a, b) => b.detected_at.localeCompare(a.detected_at) || b.id - a.id);
const followsChange = (row, follows) => follows.some(f => f.kind === "company" ? String(row.company_id) === f.value : f.kind === "program" && row.study_id === f.value);
const pageSize = 10;
const screenshotDir = path.join(__dirname, "../artifacts");
let browser;

(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  const context = await browser.newContext({ viewport: { width: 1512, height: 1100 }, serviceWorkers: "block" });
  const page = await context.newPage();
  const errors = [];
  const unexpectedRequests = [];
  const operationalRequests = [];
  const changeRequests = [];
  let follows = structuredClone(initialFollows);
  let changes = structuredClone(initialChanges);
  page.on("pageerror", error => errors.push(error.message));
  await context.route("**/api/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const method = request.method();
    const respond = (json, status = 200) => route.fulfill({ status, json });
    if (/^\/api\/(overview|sync|jobs|review|inbox)(\/|$)/.test(pathname)) operationalRequests.push(`${method} ${pathname}`);
    if (method === "GET") {
      if (pathname === "/api/auth/config") return respond({ auth_required: true, registration_enabled: true, email_delivery_available: false, password_min_length: 14 });
      if (["/api/me", "/api/account"].includes(pathname)) return respond({ id: 992001, email: "changelog-fixture@example.org", display_name: "Change Log Researcher", is_admin: false, mode: "local", digest_enabled: false, auth_required: true, email_verified: false, email_delivery_available: false, created_at: "2026-09-24T00:00:00+00:00" });
      if (pathname === "/api/companies") return respond(companies.map(c => ({ ...c, followed: follows.some(f => f.kind === "company" && f.value === String(c.id)) })));
      if (["/api/studies", "/api/home/trials"].includes(pathname)) return respond(studies);
      if (pathname === "/api/home/news") return respond([]);
      if (pathname === "/api/follows") return respond(follows);
      if (pathname === "/api/digest") return respond({ day: "2026-09-24", changes: chronological(changes.filter(c => followsChange(c, follows))), follow_count: follows.length, note: "Changes from your watchlist." });
      if (pathname === "/api/changes") {
        changeRequests.push(url.search);
        const onlyFollowed = url.searchParams.get("followed") !== "false";
        let rows = changes.filter(c => !onlyFollowed || followsChange(c, follows));
        if (url.searchParams.get("category")) rows = rows.filter(c => c.category === url.searchParams.get("category"));
        if (url.searchParams.get("q")) rows = rows.filter(c => JSON.stringify(c).toLowerCase().includes(url.searchParams.get("q").toLowerCase()));
        return respond(chronological(rows));
      }
      const row = changes.find(c => pathname === `/api/changes/${c.id}`);
      if (row) return respond({
        ...row,
        evidence: ["Previous", "Current"].map((label, index) => ({
          id: row.id * 10 + index, label, source_type: "registry", title: "ClinicalTrials.gov snapshot",
          source_updated: index ? "2026-09-24" : "2026-09-20", captured_at: index ? "2026-09-24T12:00:00+00:00" : "2026-09-20T12:00:00+00:00",
          quote: JSON.stringify({ enrollment_count: index ? 240 : 180, enrollment_type: "ESTIMATED" }, null, 2),
          url: `https://clinicaltrials.gov/study/${row.study_id}`,
        })),
        reconciliation: { status: "current", message: "Registry records preserve the reported change.", note: "Primary completion and expected readout are separate milestones." },
      });
    }
    unexpectedRequests.push(`${method} ${pathname}`);
    return respond({ detail: "Unmocked change-log test request blocked." }, 500);
  });
  const button = name => page.getByRole("button", { name, exact: true });
  const heading = name => page.getByRole("heading", { name, exact: true });
  const cards = page.locator("#main .change-card").filter({ has: page.locator(".change-title") });
  const search = page.locator("#search");
  const pagination = page.getByRole("navigation", { name: "Change log pagination", exact: true });
  const followedRows = () => chronological(changes.filter(c => followsChange(c, follows)));
  const allRows = () => chronological(changes);
  const screenshot = async name => {
    await page.evaluate(() => window.scrollTo(0, 0));
    return page.screenshot({ path: path.join(screenshotDir, `${name}.png`), fullPage: true });
  };
  async function assertPage(rows, current = 1) {
    const expected = rows.slice((current - 1) * pageSize, current * pageSize).map(c => c.title);
    await page.waitForFunction(titles => JSON.stringify([...document.querySelectorAll("#main .change-title")].map(node => node.textContent)) === JSON.stringify(titles), expected);
    assert.equal(await cards.count(), expected.length, "Each page shows at most ten chronological change cards");
    if (rows.length > pageSize) {
      assert.equal(await pagination.locator('[aria-current="page"]').innerText(), String(current));
      assert.equal(await pagination.getByRole("button", { name: "Previous", exact: true }).isDisabled(), current === 1);
      assert.equal(await pagination.getByRole("button", { name: "Next", exact: true }).isDisabled(), current === Math.ceil(rows.length / pageSize));
      assert.equal(await pagination.evaluate(node => {
        const cards = [...document.querySelectorAll("#main .change-title")];
        return cards.every(card => Boolean(card.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING));
      }), true, "Pagination appears below the change cards");
    } else assert.equal(await pagination.count(), 0, "A single page needs no pagination");
  }
  async function gotoPage(number) {
    await pagination.getByRole("button", { name: `Go to page ${number}`, exact: true }).click();
  }
  async function selectCategory(category) {
    await page.locator(`[data-filter="${category}"]`).click();
    assert.equal(await page.locator("[data-filter].selected").count(), 1, "Exactly one category is selected");
    assert.equal(await page.locator("[data-filter].selected").getAttribute("data-filter"), category);
  }
  async function openLog() {
    await button("Change log").click();
    await heading("The change log.").waitFor();
  }
  async function reloadLog() {
    await page.reload({ waitUntil: "networkidle" });
    await openLog();
  }
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.goto(process.env.APP_URL || "http://127.0.0.1:8765", { waitUntil: "networkidle" });
  await openLog();
  assert.equal(await button("Following only").getAttribute("aria-pressed"), "true", "Change log defaults to the signed-in user's follows");
  await assertPage(followedRows());
  const seen = [];
  for (let current = 1; current <= Math.ceil(followedRows().length / pageSize); current++) {
    if (current > 1) await pagination.getByRole("button", { name: "Next", exact: true }).click();
    await assertPage(followedRows(), current);
    seen.push(...await page.locator("#main .change-title").allTextContents());
  }
  assert.deepEqual(seen, followedRows().map(c => c.title), "Company-level, company trials, and individually followed trials appear exactly once, newest first");
  assert.ok(seen.includes("Alpha company readout guidance updated"));
  assert.ok(seen.includes("Alpha second trial design updated"));
  assert.ok(seen.includes("Individually followed Beta trial now recruiting"));
  assert.ok(!seen.some(title => /Unfollowed|Unrelated/.test(title)), "A trial follow must not expand to its whole company");
  await gotoPage(1);
  await assertPage(followedRows());

  // The automatic registry card has the saved comparison and source evidence.
  const automatic = cards.filter({ hasText: "Alpha second trial design updated" });
  assert.ok(!(await automatic.innerText()).includes("Reviewed"), "Automatic changes must not be falsely labeled reviewed");
  assert.equal(await automatic.locator(".comparison strong").first().innerText(), "Two arms");
  assert.equal(await automatic.locator(".comparison strong").last().innerText(), "Three arms");
  await automatic.getByRole("button", { name: /View evidence/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.waitFor();
  await dialog.getByRole("heading", { name: "Alpha second trial design updated", exact: true }).first().waitFor();
  assert.equal(await dialog.locator(".evidence").count(), 2);
  assert.match(await dialog.innerText(), /enrollment_count/);
  const sourceLinks = dialog.getByRole("link", { name: /Open original source/ });
  assert.equal(await sourceLinks.count(), 2);
  assert.equal(await sourceLinks.first().getAttribute("href"), `https://clinicaltrials.gov/study/${studies[1].id}`);
  assert.equal(await sourceLinks.first().getAttribute("target"), "_blank");
  await button("Close dialog").click();
  await screenshot("changelog-desktop");

  // Filters act on the complete collection before it is paginated.
  for (const category of ["timeline", "development_status", "enrollment", "trial_design", "registry_date"]) {
    await selectCategory(category);
    await assertPage(followedRows().filter(c => c.category === category));
  }
  await selectCategory("enrollment");
  await gotoPage(2);
  await assertPage(followedRows().filter(c => c.category === "enrollment"), 2);
  await search.fill("cohort update 01");
  await assertPage(followedRows().filter(c => c.category === "enrollment" && c.title.includes("cohort update 01")));
  await search.fill("no-matching-change-sentinel");
  await heading("No matching changes").waitFor();
  await assertPage([]);
  await search.fill("");
  await assertPage(followedRows().filter(c => c.category === "enrollment"));
  await selectCategory("");
  await gotoPage(3);
  await selectCategory("development_status");
  await assertPage(followedRows().filter(c => c.category === "development_status"));

  // The existing following toggle exposes all stored changes when requested.
  await selectCategory("");
  await gotoPage(3);
  await button("Following only").click();
  assert.equal(await button("Following only").getAttribute("aria-pressed"), "false");
  await assertPage(allRows());
  assert.ok((await page.locator("#main .change-title").allTextContents()).some(title => title.includes("Unrelated Gamma")));
  await selectCategory("enrollment");
  await assertPage(allRows().filter(c => c.category === "enrollment"));
  await search.fill("Unfollowed Beta");
  await assertPage(allRows().filter(c => c.category === "enrollment" && c.title.includes("Unfollowed Beta")));
  await search.fill("");
  await selectCategory("");
  await gotoPage(2);
  await button("Following only").click();
  await assertPage(followedRows());

  // Re-entering the log reads newly saved changes without running a collector.
  await button("Home").click();
  const requestsBeforeEntry = changeRequests.length;
  changes.push(change(992301, company, studies[1], "enrollment", "Freshly captured Alpha enrollment update", 28));
  await openLog();
  await assertPage(followedRows());
  assert.ok(changeRequests.length > requestsBeforeEntry, "Entering the log must fetch current saved changes");
  assert.equal(await cards.first().locator(".change-title").innerText(), "Freshly captured Alpha enrollment update");

  const overflows = [];
  for (const width of [360, 390, 768, 1024, 1512]) {
    await page.setViewportSize({ width, height: 900 });
    await assertPage(followedRows());
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) overflows.push(width);
    if (width === 390) await screenshot("changelog-mobile");
  }
  assert.deepEqual(overflows, [], "Change log and pagination must fit mobile, tablet, and desktop");

  // Distinct empty states should help someone either follow companies or wait
  // for a new snapshot, rather than imply that automatic changes await review.
  follows = [];
  await reloadLog();
  await assertPage([]);
  await heading("Follow companies or trials to start.").waitFor();
  const noFollowsCopy = await page.locator("#main .empty").first().innerText();
  assert.match(noFollowsCopy, /follow/i, "No-follow state must explain how to populate the log");
  assert.match(noFollowsCopy, /compan|trial|program/i);
  assert.ok(!/^No matching changes\b/.test(noFollowsCopy), "No follows differs from a filter with no matches");
  await screenshot("changelog-no-follows");
  await button("Following only").click();
  await assertPage(allRows());
  await button("Following only").click();
  await assertPage([]);

  // Fictional examples remain independent of the live watchlist.
  await button("Explore demo").click();
  await heading("Readout moves to the second half of 2026").waitFor();
  assert.equal(await cards.count(), 3);
  assert.match(await page.locator(".demo-banner").innerText(), /fictional/i);
  await selectCategory("enrollment");
  assert.equal(await cards.count(), 1);
  await selectCategory("trial_design");
  await heading("No matching changes").waitFor();
  await assertPage([]);
  await selectCategory("");
  await cards.first().getByRole("button", { name: /View evidence/ }).click();
  await dialog.waitFor();
  assert.match(await dialog.innerText(), /Fictional example/);
  await button("Close dialog").click();
  await button("Live workspace").click();
  await assertPage([]);
  follows = structuredClone(initialFollows);
  changes = [];
  await reloadLog();
  await assertPage([]);
  await heading("No changes in your watchlist yet.").waitFor();
  const noChangesCopy = await page.locator("#main .empty").first().innerText();
  assert.notEqual(noChangesCopy, noFollowsCopy, "A populated watchlist with no changes needs a different message");
  assert.ok(!/after review|await.*review|approved|publish/i.test(noChangesCopy), "Automatic history must not imply a review gate");
  await screenshot("changelog-no-changes");

  assert.deepEqual(errors, [], "Browser runtime errors");
  assert.deepEqual(unexpectedRequests, [], "All API requests must remain mocked; no writes may reach the server");
  assert.deepEqual(operationalRequests, [], "The reader Change log must not invoke collection or review operations");
  assert.ok(changeRequests.some(query => new URLSearchParams(query).get("followed") === "false"), "All-scope selection requests all saved changes");
  console.log(JSON.stringify({ result: "passed", changeLogChecks: "follow defaults, company and trial scope, company-level changes, chronological pagination, category and search filters, all scope, automatic evidence, refreshed entry, empty states, fictional demo, responsive layout", pageSize, responsiveWidths: [360, 390, 768, 1024, 1512], browserErrors: errors, operationalRequests, liveDatabaseReads: 0, liveDatabaseWrites: 0 }));
})().catch(async error => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
});


