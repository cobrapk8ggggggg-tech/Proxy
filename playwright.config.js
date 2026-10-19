const { defineConfig, devices } = require("@playwright/test");
module.exports = defineConfig({
  testDir: "tests/browser",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 45000,
  expect: { timeout: 10000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:4071",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1050 },
      },
    },
    {
      name: "iphone-webkit",
      use: { ...devices["iPhone 13"], browserName: "webkit" },
    },
  ],
  webServer: {
    command:
      (process.env.CI ? "python" : "./.venv/bin/python") +
      " tests/browser_server.py",
    url: "http://127.0.0.1:4071/health",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
