// End-to-end visitor browsing against an isolated, temporary SQLite application.
// Research fixtures, account writes, and sessions never touch the workstation app.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawn, spawnSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "trialdelta-visitors-"));
const port = Number(process.env.VISITOR_TEST_PORT || 18766);
const base = `http://127.0.0.1:${port}`;
const python = process.env.PYTHON || path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const env = {
  ...process.env,
  DATABASE_URL: "sqlite:///" + path.join(scratch, "visitors.db").replaceAll("\\", "/"),
  USE_LOCAL_MYSQL: "false", SOURCE_DATABASE_URL: "", APP_MODE: "local", APP_ORIGIN: base,
  AUTH_REQUIRED: "true", PUBLIC_BROWSING_ENABLED: "true", REGISTRATION_ENABLED: "true", COOKIE_SECURE: "false",
  SMTP_HOST: "", SMTP_FROM: "", SMTP_USER: "", SMTP_PASSWORD: "",
};
const seed = `
import json
from datetime import datetime, timedelta, timezone
from trialdelta.db import initialize, Session, Company, Study, Snapshot, Change, now
initialize()
with Session() as db:
    company = Company(id=991101, ticker="VSTR", name="Visitor Biotech", sponsor="Visitor Biotech", area="Oncology")
    db.add(company)
    db.flush()
    study = Study(id="NCT09911001", company_id=company.id, title="Visitor trial of VS-101 for solid tumors", drug="VS-101", conditions='["Solid tumors"]', targets='["MET"]', phase="PHASE2", status="RECRUITING", primary_completion="2028-06", last_checked=now())
    db.add(study)
    db.flush()
    previous = Snapshot(study_id=study.id, captured_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), source_updated="2026-09-23", digest="1" * 64, raw_json=json.dumps({"protocolSection": {"identificationModule": {"nctId": study.id}, "enrollment": 120}}), normalized=json.dumps({"primary_completion": "2028-06", "enrollment": 120}))
    current = Snapshot(study_id=study.id, captured_at=now(), source_updated="2026-09-24", digest="2" * 64, raw_json=json.dumps({"protocolSection": {"identificationModule": {"nctId": study.id}, "enrollment": 180}}), normalized=json.dumps({"primary_completion": "2028-06", "enrollment": 180}))
    db.add_all([previous, current])
    db.flush()
    db.add(Change(company_id=company.id, study_id=study.id, category="enrollment", title="Visitor trial enrollment target increased", before_value="120 participants", after_value="180 participants", source="registry", review_status="automatic", before_snapshot=previous.id, after_snapshot=current.id, detected_at=now()))
    db.commit()
`;
const password = "Visitor account password 2026!";
const errors = [];
const mutations = [];
const privateRequests = [];
const operationalRequests = [];
let browser;
let server;
let serverLog = "";
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const screenshotDir = path.join(root, "artifacts");
async function screenshot(page, name) {
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: path.join(screenshotDir, name + ".png"), fullPage: true });
}
(async () => {
  const seeded = spawnSync(python, ["-c", seed], { cwd: root, env, windowsHide: true, encoding: "utf8" });
  assert.equal(seeded.status, 0, "Isolated fixture setup failed: " + seeded.stderr);
  server = spawn(python, ["-m", "uvicorn", "trialdelta.app:app", "--host", "127.0.0.1", "--port", String(port), "--no-proxy-headers"], {
    cwd: root, windowsHide: true, stdio: ["ignore", "pipe", "pipe"], env,
  });
  server.stdout.on("data", data => { serverLog = (serverLog + data).slice(-12000); });
  server.stderr.on("data", data => { serverLog = (serverLog + data).slice(-12000); });
  server.on("error", err => { serverLog += err.message; });
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error("Isolated server exited: " + serverLog);
    try {
      const response = await fetch(base + "/api/auth/config", { signal: AbortSignal.timeout(1000) });
      if (response.ok) {
        const config = await response.json();
        if (config.auth_required && config.public_browsing_enabled) { ready = true; break; }
      }
    } catch {}
    await sleep(200);
  }
  assert.ok(ready, "Isolated visitor server did not start: " + serverLog);
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "chrome" }) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  const page = await context.newPage();
  await page.bringToFront();
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    const pathname = new URL(request.url()).pathname;
    if (!pathname.startsWith("/api/")) return;
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) mutations.push(request.method() + " " + pathname);
    if (/^\/api\/(account|digest|follows)(\/|$)/.test(pathname)) privateRequests.push(request.method() + " " + pathname);
    if (/^\/api\/(overview|sync|jobs|review|inbox)(\/|$)/.test(pathname)) operationalRequests.push(pathname);
  });
  const nav = name => page.getByRole("navigation", { name: "Main navigation" }).getByRole("button", { name, exact: true });
  const heading = title => page.getByRole("heading", { name: title, exact: true });
  async function guestHome() {
    await page.locator(".visitor-shell").waitFor();
    await heading("Home.").waitFor();
    await page.locator(".home-trial-card").first().waitFor();
    assert.equal(await page.locator("#login-form").count(), 0, "Public browsing must not force a login form");
  }
  async function continueBrowsingPrompt() {
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("heading", { name: "Make this research your own.", exact: true }).waitFor();
    await dialog.getByRole("button", { name: "Continue browsing", exact: true }).click();
    await dialog.waitFor({ state: "hidden" });
  }
  async function signIn() {
    await page.locator("#login-email").fill("visitor@example.org");
    await page.locator("#login-password").fill(password);
    await page.locator("#login-form button[type=submit]").click();
    await page.locator(".avatar").waitFor();
    assert.equal(await page.locator(".visitor-shell").count(), 0);
  }
  await page.goto(base, { waitUntil: "networkidle" });
  await guestHome();
  assert.equal((await context.request.get(base + "/api/me")).status(), 401);
  assert.equal((await context.cookies()).some(cookie => cookie.name === "td_session"), false, "Visitors must not get fake account sessions");
  await page.locator('.visitor-actions [data-auth="login"]').waitFor();
  await page.locator('.visitor-actions [data-auth="signup"]').waitFor();
  assert.equal(await page.locator(".visitor-label").innerText(), "Visitor");
  await screenshot(page, "visitor-home-desktop");
  await page.locator(".home-trial-card").first().getByRole("button", { name: "Open trial" }).click();
  await page.getByRole("dialog").getByRole("heading", { name: "NCT09911001", exact: true }).waitFor();
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await page.getByRole("tab", { name: "News", exact: true }).click();
  await heading("No biotech news yet.").waitFor();

  await nav("Change log").click();
  await heading("The change log.").waitFor();
  await heading("Visitor trial enrollment target increased").waitFor();
  assert.equal(await page.getByRole("button", { name: "Following only", exact: true }).getAttribute("aria-pressed"), "false", "Visitors must see all public changes by default");
  await page.locator("#change-feed-panel [data-change]").first().click();
  await page.getByRole("dialog").getByRole("heading", { name: "Visitor trial enrollment target increased", exact: true, level: 2 }).waitFor();
  assert.equal(await page.getByRole("dialog").locator(".evidence").count(), 2, "Public source comparison must show both registry records");
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await page.getByRole("button", { name: "Explore demo", exact: true }).click();
  await heading("Readout moves to the second half of 2026").waitFor();
  await page.locator("#change-feed-panel [data-change]").first().click();
  await page.getByRole("dialog").getByText("Fictional example. This is not research about a real company.", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await page.getByRole("button", { name: "Add evidence", exact: true }).click();
  await continueBrowsingPrompt();
  assert.equal(await page.locator("#evidence-form").count(), 0);

  await nav("Coverage").click();
  await heading("Explore the research universe.").waitFor();
  await page.locator("[data-follow-company]").first().click();
  await continueBrowsingPrompt();
  await page.getByRole("button", { name: "Clinical programs", exact: true }).click();
  await page.locator("[data-follow-study]").first().click();
  await continueBrowsingPrompt();
  await page.getByRole("button", { name: "Follow a theme", exact: true }).click();
  await continueBrowsingPrompt();
  assert.equal(await page.locator("#follow-form").count(), 0);
  for (const name of ["Account", "Daily digest", "Settings"]) {
    await nav(name).click();
    await page.locator(".visitor-invite").waitFor();
    await page.locator('.visitor-invite [data-auth="login"]').waitFor();
    await page.locator('.visitor-invite [data-auth="signup"]').waitFor();
    assert.equal(await page.locator("#profile-form, #preferences-form, #password-form").count(), 0);
    if (name === "Account") await screenshot(page, "visitor-account-invitation");
  }
  assert.deepEqual(mutations, [], "Visitor research and account invitations must not attempt mutations");
  assert.deepEqual(privateRequests, [], "Guest pages must not fetch account, watchlist, or digest data");

  for (const route of ["login", "signup", "forgot-password"]) {
    await page.goto(base + "/#" + route, { waitUntil: "networkidle" });
    await page.locator(".auth-card").waitFor();
    if (route === "login") await page.locator("#login-form").waitFor();
    if (route === "signup") await page.locator("#register-form").waitFor();
    if (route === "forgot-password") await heading("Reset your password.").waitFor();
    await page.locator('[data-action="browse"]').click();
    await guestHome();
  }
  for (const width of [360, 390, 768]) {
    await page.setViewportSize({ width, height: 900 });
    for (const name of ["Home", "Coverage", "Account", "Daily digest"]) {
      await nav(name).click();
      if (["Account", "Daily digest"].includes(name)) await page.locator(".visitor-invite").waitFor();
      else await heading(name === "Home" ? "Home." : "Explore the research universe.").waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `Visitor ${name} overflow at ${width}px`);
      await page.locator('.visitor-actions [data-auth="login"]').waitFor();
      await page.locator('.visitor-actions [data-auth="signup"]').waitFor();
    }
    if (width === 390) { await nav("Home").click(); await guestHome(); await screenshot(page, "visitor-home-mobile"); }
  }
  assert.deepEqual(mutations, [], "Exploring auth routes and responsive views must not submit mutations");
  assert.deepEqual(privateRequests, [], "Responsive guest views must remain independent of private APIs");
  await page.setViewportSize({ width: 1440, height: 1050 });

  // A stale cookie falls back to browsing instead of trapping the visitor at login.
  const expiredContext = await browser.newContext();
  await expiredContext.addCookies([{ name: "td_session", value: "expired-visitor-session", url: base, httpOnly: true }]);
  const expiredPage = await expiredContext.newPage();
  expiredPage.on("pageerror", error => errors.push(error.message));
  await expiredPage.bringToFront();
  await expiredPage.goto(base + "/#coverage", { waitUntil: "networkidle" });
  await expiredPage.locator(".visitor-shell").waitFor();
  await expiredPage.getByRole("heading", { name: "Explore the research universe.", exact: true }).waitFor();
  await expiredContext.close();
  await page.bringToFront();

  // All accounts in the remaining checks live only in the temporary fixture DB.
  await page.goto(base + "/#signup", { waitUntil: "networkidle" });
  await page.locator("#register-name").fill("Visitor Researcher");
  await page.locator("#register-email").fill("visitor@example.org");
  await page.locator("#register-password").fill(password);
  await page.locator("#register-confirm").fill(password);
  await page.locator("#register-form button[type=submit]").click();
  await page.locator(".avatar").waitFor();
  assert.equal((await (await context.request.get(base + "/api/me")).json()).email, "visitor@example.org");
  await nav("Account").click();
  await page.locator("#profile-form").waitFor();
  assert.equal(await page.locator("#account-email").inputValue(), "visitor@example.org");
  await page.getByRole("button", { name: "Sign out", exact: true }).first().click();
  await guestHome();
  assert.equal((await context.request.get(base + "/api/me")).status(), 401);
  assert.equal(await page.locator("#account-email").count(), 0);
  await page.goto(base + "/#login", { waitUntil: "networkidle" });
  await signIn();
  await page.getByRole("button", { name: "Sign out", exact: true }).first().click();
  await guestHome();

  await nav("Coverage").click();
  await page.locator("[data-follow-company]").first().click();
  await page.getByRole("dialog").locator('[data-auth="login"]').click();
  await signIn();
  await heading("Your research universe.").waitFor();
  assert.equal(await page.locator("[data-follow-company]").first().innerText(), "+ Follow", "Signing in must not replay the attempted visitor follow");
  assert.equal(mutations.some(value => /\/api\/(follows|evidence)/.test(value)), false, "Visitor prompts must never replay writes after authentication");
  await page.getByRole("button", { name: "Sign out", exact: true }).first().click();
  await guestHome();
  assert.deepEqual(operationalRequests, [], "Visitor and account UI must not call administrative operations");
  assert.deepEqual(errors, [], "Browser errors");
  console.log(JSON.stringify({
    result: "passed", database: "isolated temporary SQLite",
    visitors: "public Home, news empty state, trial and evidence details, change log, demo, coverage, account/digest invitations, follow/evidence prompts, explicit auth routes, continue browsing, stale session fallback, signup/login/logout, intended-page return without replayed writes",
    responsiveWidths: [360, 390, 768, 1440], browserErrors: errors,
    guestMutationAttempts: 0, liveDatabaseWrites: 0, emailSent: 0,
  }));
})().catch(async error => {
  console.error(error);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server?.pid && server.exitCode === null) {
    if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(server.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    else { server.kill("SIGTERM"); await new Promise(resolve => server.once("exit", resolve)); }
  }
  const resolved = path.resolve(scratch);
  if (path.dirname(resolved) !== path.resolve(os.tmpdir()) || !path.basename(resolved).startsWith("trialdelta-visitors-")) throw new Error("Unexpected temporary directory");
  fs.rmSync(resolved, { recursive: true, force: true });
});
