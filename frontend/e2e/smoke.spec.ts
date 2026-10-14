import { expect, test, type Page } from "@playwright/test";

const ROUTES = [
  "#/", "#/films", "#/pick", "#/downloads", "#/timeline",
  "#/settings", "#/settings/services", "#/settings/sync", "#/settings/sites",
  "#/settings/playlists", "#/settings/rules", "#/settings/appearance", "#/settings/backups", "#/settings/logs",
];
const WIDTHS = [1440, 768, 375];

function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
  });
  return errors;
}

for (const width of WIDTHS) {
  test(`全部页面可渲染且无横向溢出 @${width}`, async ({ page }) => {
    const errors = watchErrors(page);
    await page.setViewportSize({ width, height: 900 });
    for (const route of ROUTES) {
      await page.goto(`/${route}`);
      await page.waitForLoadState("networkidle");
      await expect(page.locator("#app, main").first(), route).not.toBeEmpty();
      await expect(page.getByText("页面不存在"), route).toHaveCount(0);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `${route} 横向溢出`).toBeLessThanOrEqual(0);
    }
    expect(errors).toEqual([]);
  });
}

test("hash 路由支持刷新与前进后退", async ({ page }) => {
  await page.goto("/#/settings/rules");
  await page.reload();
  await expect(page).toHaveURL(/#\/settings\/rules$/);
  await page.goto("/#/timeline");
  await page.goBack();
  await expect(page).toHaveURL(/#\/settings\/rules$/);
  await page.goForward();
  await expect(page).toHaveURL(/#\/timeline$/);
});

test("四套主题可切换", async ({ page }) => {
  const errors = watchErrors(page);
  await page.goto("/#/settings/appearance");
  await page.waitForLoadState("networkidle");
  for (const theme of ["paper", "jade", "lacquer", "darkroom"]) {
    await page.evaluate((id) => document.documentElement.setAttribute("data-theme", id), theme);
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    expect(bg, theme).not.toBe("");
  }
  expect(errors).toEqual([]);
});

test("不存在的页面给出提示", async ({ page }) => {
  await page.goto("/#/nowhere");
  await expect(page.locator("body")).toContainText(/不存在|未找到/);
});
