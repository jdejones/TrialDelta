// Healthcare scope regression. All application assets are local and every API
// request is mocked or blocked. No server, network data, or database is used.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const companies = [
  { id: 993001, ticker: "CARD", name: "Cardiac Devices Fixture", area: "Medical Devices", study_count: 1 },
  { id: 993002, ticker: "META", name: "Metabolic Research Fixture", area: "Oncology", study_count: 1 },
  { id: 993003, ticker: "ONCO", name: "Oncology Research Fixture", area: "Oncology", study_count: 1 },
];
const studies = [
  { id: "NCT09930001", company_id: companies[0].id, company: companies[0].name, ticker: "CARD", title: "Device study in heart failure", drug: "Cardiac monitoring device", phase: "NA", conditions: ["Heart failure"] },
  { id: "NCT09930002", company_id: companies[1].id, company: companies[1].name, ticker: "META", title: "Observational study in type 2 diabetes", drug: "", phase: "", conditions: ["Type 2 diabetes"] },
  { id: "NCT09930003", company_id: companies[2].id, company: companies[2].name, ticker: "ONCO", title: "Study in melanoma", drug: "ONC-101", phase: "PHASE2", conditions: ["Melanoma"] },
].map(s => ({ ...s, status: "RECRUITING", targets: [], primary_completion: "2028-06", added_at: "2026-09-26T12:00:00+00:00" }));

