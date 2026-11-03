import { expect, test, type Page } from "@playwright/test";
import { createRequire } from "node:module";
import path from "node:path";

const OWNER = "e2e-owner-token-not-secret-0123456789abcdef";
const VIEWER = "e2e-viewer-token-not-secret-0123456789abcdef";
const require = createRequire(import.meta.url);
const AXE = require.resolve("axe-core/axe.min.js");
const SHOTS = path.join(import.meta.dirname, "screenshots");

async function signIn(page: Page, token = OWNER) {
  await page.goto("/");
  await page.getByLabel("Dashboard token").fill(token);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
}

async function go(page: Page, label: string, heading: string) {
  const menu = page.getByRole("button", { name: "Open menu" });
  if (await menu.isVisible()) await menu.click();
  await page.getByRole("navigation").getByRole("link", { name: label, exact: true }).click();
  await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
}

async function noHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow, "page must not scroll sideways").toBeLessThanOrEqual(1);
}

async function axe(page: Page) {
  await page.addScriptTag({ path: AXE });
  const violations = await page.evaluate(async () => {
    // @ts-expect-error injected global
    const result = await window.axe.run(document, { resultTypes: ["violations"] });
    return result.violations.map((v: { id: string; help: string; nodes: { target: string[] }[] }) => `${v.id}: ${v.help} (${v.nodes.length}) ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
  });
  expect(violations).toEqual([]);
}

const PAGES = [
  { label: "Overview", heading: "Overview", ready: "Spending trend (USD)" },
  { label: "Providers", heading: "Providers", ready: "ElevenLabs" },
  { label: "Payments", heading: "Payments", ready: "OAI-2026-0091" },
  { label: "Thresholds", heading: "Thresholds", ready: "Email delivery" },
  { label: "Alerts", heading: "Alert history", ready: "Gave up after 6 attempts" },
  { label: "AWS", heading: "AWS monitoring", ready: "tutor-worker" },
  // Without the AdhyaapakAI admin secret the page shows only the secret screen (no backend call).
  { label: "Voucher Management", heading: "Voucher Management", ready: "Unlock Voucher Management" },
  // The e2e fixture database has no courses: the page shows its real empty state.
  { label: "Course Management", heading: "Course Management", ready: "No courses yet" },
  // No app database in e2e: the page shows its real "not configured" notice.
  { label: "App Users", heading: "App Users", ready: "App users are not configured" },
  // No AdhyaapakAI backend in e2e: the page shows its real "not configured" notice.
  { label: "Account Deletions", heading: "Account Deletions", ready: "Account deletions are not configured" },
];

test("content security policy blocks injected inline scripts", async ({ page }) => {
  const response = await page.goto("/");
  expect(response?.headers()["content-security-policy"]).toContain("script-src 'self'");
  await expect(page.addScriptTag({ content: "window.__injected = true" })).rejects.toThrow(/Content Security Policy/);
  expect(await page.evaluate(() => (window as unknown as { __injected?: boolean }).__injected)).toBeUndefined();
});

// axe-core is injected as a test-only script, which the UI's CSP rightly blocks; bypass it
// for this scan only (the test above proves the policy is enforced).
test.describe("audited pages", () => {
  test.use({ bypassCSP: true });

test("every page renders real data, fits the viewport and passes axe", async ({ page }, info) => {
  test.setTimeout(180_000);
  const consoleErrors: string[] = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  const notFound: string[] = [];
  const unauthorized: string[] = [];
  page.on("response", (r) => {
    if (r.status() === 404) notFound.push(new URL(r.url()).pathname);
    if (r.status() === 401) unauthorized.push(new URL(r.url()).pathname);
  });
  const external: string[] = [];
  page.on("request", (r) => {
    const host = new URL(r.url()).host;
    if (host !== new URL(info.project.use.baseURL!).host) external.push(r.url());
  });

  await signIn(page);
  for (const p of PAGES) {
    await go(page, p.label, p.heading);
    await expect(page.getByText(p.ready).first()).toBeVisible();
    // (No "networkidle" wait: the live-update stream intentionally stays open.)
    await expect(page.getByRole("status").filter({ hasText: /^Loading/ })).toHaveCount(0);
    await noHorizontalOverflow(page);
    await axe(page);
    await page.screenshot({ path: path.join(SHOTS, `${info.project.name}-${p.label.toLowerCase()}.png`), fullPage: true });
  }
  // provider detail page
  await go(page, "Providers", "Providers");
  await page.getByRole("link", { name: /ElevenLabs/ }).first().click();
  await expect(page.getByRole("heading", { name: "ElevenLabs", level: 1 })).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Plan credits used" })).toBeVisible();
  await expect(page.getByText("rate limited by provider (429)")).toBeVisible();
  await noHorizontalOverflow(page);
  await axe(page);
  await page.screenshot({ path: path.join(SHOTS, `${info.project.name}-provider-detail.png`), fullPage: true });

  expect(external, "the browser must only talk to the dashboard backend").toEqual([]);
  // A provider without a threshold answers 404 (threshold_not_configured), which the UI
  // shows as "No threshold configured". Browsers log any 404 to the console, so those
  // expected responses are the only console errors allowed.
  expect(notFound.every((p) => /^\/api\/v1\/providers\/[a-z]+\/threshold$/.test(p)), notFound.join(", ")).toBe(true);
  // Before sign-in the page asks "is there a session?" and correctly gets 401 once.
  expect(unauthorized).toEqual(["/api/v1/auth/session"]);
  expect(consoleErrors.filter((e) => !e.includes("404 (Not Found)") && !e.includes("401 (Unauthorized)"))).toEqual([]);
  expect(consoleErrors.length).toBeLessThanOrEqual(notFound.length + unauthorized.length);
  // the token never appears in the page or in any URL
  expect(await page.content()).not.toContain(OWNER);
});
});

test("keyboard: skip link, navigation and dialogs", async ({ page }, info) => {
  test.skip(info.project.name === "mobile", "keyboard flow checked on desktop/tablet");
  await signIn(page);
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();

  await go(page, "Payments", "Payments");
  const del = page.getByRole("button", { name: /Delete payment OAI-2026-0091/ });
  await del.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "Delete this payment?" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused(); // safe default
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.getByText("OAI-2026-0091")).toBeVisible(); // nothing deleted
});

test("viewer token is read-only in the UI and the API", async ({ page }) => {
  await signIn(page, VIEWER);
  await expect(page.getByText("Viewer (read-only)")).toBeAttached();
  await go(page, "Payments", "Payments");
  await expect(page.getByText("OAI-2026-0091")).toBeVisible();
  await expect(page.getByRole("button", { name: /Record payment/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Delete payment/ })).toHaveCount(0);
  // Even a hand-crafted request with the real CSRF token is refused for a viewer.
  const status = await page.evaluate(async () => {
    const s = await (await fetch("/api/v1/auth/session")).json();
    const r = await fetch("/api/v1/payments/1", { method: "DELETE", headers: { "X-CSRF-Token": s.csrf_token } });
    return r.status;
  });
  expect(status).toBe(403);
});

test("session cookie is HttpOnly and SameSite, nothing in storage, CSRF enforced", async ({ page, context }) => {
  await signIn(page);
  const [cookie] = (await context.cookies()).filter((c) => c.name.includes("acm_session"));
  expect(cookie, "session cookie set").toBeDefined();
  expect(cookie!.httpOnly).toBe(true);
  expect(cookie!.sameSite).toBe("Strict");
  expect(cookie!.value).not.toContain(OWNER);
  const visible = await page.evaluate(() => ({
    cookie: document.cookie,
    local: Object.keys(localStorage).length,
    session: Object.keys(sessionStorage).length,
  }));
  expect(visible).toEqual({ cookie: "", local: 0, session: 0 }); // scripts cannot read the session
  const withoutCsrf = await page.evaluate(async () => (await fetch("/api/v1/payments/1", { method: "DELETE" })).status);
  expect(withoutCsrf).toBe(403);
  // Sign out ends the session server-side.
  await page.getByRole("button", { name: "Sign out" }).first().click({ force: true }).catch(async () => {
    await page.getByRole("button", { name: "Open menu" }).click();
    await page.getByRole("button", { name: "Sign out" }).click();
  });
  await expect(page.getByLabel("Dashboard token")).toBeVisible();
  const after = await page.evaluate(async () => (await fetch("/api/v1/providers")).status);
  expect(after).toBe(401);
});

test("payment lifecycle: record, apply to balance explicitly, delete with confirmation", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "mutating flow runs once");
  await signIn(page);
  await go(page, "Payments", "Payments");
  await page.getByRole("button", { name: /Record payment/ }).click();
  const form = page.getByRole("dialog", { name: "Record a payment" });
  await form.getByLabel("Provider").selectOption("openai");
  await form.getByLabel("Amount").fill("15.25");
  await form.getByLabel("Reference").fill("E2E-REF-1");
  await form.getByRole("button", { name: "Record payment" }).click();
  await expect(page.getByText(/does not change any balance/)).toBeVisible();
  const row = page.getByRole("row", { name: /E2E-REF-1/ });
  await expect(row).toBeVisible();

  // Recording did not change the balance; only the explicit action does.
  await row.getByRole("button", { name: "Apply to balance" }).click();
  const apply = page.getByRole("dialog", { name: "Apply payment to balance" });
  await expect(apply).toContainText("$15.25");
  await apply.getByRole("button", { name: "Apply to balance" }).click();
  await expect(page.getByText(/Balance updated to/)).toBeVisible();
  await expect(row).toContainText("Applied to balance");

  await row.getByRole("button", { name: /Delete payment E2E-REF-1/ }).click();
  await page.getByRole("dialog", { name: "Delete this payment?" }).getByRole("button", { name: "Delete payment" }).click();
  await expect(page.getByText(/Payment deleted/)).toBeVisible();
  await expect(page.getByRole("row", { name: /E2E-REF-1/ })).toHaveCount(0);
});

test("thresholds: validation, save feedback and email preview", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop", "mutating flow runs once");
  await signIn(page);
  await go(page, "Thresholds", "Thresholds");
  const section = page.locator("#threshold-deepgram");
  const amount = section.getByLabel("Alert when balance is at or below (USD)");
  await amount.fill("abc");
  await section.getByRole("button", { name: "Save threshold" }).click();
  await expect(section.getByText("Enter an amount like 25.00.")).toBeVisible();
  await amount.fill("20.00");
  await section.getByRole("button", { name: "Save threshold" }).click();
  await expect(page.getByText(/Deepgram threshold saved/)).toBeVisible();
  await section.getByRole("button", { name: "Preview alert email" }).click();
  const preview = page.getByRole("dialog", { name: "Alert email preview" });
  await expect(preview).toContainText("Subject:");
  await page.screenshot({ path: path.join(SHOTS, "desktop-email-preview.png") });
  await page.keyboard.press("Escape");
});

test("mobile navigation menu", async ({ page }, info) => {
  test.skip(info.project.name !== "mobile", "mobile only");
  await signIn(page);
  const open = page.getByRole("button", { name: "Open menu" });
  await expect(open).toHaveAttribute("aria-expanded", "false");
  await open.click();
  await expect(open).toHaveAttribute("aria-expanded", "true");
  await page.screenshot({ path: path.join(SHOTS, "mobile-menu.png") });
  await page.getByRole("navigation").getByRole("link", { name: "Alerts" }).click();
  await expect(page.getByRole("heading", { name: "Alert history", level: 1 })).toBeVisible();
  await expect(open).toHaveAttribute("aria-expanded", "false");
});

