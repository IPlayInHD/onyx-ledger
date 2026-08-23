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
import { type } from './form'
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

/** Redeem the link REGISTRATION ALREADY SENT.
 *
 *  Asking for another would be refused by the resend floor, and it is not what
 *  a customer does either — they open the message that arrived.
 */
async function verifyViaApi(api: APIRequestContext, email: string): Promise<void> {
  const confirmed = await api.post(`${API}/api/v1/auth/verification/confirm`, {
    data: { token: tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION')) },
  })
  expect(confirmed.status(), 'confirm').toBe(200)
}

/** Clear the legal gate through the API.
 *
 *  A PRECONDITION HERE, NOT A SUBJECT. B4 puts an acceptance gate after
 *  verification, so an account that has not accepted lands on `/legal/accept`
 *  rather than in the product — correct behaviour, and proved by
 *  `legal.spec.ts`. Recovery tests that care about "the new password works"
 *  clear it first, so they keep asserting the thing they are actually about.
 *
 *  The required set comes from the SERVER. A list written here would be a
 *  second registry, and it would stop covering a document the day one is
 *  added.
 */
async function acceptLegalViaApi(api: APIRequestContext, email: string): Promise<void> {
  const signedIn = await api.post(`${API}/api/v1/auth/login`, {
    data: { email, password: PASSWORD },
  })
  expect(signedIn.status(), 'login').toBe(200)
  const headers = { authorization: `Bearer ${(await signedIn.json()).access_token}` }

  const state = await api.get(`${API}/api/v1/legal/state`, { headers })
  expect(state.status(), `legal state: ${await state.text()}`).toBe(200)
  for (const document of (await state.json()).documents) {
    if (!document.acceptance_outstanding) continue
    const accepted = await api.post(`${API}/api/v1/legal/acceptances`, {
      headers,
      data: {
        document_type: document.document_type,
        document_version: document.current_version,
      },
    })
    expect(accepted.status(), `accept ${document.document_type}`).toBe(200)
  }
}

test.describe('email verification', () => {
  test('an unverified account is told what to do, not shown a login failure', async ({
    page,
    request,
  }) => {
    const email = freshEmail('unverified')
    await registerViaApi(request, email)

    await page.goto('/sign-in')
    await type(page, /email/i, email)
    await type(page, /password/i, PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()

    // The credentials WERE accepted. The customer is not sent back to the form
    // with a generic failure; they land somewhere that names the one thing
    // standing between them and the product.
    await expect(page).toHaveURL(/\/verify-email/, { timeout: 30_000 })
    const body = (await page.locator('body').innerText()).toLowerCase()
    expect(body).toContain('confirm')
    expect(body).not.toMatch(/do not match|incorrect|suspended|contact support/)
  })

  test('opening the link confirms the address and moves the customer on', async ({
    page,
    request,
  }) => {
    const email = freshEmail('verify')
    await registerViaApi(request, email)

    await page.goto('/sign-in')
    await type(page, /email/i, email)
    await type(page, /password/i, PASSWORD)
    await page.getByRole('button', { name: /sign in/i }).click()
    await expect(page).toHaveURL(/\/verify-email/, { timeout: 30_000 })

    const token = tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION'))
    await page.goto(`/verify-email?token=${token}`)

    // NOT `/app`, and not this page either. Confirming the address clears the
    // FIRST gate; B4 puts the current Terms and Privacy Policy behind the
    // second one. What matters here is that the customer is carried onward to
    // something they can act on rather than left sitting on the screen that
    // just finished its job — which is exactly what happened when this page
    // recognised only `authenticated` as "holds a session", and told a
    // customer who had confirmed in this very tab that they had used a
    // different browser.
    await expect(page).toHaveURL(/\/legal\/accept/, { timeout: 30_000 })
    // The token is out of the address bar before anything else happens.
    expect(page.url()).not.toContain('token=')
  })

  test('registering sends the link the screen promises', async ({ request }) => {
    /* The verification screen says "we sent a link to ...". It said that over a
       link nobody had sent, because the first send was left to the resend
       endpoint — caught by this suite waiting fifteen seconds for a message
       that was never coming. Registration sends it now, and this is the
       assertion that keeps it that way. */
    const email = freshEmail('firstsend')
    await registerViaApi(request, email)
    const message = await waitForMessage(email, 'EMAIL_VERIFICATION')
    expect(message.subject.toLowerCase()).toContain('confirm')
    expect(tokenFrom(message).length).toBeGreaterThan(20)
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
      await type(page, /email/i, address)
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
    await acceptLegalViaApi(request, email)

    await page.goto('/sign-in')
    await page.getByRole('link', { name: /forgot your password/i }).click()
    await expect(page).toHaveURL(/\/forgot-password/)

    await type(page, /email/i, email)
    await page.getByRole('button', { name: /send reset link/i }).click()
    await expect(page.getByRole('heading', { name: /check your inbox/i })).toBeVisible({
      timeout: 30_000,
    })

    const token = tokenFrom(await waitForMessage(email, 'PASSWORD_RESET'))
    await page.goto(`/reset-password?token=${token}`)
    await expect(page.getByRole('heading', { name: /choose a new password/i })).toBeVisible()

    // Gone from the URL before a character is typed into the form.
    expect(page.url()).not.toContain('token=')

    await type(page, /^new password$/i, NEW_PASSWORD)
    await type(page, /confirm new password/i, NEW_PASSWORD)
    await page.getByRole('button', { name: /change password/i }).click()

    await expect(page.getByRole('heading', { name: /password is changed/i })).toBeVisible({
      timeout: 30_000,
    })

    // NOT SIGNED IN. A reset hands out no session — the premise is that
    // somebody else may have had access to the account.
    await page.goto('/app')
    await expect(page).toHaveURL(/\/sign-in/, { timeout: 30_000 })

    await type(page, /email/i, email)
    await type(page, /password/i, NEW_PASSWORD)
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
    await type(page, /^new password$/i, NEW_PASSWORD)
    await type(page, /confirm new password/i, `${NEW_PASSWORD}-typo`)
    await page.getByRole('button', { name: /change password/i }).click()
    await expect(page.getByRole('alert')).toBeVisible()

    // The link is UNSPENT: the mismatch was caught in the browser, so the
    // customer can correct it rather than start over.
    await type(page, /confirm new password/i, NEW_PASSWORD)
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
