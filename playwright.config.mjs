import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests_browser",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: "line",
  use: {
    headless: true,
    trace: "retain-on-failure",
  },
});
