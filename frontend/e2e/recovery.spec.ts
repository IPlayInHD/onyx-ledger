/* =========================================================================
   VERIFICATION AND PASSWORD RECOVERY  —  in a real browser
   =========================================================================
   These drive the flows the way a customer meets them: register, get bounced
   to a screen that says why, open a link out of an actual message, choose a
   password, sign in with it.

   Three things here are NOT UX assertions and are the reason the file exists:

     · the reset request answers identically for a real address and an
       invented one, IN THE RENDERED PAGE — the backend's careful wording is
       worth nothing if the frontend renders one branch differently
     · the token never survives in the address bar, because a URL is copied,
       bookmarked, and screenshotted into support tickets
     · a completed reset does not sign anybody in, which is the whole point of
       a flow whose premise is that somebody else may have had access
   ========================================================================= */
import { expect, test, type APIRequestContext } from '@playwright/test'
import { messagesFor, tokenFrom, waitForMessage } from './mailbox'

const API = process.env.ONYX_E2E_API ?? 'http://127.0.0.1:8099'
const PASSWORD = 'supersecret1'
const NEW_PASSWORD = 'a-completely-different-one-9'

function freshEmail(tag: string): string {
  return `e2e_${tag}_${Date.now()}_${Math.floor(Math.random() * 1e6)}@test.ca`
}

/** Register through the API. Faster and less brittle than driving the form for
 *  a step that is a precondition rather than the subject. */
async function registerViaApi(api: APIRequestContext, email: string): Promise<void> {
  const created = await api.post(`${API}/api/v1/auth/register`, {
    data: { email, password: PASSWORD },
  })
  expect(created.status(), 'register').toBe(201)
}

