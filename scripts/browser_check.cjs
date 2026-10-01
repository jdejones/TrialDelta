const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
let browser;
(async () => {
  browser = await chromium.launch({
    headless: true,
    channel: process.env.BROWSER_CHANNEL || "msedge",
  });
  const page = await browser.newPage({
    viewport: { width: 1512, height: 1100 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(process.env.APP_URL || "http://127.0.0.1:8765", {
    waitUntil: "networkidle",
  });
  await page.getByRole("heading", { name: "The change log." }).waitFor();
  fs.mkdirSync(path.join(__dirname, "../artifacts"), { recursive: true });
  await page.screenshot({
    path: path.join(__dirname, "../artifacts/desktop.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Explore demo", exact: true }).click();
  await page
    .getByRole("heading", { name: "Readout moves to the second half of 2026" })
    .waitFor();
  await page.getByRole("button", { name: "View evidence" }).first().click();
  await page.getByRole("dialog").waitFor();
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Coverage", exact: true }).click();
  await page
    .getByRole("heading", { name: "Your research universe." })
    .waitFor();
  const follow = page
    .getByRole("button", { name: "+ Follow", exact: true })
    .first();
  const rowName = await follow
    .locator("xpath=ancestor::tr")
    .locator("td")
    .first()
    .innerText();
  await follow.click();
  await page
    .getByRole("row")
    .filter({ hasText: rowName.split("\n")[0] })
    .getByRole("button", { name: "Following ✓", exact: true })
    .waitFor();
  // Restore this smoke test's watchlist mutation.
  await page
    .getByRole("row")
    .filter({ hasText: rowName.split("\n")[0] })
    .getByRole("button", { name: "Following ✓", exact: true })
    .click();
  await page
    .getByRole("row")
    .filter({ hasText: rowName.split("\n")[0] })
    .getByRole("button", { name: "+ Follow", exact: true })
    .waitFor();
  await page.getByRole("button", { name: "Daily digest", exact: true }).click();
  await page.getByRole("heading", { name: "Your daily brief." }).waitFor();
  await page.getByRole("button", { name: /Review queue/ }).click();
  await page.getByRole("heading", { name: "A second look." }).waitFor();
  await page.getByRole("button", { name: "Add source evidence" }).click();
  await page.getByLabel("Original source URL").waitFor();
  await page.getByRole("button", { name: "Close dialog" }).click();
  await page.getByRole("button", { name: "Source inbox", exact: true }).click();
  await page.getByRole("heading", { name: "Start with the source." }).waitFor();
  const recordButton = page
    .getByRole("button", { name: "Record evidence", exact: true })
    .first();
  if (await recordButton.count()) {
    await recordButton.click();
    if (
      !(await page.getByLabel("Original source URL").inputValue()).startsWith(
        "http",
      )
    )
      throw new Error("Source prefill failed");
    await page.getByRole("button", { name: "Close dialog" }).click();
  }
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await page.getByRole("heading", { name: "Workspace settings." }).waitFor();
  await page.getByRole("button", { name: "Change log", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(__dirname, "../artifacts/mobile.png"),
    fullPage: true,
  });
  const overflows = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  if (overflows) errors.push("Mobile document has horizontal overflow");
  console.log(
    JSON.stringify({
      browserErrors: errors,
      mobileOverflow: overflows,
      url: page.url(),
    }),
  );
  await browser.close();
  if (errors.length) process.exitCode = 1;
})().catch(async (e) => {
  console.error(e);
  if (browser) await browser.close();
  process.exitCode = 1;
});
