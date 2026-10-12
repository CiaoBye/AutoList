import { defineConfig } from "@playwright/test";

// 冒烟测试：用全新的临时数据目录启动真实服务（空库），逐页检查渲染、控制台与溢出。
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  // 本机可设 PW_CHANNEL=chrome 直接使用已安装的 Chrome；CI 用 `npx playwright install chromium`。
  use: { baseURL: "http://127.0.0.1:18599", channel: process.env.PW_CHANNEL || undefined },
  webServer: {
    command: "bash e2e/serve.sh",
    url: "http://127.0.0.1:18599/api/health",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