async function verifyViaApi(api: APIRequestContext, email: string): Promise<void> {
  const loggedIn = await api.post(`${API}/api/v1/auth/login`, {
    data: { email, password: PASSWORD },
  })
  expect(loggedIn.status(), 'login').toBe(200)
  const { access_token } = (await loggedIn.json()) as { access_token: string }
  const asked = await api.post(`${API}/api/v1/auth/verification`, {
    headers: { authorization: `Bearer ${access_token}` },
  })
  expect(asked.status(), 'ask for link').toBe(202)
  const confirmed = await api.post(`${API}/api/v1/auth/verification/confirm`, {
    data: { token: tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION')) },
  })
  expect(confirmed.status(), 'confirm').toBe(200)
}

test.describe('email verification', () => {
  test('an unverified account is told what to do, not shown a login failure', async ({
    page,
    request,
  }) => {
    const email = freshEmail('unverified')
    await registerViaApi(request, email)

    await page.goto('/sign-in')
    await page.getByLabel(/email/i).fill(email)
    await page.getByLabel(/password/i).fill(PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()

    // The credentials WERE accepted. The customer is not sent back to the form
    // with a generic failure; they land somewhere that names the one thing
    // standing between them and the product.
    await expect(page).toHaveURL(/\/verify-email/, { timeout: 30_000 })
    const body = (await page.locator('body').innerText()).toLowerCase()
    expect(body).toContain('confirm')
    expect(body).not.toMatch(/do not match|incorrect|suspended|contact support/)
  })

  test('opening the link confirms the address and opens the product', async ({
    page,
    request,
  }) => {
    const email = freshEmail('verify')
    await registerViaApi(request, email)

    await page.goto('/sign-in')
    await page.getByLabel(/email/i).fill(email)
    await page.getByLabel(/password/i).fill(PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page).toHaveURL(/\/verify-email/, { timeout: 30_000 })

    const token = tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION'))
    await page.goto(`/verify-email?token=${token}`)

    await expect(page).toHaveURL(/\/app/, { timeout: 30_000 })
    // The token is out of the address bar before anything else happens.
    expect(page.url()).not.toContain('token=')
  })

  test('a spent link says so without saying why', async ({ page, request }) => {
    const email = freshEmail('spent')
    await registerViaApi(request, email)
    await verifyViaApi(request, email)

    const token = tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION'))
    await page.goto(`/verify-email?token=${token}`)

    await expect(page.getByRole('alert')).toBeVisible({ timeout: 30_000 })
    const body = (await page.locator('body').innerText()).toLowerCase()
    expect(body).toContain('no longer valid')
    // "Already used" and "expired" would each tell the holder of a guessed
    // token that they guessed a real one.
    expect(body).not.toMatch(/already used|expired on|belongs to/)
    expect(await page.locator('body').innerText()).not.toContain(token)
  })
})

test.describe('password recovery', () => {
  test('the request page answers a real address and an invented one identically', async ({
    page,
    request,
  }) => {
    const known = freshEmail('known')
    await registerViaApi(request, known)
    await verifyViaApi(request, known)
    const unknown = freshEmail('nobody')

    const submit = async (address: string): Promise<string> => {
      await page.goto('/forgot-password')
      await page.getByLabel(/email/i).fill(address)
      await page.getByRole('button', { name: /send reset link/i }).click()
      await expect(page.getByRole('heading', { name: /check your inbox/i })).toBeVisible({
        timeout: 30_000,
      })
      return (await page.locator('main').innerText()).toLowerCase().replaceAll(address, '')
    }

    const forKnown = await submit(known)
    const forUnknown = await submit(unknown)

    // Byte for byte, once the typed address itself is removed.
    expect(forKnown).toBe(forUnknown)

    // Non-vacuous: one of the two actually produced a message.
    await waitForMessage(known, 'PASSWORD_RESET')
    expect(messagesFor(unknown)).toHaveLength(0)
  })

  test('a customer resets their password and signs in with the new one', async ({
    page,
    request,
  }) => {
    const email = freshEmail('reset')
    await registerViaApi(request, email)
    await verifyViaApi(request, email)

    await page.goto('/sign-in')
    await page.getByRole('link', { name: /forgot your password/i }).click()
    await expect(page).toHaveURL(/\/forgot-password/)

    await page.getByLabel(/email/i).fill(email)
    await page.getByRole('button', { name: /send reset link/i }).click()
    await expect(page.getByRole('heading', { name: /check your inbox/i })).toBeVisible({
      timeout: 30_000,
    })

    const token = tokenFrom(await waitForMessage(email, 'PASSWORD_RESET'))
    await page.goto(`/reset-password?token=${token}`)
    await expect(page.getByRole('heading', { name: /choose a new password/i })).toBeVisible()

    // Gone from the URL before a character is typed into the form.
    expect(page.url()).not.toContain('token=')

    await page.getByLabel(/^new password$/i).fill(NEW_PASSWORD)
    await page.getByLabel(/confirm new password/i).fill(NEW_PASSWORD)
    await page.getByRole('button', { name: /change password/i }).click()

    await expect(page.getByRole('heading', { name: /password is changed/i })).toBeVisible({
      timeout: 30_000,
    })

    // NOT SIGNED IN. A reset hands out no session — the premise is that
    // somebody else may have had access to the account.
    await page.goto('/app')
    await expect(page).toHaveURL(/\/sign-in/, { timeout: 30_000 })

    await page.getByLabel(/email/i).fill(email)
    await page.getByLabel(/password/i).fill(NEW_PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page).toHaveURL(/\/app/, { timeout: 30_000 })

    // And the customer was told, in a message with no link in it.
    const notice = await waitForMessage(email, 'PASSWORD_CHANGED')
    expect(notice.text).not.toContain('token=')
    expect(notice.text).not.toContain(NEW_PASSWORD)
  })

  test('a mistyped confirmation is caught before the link is spent', async ({
    page,
    request,
  }) => {
    const email = freshEmail('mistyped')
    await registerViaApi(request, email)
    await verifyViaApi(request, email)

    const asked = await request.post(`${API}/api/v1/auth/password-reset`, { data: { email } })
    expect(asked.status()).toBe(202)
    const token = tokenFrom(await waitForMessage(email, 'PASSWORD_RESET'))

    await page.goto(`/reset-password?token=${token}`)
    await page.getByLabel(/^new password$/i).fill(NEW_PASSWORD)
    await page.getByLabel(/confirm new password/i).fill(`${NEW_PASSWORD}-typo`)
    await page.getByRole('button', { name: /change password/i }).click()
    await expect(page.getByRole('alert')).toBeVisible()

    // The link is UNSPENT: the mismatch was caught in the browser, so the
    // customer can correct it rather than start over.
    await page.getByLabel(/confirm new password/i).fill(NEW_PASSWORD)
    await page.getByRole('button', { name: /change password/i }).click()
    await expect(page.getByRole('heading', { name: /password is changed/i })).toBeVisible({
      timeout: 30_000,
    })
  })

  test('the reset page without a link does not pretend to be a form', async ({ page }) => {
    await page.goto('/reset-password')
    await expect(page.getByRole('heading', { name: /needs a reset link/i })).toBeVisible()
    expect(await page.locator('input[type="password"]').count()).toBe(0)
  })
})
