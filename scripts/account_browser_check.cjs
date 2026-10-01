// End-to-end customer accounts against an isolated temporary SQLite database.
// No requests, account writes, or mail are sent to the normal workstation app.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawn, spawnSync } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "trialdelta-accounts-"));
const port = Number(process.env.ACCOUNT_TEST_PORT || 18765);
const base = `http://127.0.0.1:${port}`;
const python = process.env.PYTHON || path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const headers = { "X-TrialDelta-Client": "web" };
let server;
let browser;
let serverLog = "";
const errors = [];
const password = "Initial account password 2026!";
const nextPassword = "Changed account password 2026!";
const screenshotDir = path.join(root, "artifacts");
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function screenshot(page, name) {
  fs.mkdirSync(screenshotDir, { recursive: true });
  await page.screenshot({ path: path.join(screenshotDir, name + ".png"), fullPage: true });
}
async function signedIn(page) {
  await page.getByRole("button", { name: "Account", exact: true }).waitFor();
}
async function account(page) {
  await page.getByRole("button", { name: "Account", exact: true }).click();
  await page.locator("#profile-form").waitFor();
}
async function signIn(page, email, secret) {
  await page.locator("#login-email").fill(email);
  await page.locator("#login-password").fill(secret);
  await page.locator("#login-form").getByRole("button", { name: "Sign in", exact: true }).click();
  await signedIn(page);
}
(async () => {
  server = spawn(python, ["-m", "uvicorn", "trialdelta.app:app", "--host", "127.0.0.1", "--port", String(port), "--no-proxy-headers"], {
    cwd: root, windowsHide: true, stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, DATABASE_URL: "sqlite:///" + path.join(scratch, "accounts.db").replaceAll("\\", "/"), USE_LOCAL_MYSQL: "false", SOURCE_DATABASE_URL: "", APP_MODE: "local", APP_ORIGIN: base, AUTH_REQUIRED: "true", PUBLIC_BROWSING_ENABLED: "false", REGISTRATION_ENABLED: "true", COOKIE_SECURE: "false", SMTP_HOST: "", SMTP_FROM: "", SMTP_USER: "", SMTP_PASSWORD: "" },
  });
  server.stdout.on("data", data => { serverLog = (serverLog + data).slice(-12000); });
  server.stderr.on("data", data => { serverLog = (serverLog + data).slice(-12000); });
  server.on("error", err => { serverLog += err.message; });
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error("Isolated server exited: " + serverLog);
    try {
      const response = await fetch(base + "/api/auth/config", { signal: AbortSignal.timeout(1000) });
      if (response.ok && (await response.json()).auth_required) { ready = true; break; }
    } catch {}
    await sleep(200);
  }
  assert.ok(ready, "Isolated server did not start: " + serverLog);
  browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_CHANNEL === "chromium" ? {} : { channel: process.env.BROWSER_CHANNEL || "msedge" }) });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  const page = await context.newPage();
  await page.bringToFront();
  page.on("pageerror", err => errors.push(err.message));
  await page.goto(base, { waitUntil: "networkidle" });
  await page.locator("#login-form").waitFor();
  assert.equal((await context.request.get(base + "/api/account")).status(), 401);
  await screenshot(page, "account-login-desktop");
  await page.getByRole("button", { name: "Create account", exact: true }).click();
  await page.locator("#register-name").fill("Alex Researcher");
  await page.locator("#register-email").fill("alex@example.org");
  await page.locator("#register-password").fill(password);
  await page.locator("#register-confirm").fill("A different password value");
  await page.locator("#register-form").getByRole("button", { name: "Create account", exact: true }).click();
  assert.ok(await page.locator("#register-form").count(), "Mismatch must not register the account");
  await page.locator("#register-confirm").fill(password);
  await page.locator("#register-form").getByRole("button", { name: "Create account", exact: true }).click();
  await signedIn(page);
  let me = await (await context.request.get(base + "/api/me")).json();
  assert.equal(me.email, "alex@example.org");
  assert.equal(me.is_admin, false);
  assert.equal(me.email_verified, false);
  const cookies = await context.cookies();
  const session = cookies.find(cookie => cookie.name === "td_session");
  assert.ok(session.httpOnly && session.sameSite === "Lax");
  assert.ok(!(await page.evaluate(() => document.cookie)).includes("td_session"));
  await account(page);
  assert.equal(await page.locator("#account-email").inputValue(), "alex@example.org");
  assert.ok(await page.locator("#account-email").getAttribute("readonly") !== null);
  await page.locator("#account-name").fill("Alex <research>");
  await page.getByRole("button", { name: "Save profile", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("saved"));
  await page.reload({ waitUntil: "networkidle" });
  await account(page);
  assert.equal(await page.locator("#account-name").inputValue(), "Alex <research>");
  assert.equal(await page.locator("research").count(), 0, "Display name must be escaped");
  await page.locator("#digest-enabled").check();
  await page.getByRole("button", { name: "Save preferences", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#toast")?.textContent.includes("saved"));
  me = await (await context.request.get(base + "/api/me")).json();
  assert.equal(me.digest_enabled, true);
  await screenshot(page, "account-desktop");
  for (const width of [360, 390, 768]) {
    await page.setViewportSize({ width, height: 900 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "Account overflow at " + width);
  }
  await screenshot(page, "account-mobile");
  await page.setViewportSize({ width: 1440, height: 1050 });

  const sibling = await context.newPage();
  sibling.on("pageerror", err => errors.push(err.message));
  await sibling.goto(base + "/#account", { waitUntil: "networkidle" });
  await sibling.locator("#profile-form").waitFor();
  const second = await browser.newContext();
  const otherPage = await second.newPage();
  otherPage.on("pageerror", err => errors.push(err.message));
  await otherPage.goto(base, { waitUntil: "networkidle" });
  await signIn(otherPage, "alex@example.org", password);
  await page.bringToFront();
  console.log("Checking password changes and session revocation");
  await page.locator("#current-password").fill("Wrong current password");
  await page.locator("#new-password").fill(nextPassword);
  await page.locator("#confirm-new-password").fill(nextPassword);
  await page.getByRole("button", { name: "Change password", exact: true }).click();
  await page.locator("#password-form .error").filter({ hasText: /incorrect/i }).waitFor();
  assert.ok(await page.locator("#profile-form").count(), "Wrong current password must keep the account visible");
  await page.locator("#current-password").fill(password);
  await page.getByRole("button", { name: "Change password", exact: true }).click();
  await page.waitForFunction(() => document.querySelector("#toast")?.textContent.toLowerCase().includes("password changed"));
  assert.equal((await second.request.get(base + "/api/me")).status(), 401, "Other sessions must be revoked");
  await otherPage.bringToFront();
  await otherPage.getByRole("button", { name: "Account", exact: true }).click();
  await otherPage.locator("#login-form").waitFor();
  assert.equal(await otherPage.locator("#account-email").count(), 0, "Expired session must clear account information");
  await page.bringToFront();
  await page.getByRole("button", { name: "Sign out all devices", exact: true }).click();
  await page.locator("#login-form").waitFor();
  await sibling.bringToFront();
  await sibling.locator("#login-form").waitFor();
  await page.bringToFront();
  await signIn(page, "alex@example.org", nextPassword);
  await page.getByRole("button", { name: "Sign out", exact: true }).first().click();
  await page.locator("#login-form").waitFor();
  assert.equal((await context.request.get(base + "/api/me")).status(), 401);

  await page.goto(base + "/#forgot-password", { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "Reset your password.", exact: true }).waitFor();
  assert.match(await page.locator("#app").innerText(), /unavailable|not.*configured|not.*available|isn.t.*available/i);
  await page.goto(base + "/#reset-password=" + "x".repeat(64), { waitUntil: "networkidle" });
  await page.locator("#reset-form").waitFor();
  assert.ok(!page.url().includes("x".repeat(64)), "Recovery token must be scrubbed from the URL");
  await page.locator("#reset-password").fill(nextPassword);
  await page.locator("#reset-confirm").fill(nextPassword);
  await page.locator("#reset-form button[type=submit]").click();
  await page.locator("#reset-form .error").filter({ hasText: /invalid|expired/i }).waitFor();
  await page.goto(base + "/#signup", { waitUntil: "networkidle" });
  await page.locator("#register-name").fill("Blair Researcher");
  await page.locator("#register-email").fill("blair@example.org");
  await page.locator("#register-password").fill(password);
  await page.locator("#register-confirm").fill(password);
  await page.locator("#register-form").getByRole("button", { name: "Create account", exact: true }).click();
  await signedIn(page);
  await account(page);
  assert.equal(await page.locator("#account-name").inputValue(), "Blair Researcher");
  assert.equal(await page.locator("#account-email").inputValue(), "blair@example.org");
  assert.equal((await (await context.request.get(base + "/api/me")).json()).digest_enabled, false);
  await sibling.bringToFront();
  await signedIn(sibling);
  await account(sibling);
  assert.equal(await sibling.locator("#account-email").inputValue(), "blair@example.org", "Other tabs must adopt the new account without old-user data");
  assert.deepEqual(errors, [], "Browser errors");
  console.log(JSON.stringify({ result: "passed", database: "isolated temporary SQLite", accounts: "signup, mismatch, login, profile persistence, escaped names, preferences, password change, session revocation, logout, recovery errors, identity separation", responsiveWidths: [360, 390, 768, 1440], browserErrors: errors, liveDatabaseWrites: 0, emailSent: 0 }));
})().catch(async err => {
  console.error(err);
  process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server?.pid && server.exitCode === null) {
    if (process.platform === "win32") spawnSync("taskkill", ["/PID", String(server.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
    else { server.kill("SIGTERM"); await new Promise(resolve => server.once("exit", resolve)); }
  }
  const resolved = path.resolve(scratch);
  if (path.dirname(resolved) !== path.resolve(os.tmpdir()) || !path.basename(resolved).startsWith("trialdelta-accounts-")) throw new Error("Unexpected temporary directory");
  fs.rmSync(resolved, { recursive: true, force: true });
});
