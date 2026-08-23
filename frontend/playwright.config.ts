import { defineConfig, devices } from '@playwright/test'

/* Browser E2E for the critical journeys.

   ONYX_E2E_CHROMIUM, when set, points Playwright at a Chromium that is already
   on the machine instead of the build it downloads for itself. UNSET BY
   DEFAULT, so CI — which runs `npx playwright install` and gets an exactly
   matching build — behaves as it always has.

   It exists for a sandbox that pre-provisions browsers: the image's Chromium
   is pinned to whatever Playwright version it was built against, and a
   lockfile bump leaves the two disagreeing about a build number. Playwright
   then refuses to launch and EVERY test fails in single-digit milliseconds,
   which reads exactly like a catastrophic application regression and is
   nothing of the kind. Naming the binary is cheaper than downloading 150MB
   into a container that already has one. */
const CHROMIUM = process.env.ONYX_E2E_CHROMIUM
const launch = CHROMIUM ? { executablePath: CHROMIUM } : {}

export default defineConfig({
  testDir: './e2e',
  /* Longer than Playwright's 30s default, on purpose. Authentication draws on
     a per-source-address budget, and a persona suite signing in from one host
     WILL be paced by it — that is the backend protecting itself, not a fault.
     A test that waits out a throttle needs room to do so; at 30s the retry
     logic could not survive even one pacing round and timed out instead. */
  timeout: 90_000,
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], launchOptions: launch } },
    // A real small-phone profile: mobile is a supported target, not an
    // afterthought, so the same journeys run at 393px.
    { name: 'mobile', use: { ...devices['Pixel 7'], launchOptions: launch } },
  ],
  webServer: {
    // --host 127.0.0.1 IS LOAD-BEARING. `vite preview` binds `localhost`,
    // which on a CI runner resolves to ::1 first, while the readiness probe
    // below polls 127.0.0.1. The server came up healthy on IPv6 and the IPv4
    // probe never saw it, so the whole suite failed with a bare
    // "Timed out waiting 120000ms from config.webServer" — a message that
    // names the timeout and not one word about why.
    command: 'npm run preview -- --port 4173 --strictPort --host 127.0.0.1',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    // Surface the preview server's own output. Without this its stdout and
    // stderr are discarded, which is exactly why the failure above said
    // nothing useful.
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
