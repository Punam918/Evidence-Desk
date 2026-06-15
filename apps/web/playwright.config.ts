import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "../../tests/ui",
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5178",
    browserName: "chromium",
    channel:
      process.env.ED_TEST_BROWSER_CHANNEL === "chrome" ? "chrome" : undefined,
    trace: "retain-on-failure",
  },
  webServer: {
    command: "../../scripts/pnpm dev --port 5178",
    url: "http://127.0.0.1:5178",
    reuseExistingServer: false,
  },
  reporter: [
    ["list"],
    [
      "json",
      { outputFile: "../../docs/10-testing/frontend-shell-results.json" },
    ],
  ],
  outputDir: "../../.cache/playwright-results",
});
