// Rehearsal browser check with the locally installed Chrome (run from frontend/ so that
// @playwright/test resolves):  node ../deploy/rehearsal/browser_check.mjs URL [SCREENSHOT_DIR]
// The owner token comes from $ACM_TOKEN and is never printed.
import { createRequire } from "node:module";

// Resolve Playwright from the current directory (frontend/), not from this file's folder.
const { chromium } = createRequire(`${process.cwd()}/`)("@playwright/test");

const [url, shots = "."] = process.argv.slice(2);
const token = process.env.ACM_TOKEN;
if (!url || !token) throw new Error("usage: ACM_TOKEN=... node browser_check.mjs URL [DIR]");

const browser = await chromium.launch({ channel: "chrome" });
const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 860 } });
const page = await context.newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
const ok = (msg) => console.log(`PASS  ${msg}`);
const fail = (msg) => { problems.push(msg); console.log(`FAIL  ${msg}`); };
const label = new URL(url).port === "8443" ? "https" : "tunnel";

await page.goto(url);
await page.getByLabel("Dashboard token").fill(token);
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview", level: 1 }).waitFor({ timeout: 15000 });
ok(`signed in at ${url}`);

const cookie = (await context.cookies()).find((c) => c.name.endsWith("acm_session"));
cookie && cookie.httpOnly && cookie.secure && cookie.sameSite === "Strict"
  ? ok(`session cookie ${cookie.name}: HttpOnly, Secure, SameSite=Strict`)
  : fail(`session cookie flags: ${JSON.stringify(cookie && { name: cookie.name, httpOnly: cookie.httpOnly, secure: cookie.secure, sameSite: cookie.sameSite })}`);
const readable = await page.evaluate(() => document.cookie.includes("acm_session"));
readable ? fail("session cookie readable by scripts") : ok("session cookie not readable by page scripts");
const stored = await page.evaluate(() => localStorage.length + sessionStorage.length);
stored === 0 ? ok("nothing in localStorage/sessionStorage") : fail(`${stored} item(s) in browser storage`);

try {
  await page.getByText("Live updates on").first().waitFor({ timeout: 15000 });
  ok("live updates (Server-Sent Events) connected");
} catch {
  fail("live updates did not connect");
}
await page.screenshot({ path: `${shots}/rehearsal-${label}-overview.png`, fullPage: true });

await page.getByRole("link", { name: "Providers" }).first().click();
await page.getByRole("heading", { name: "Providers", level: 1 }).waitFor();
const cards = await page.locator("a.card-link h2").allInnerTexts();
cards.length === 5 ? ok(`providers page: ${cards.join(", ")}`) : fail(`providers page shows ${cards.length} providers`);
await page.screenshot({ path: `${shots}/rehearsal-${label}-providers.png`, fullPage: true });

await page.getByRole("link", { name: "AWS" }).first().click();
await page.getByRole("heading", { name: "Infrastructure health" }).waitFor();
(await page.getByRole("heading", { name: "Billing" }).isVisible())
  ? ok("AWS page separates infrastructure health from billing")
  : fail("AWS billing section missing");

await page.getByRole("button", { name: "Sign out" }).click();
await page.getByText("You have signed out.").waitFor();
ok("signed out");

await browser.close();
console.log(problems.length ? `FAILED: ${problems.join("; ")}` : "OK: browser checks passed");
process.exit(problems.length ? 1 : 0);
