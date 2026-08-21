/* =========================================================================
   PUBLIC SURFACES — landing, trust, legal, 404
   =========================================================================
   These run against the real built bundle in a real browser, at both a desktop
   and a small-phone viewport. They check the promises a visitor can see before
   they ever hand Onyx a financial fact: honest positioning, reachable legal
   documents, a visible draft warning, and no forbidden marketing claim.
   ========================================================================= */
import { AxeBuilder } from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

/** Claims the product must never make. Checked as rendered text so it catches
 *  copy no matter which component introduced it. */
const FORBIDDEN_CLAIMS = [
  'guaranteed savings',
  'guarantee you',
  'never overpay',
  'cra approved',
  'cra-approved',
  'approved by the cra',
  'ai accountant',
  'unhackable',
  '100% secure',
  'military-grade',
  'bank-grade',
  'can never be breached',
  'maximise your refund',
  'maximize your refund',
]

async function expectNoForbiddenClaims(text: string) {
  const lower = text.toLowerCase()
  for (const claim of FORBIDDEN_CLAIMS) {
    expect(lower, `forbidden claim present: "${claim}"`).not.toContain(claim)
  }
}

test.describe('landing', () => {
  test('states the tax-assurance positioning, not refund chasing', async ({ page }) => {
    await page.goto('/')
    await expect(
      page.getByRole('heading', { name: /know where you stand before tax time/i }),
    ).toBeVisible()
    await expectNoForbiddenClaims((await page.textContent('body')) ?? '')
  })

  test('offers a way in and a way to read the terms first', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('link', { name: /sign in/i }).first()).toBeVisible()
    await expect(page.getByRole('link', { name: /trust/i }).first()).toBeVisible()
  })

  test('has no detectable accessibility violations', async ({ page }) => {
    await page.goto('/')
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()
    expect(
      results.violations.map((v) => `${v.id}: ${v.description}`),
      'axe violations',
    ).toEqual([])
  })

  test('does not scroll horizontally on a small phone', async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== 'mobile', 'mobile viewport only')
    await page.goto('/')
    const overflows = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    )
    expect(overflows, 'page body scrolls horizontally').toBe(false)
  })
})

test.describe('legal centre', () => {
  const DOCUMENTS = [
    'terms',
    'privacy',
    'ai-transparency',
    'tax-disclaimer',
    'accessibility',
    'security',
  ]

  test('lists every policy document', async ({ page }) => {
    await page.goto('/legal')
    for (const id of DOCUMENTS) {
      await expect(page.locator(`a[href="/legal/${id}"]`).first()).toBeVisible()
    }
  })

  for (const id of DOCUMENTS) {
    test(`${id} renders with version metadata and a draft warning`, async ({ page }) => {
      await page.goto(`/legal/${id}`)
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible()

      const body = (await page.textContent('body')) ?? ''
      // Every document is a draft that has not had Canadian legal review, and
      // it must say so rather than looking finished.
      expect(body.toLowerCase()).toContain('draft')
      expect(body.toLowerCase()).toMatch(/legal counsel|legal review/)
      expect(body).toContain('0.1.0-draft')
      await expectNoForbiddenClaims(body)
    })
  }

  test('AI transparency states that AI does not determine tax results', async ({ page }) => {
    await page.goto('/legal/ai-transparency')
    const body = ((await page.textContent('body')) ?? '').toLowerCase()
    expect(body).toContain('does not determine')
    // The disclosure must not invent a legal obligation that nobody confirmed.
    expect(body).not.toMatch(/law requires|legally required to disclose|required by law/)
  })

  test('tax disclaimer disclaims CRA affiliation and professional advice', async ({ page }) => {
    await page.goto('/legal/tax-disclaimer')
    const body = ((await page.textContent('body')) ?? '').toLowerCase()
    expect(body).toMatch(/not affiliated|not endorsed/)
    expect(body).toMatch(/not a substitute for/)
  })

  test('an unknown document does not crash', async ({ page }) => {
    await page.goto('/legal/does-not-exist')
    await expect(page.locator('body')).toContainText(/not found|could not find|no such/i)
  })
})

test.describe('trust centre', () => {
  test('explains the authority separation in plain language', async ({ page }) => {
    await page.goto('/trust')
    const body = ((await page.textContent('body')) ?? '').toLowerCase()
    expect(body).toMatch(/deterministic/)
    expect(body).toMatch(/does not (calculate|determine)/)
    // Freshness and integrity are different questions and the Trust Centre is
    // where the customer is taught the difference.
    expect(body).toMatch(/freshness/)
    expect(body).toMatch(/reproduc/)
    await expectNoForbiddenClaims(body)
  })

  test('has no detectable accessibility violations', async ({ page }) => {
    await page.goto('/trust')
    const results = await new AxeBuilder({ page })
      .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
      .analyze()
    expect(results.violations.map((v) => v.id)).toEqual([])
  })
})

test.describe('not found', () => {
  test('renders a calm 404 with a way back', async ({ page }) => {
    await page.goto('/no/such/route')
    await expect(page.locator('body')).toContainText(/not found|cannot find|does not exist/i)
    await expect(page.getByRole('link').first()).toBeVisible()
  })
})

test.describe('security posture of the delivered bundle', () => {
  test('ships no source maps and no obvious secret material', async ({ page, request }) => {
    await page.goto('/')
    const scripts = await page.evaluate(() =>
      Array.from(document.querySelectorAll('script[src]')).map(
        (s) => (s as HTMLScriptElement).src,
      ),
    )
    expect(scripts.length).toBeGreaterThan(0)

    for (const src of scripts) {
      const response = await request.get(src)
      expect(response.status()).toBe(200)
      const body = await response.text()
      // Source maps would republish readable internals to anyone with devtools.
      expect(body).not.toContain('//# sourceMappingURL')
      // A credential must never be compiled into a browser bundle.
      expect(body).not.toMatch(/sk-[A-Za-z0-9]{16,}/)
      expect(body).not.toMatch(/-----BEGIN [A-Z ]*PRIVATE KEY-----/)
    }
  })
})