let browser;
(async () => {
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  const context = await browser.newContext({ viewport: { width: 1512, height: 1000 }, serviceWorkers: "block" });
  const page = await context.newPage();
  const errors = [], unexpected = [], follows = [];
  page.on("pageerror", error => errors.push(error.message));
  await context.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url()), pathname = url.pathname;
    const respond = json => route.fulfill({ status: 200, json });
    if (url.origin !== "http://trialdelta.test") {
      unexpected.push(req.url());
      return route.abort();
    }
    const staticFiles = {
      "/": ["index.html", "text/html"],
      "/static/app.js": ["app.js", "text/javascript"],
      "/static/styles.css": ["styles.css", "text/css"],
      "/static/base.css": ["base.css", "text/css"],
      "/static/favicon.svg": ["favicon.svg", "image/svg+xml"],
    };
    if (staticFiles[pathname]) {
      const [file, contentType] = staticFiles[pathname];
      return route.fulfill({ status: 200, contentType, body: fs.readFileSync(path.join(__dirname, "../trialdelta/static", file)) });
    }
    if (req.method() === "GET") {
      if (pathname === "/api/auth/config") return respond({ auth_required: false, public_browsing_enabled: false, registration_enabled: false });
      if (pathname === "/api/me") return respond({ id: 993001, display_name: "Healthcare Reader", email: "healthcare@example.org", is_admin: false, mode: "local", digest_enabled: false });
      if (pathname === "/api/companies") return respond(companies.map(c => ({ ...c, followed: false })));
      if (["/api/studies", "/api/home/trials"].includes(pathname)) return respond(studies);
      if (["/api/changes", "/api/home/news"].includes(pathname)) return respond([]);
      if (pathname === "/api/follows") return respond(follows);
      const study = studies.find(s => pathname === "/api/studies/" + s.id);
      if (study) return respond({ study: { ...study, targets: "[]" }, reconciliation: { status: "current", note: "Timing is uncertain." }, snapshots: [], guidance: [], changes: [] });
    }
    if (req.method() === "POST" && pathname === "/api/follows") {
      const row = { id: follows.length + 1, ...req.postDataJSON() };
      follows.push(row);
      return respond(row);
    }
    unexpected.push(`${req.method()} ${pathname}`);
    return route.fulfill({ status: 500, json: { detail: "Unexpected healthcare test request blocked." } });
  });
  const button = name => page.getByRole("button", { name, exact: true });
  const search = page.locator("#search");
  await page.goto("http://trialdelta.test", { waitUntil: "networkidle" });
  assert.equal(await page.locator(".home-trial-card").count(), 3);
  assert.equal(await page.locator(".workspace span").innerText(), "Research workspace");
  assert.equal(await page.locator(".page-heading .eyebrow").textContent(), "Clinical research workspace");
  assert.match(await page.locator('meta[name="description"]').getAttribute("content"), /healthcare companies/);
  const deviceCard = page.locator(".home-trial-card").filter({ hasText: "Device study in heart failure" });
  assert.match(await deviceCard.innerText(), /Not applicable/);
  assert.match(await page.locator(".home-trial-card").filter({ hasText: "Observational study" }).innerText(), /Phase not reported/);
  assert.match(await page.locator(".rail .coverage-row").filter({ hasText: "Metabolic Research Fixture" }).innerText(), /1 trial/);
  assert.match(await page.locator(".rail .coverage-row").filter({ hasText: "Oncology Research Fixture" }).innerText(), /ONCO · 1 trial/);

  await search.fill("type 2 diabetes");
  await page.waitForFunction(() => document.querySelectorAll(".home-trial-card").length === 1);
  assert.match(await page.locator(".home-trial-card").innerText(), /Observational study/);
  await search.fill("");
  await page.waitForFunction(() => document.querySelectorAll(".home-trial-card").length === 3);
  await deviceCard.locator(".feed-title").click();
  assert.match(await page.getByRole("dialog").innerText(), /Not applicable/);
  await button("Close dialog").click();

  await button("Coverage").click();
  await page.getByRole("heading", { name: "Your research universe.", exact: true }).waitFor();
  assert.equal(await page.getByRole("columnheader", { name: "Trials captured", exact: true }).count(), 1);
  const metabolicCompany = page.locator("tbody tr").filter({ hasText: "Metabolic Research Fixture" });
  assert.equal(await metabolicCompany.locator("td").nth(1).innerText(), "1");
  assert.doesNotMatch(await metabolicCompany.innerText(), /Oncology/);
  await search.fill("oncology");
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 1);
  assert.match(await page.locator("tbody").innerText(), /Oncology Research Fixture/);
  await search.fill("");
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 3);
  await button("Clinical programs").click();
  assert.equal(await page.locator("tbody tr").count(), 3);
  assert.match(await page.locator("tbody tr").filter({ hasText: "Device study" }).innerText(), /Not applicable/);
  assert.match(await page.locator("tbody tr").filter({ hasText: "Observational study" }).innerText(), /Phase not reported/);
  await search.fill("type 2 diabetes");
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 1);
  assert.match(await page.locator("tbody").innerText(), /Observational study/);
  await search.fill("");
  await page.waitForFunction(() => document.querySelectorAll("tbody tr").length === 3);

  await button("Follow a theme").click();
  assert.deepEqual(await page.locator("#theme-options option").evaluateAll(options => options.map(o => o.value)), ["Cardiac monitoring device", "ONC-101"]);
  await page.locator("#follow-kind").selectOption("indication");
  assert.deepEqual(await page.locator("#theme-options option").evaluateAll(options => options.map(o => o.value)), ["Heart failure", "Melanoma", "Type 2 diabetes"]);
  await page.locator("#follow-value").fill("Type 2 diabetes");
  await button("Add to watchlist").click();
  await page.locator(".pill").filter({ hasText: "indication: Type 2 diabetes" }).waitFor();
  assert.equal(follows[0].value, "Type 2 diabetes");

  await button("Change log").click();
  await button("Explore demo").click();
  const demoText = await page.locator("#change-feed-panel").innerText();
  assert.match(demoText, /type 2 diabetes/);
  assert.match(demoText, /rheumatoid arthritis/);
  assert.match(demoText, /solid tumors/);
  assert.deepEqual(errors, []);
  assert.deepEqual(unexpected, []);
  fs.mkdirSync(path.join(__dirname, "../artifacts"), { recursive: true });
  await page.screenshot({ path: path.join(__dirname, "../artifacts/healthcare-scope.png"), fullPage: true });
  console.log(JSON.stringify({ result: "passed", checks: "neutral workspace and metadata, actual company trial counts replace legacy area labels, true oncology names preserved, non-oncology trial rendering and search, device/missing phases, intervention and indication themes, diverse demo", liveDatabaseReads: 0, liveDatabaseWrites: 0 }));
  await browser.close();
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exitCode = 1; });
