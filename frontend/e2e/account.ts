/* =========================================================================
   GETTING AN ACCOUNT PAST THE GATES
   =========================================================================
   Registration no longer produces an account that can use the product. B3 put
   email confirmation in front of it and B4 put the current legal documents in
   front of that, so a spec whose SUBJECT is something else — an accessibility
   sweep, a screenshot run — has two preconditions to satisfy before the screen
   it came to look at will render.

   WHY THIS EXISTS AS A SHARED HELPER. The a11y sweep seeded an account, never
   confirmed it, and never accepted anything. Its seeding calls asserted no
   status, so every one of them was refused with a 403 and the sweep carried on
   regardless: `waitForURL(/\/app/)` was satisfied by the instant before
   `RequireAuth` bounced the browser to `/verify-email`, and axe then scanned
   that same small page eight times under eight product-screen names. A green
   accessibility gate over zero product surfaces.

   So the rule here is the one that failure teaches: EVERY SEEDING CALL IS
   CHECKED. A precondition that fails silently is worse than one that fails,
   because the suite keeps reporting on a thing it never reached.
   ========================================================================= */
import { expect, type APIRequestContext } from '@playwright/test'
import { tokenFrom, waitForMessage } from './mailbox'

const API = process.env.ONYX_E2E_API ?? 'http://127.0.0.1:8099'

export interface SeededAccount {
  email: string
  headers: { authorization: string }
}

/** Register, confirm the address, accept what the registry requires, sign in.
 *
 *  The required set of documents comes from the SERVER on every run. A list
 *  written here would be a second registry — the precise thing B4 forbids the
 *  frontend from keeping — and it would stop covering a document the day one
 *  is added.
 */
export async function seedUsableAccount(
  api: APIRequestContext,
  email: string,
  password: string,
): Promise<SeededAccount> {
  const registered = await api.post(`${API}/api/v1/auth/register`, {
    data: { email, password },
  })
  expect(registered.status(), 'register').toBe(201)

  // Redeem the link REGISTRATION ALREADY SENT. Asking for another is refused
  // by the resend floor, and it is not what a customer does either.
  const confirmed = await api.post(`${API}/api/v1/auth/verification/confirm`, {
    data: { token: tokenFrom(await waitForMessage(email, 'EMAIL_VERIFICATION')) },
  })
  expect(confirmed.status(), 'confirm verification').toBe(200)

  const signedIn = await api.post(`${API}/api/v1/auth/login`, {
    data: { email, password },
  })
  expect(signedIn.status(), 'login').toBe(200)
  const headers = {
    authorization: `Bearer ${(await signedIn.json()).access_token}`,
  }

  await acceptOutstanding(api, headers)

  return { email, headers }
}

/** Read `/legal/state` for an account whose address was JUST confirmed.
 *
 *  TOLERATES A RECORDED SERVER DEFECT, and does not hide it. On a real ASGI
 *  server FastAPI can hand the client a response before the request's `yield`
 *  dependency has torn down — and this application commits its unit of work in
 *  exactly that teardown. So `POST /auth/verification/confirm` can answer 200
 *  while the transaction that marks the account verified has not landed, and
 *  the very next request reads `pending_verification` and is refused 403.
 *
 *  Measured on this host with a real uvicorn server and a 2 ms commit: the
 *  response arrived before the teardown in 12 of 40 requests behind
 *  BaseHTTPMiddleware, and 24 of 40 behind a pure-ASGI middleware — so it is
 *  not a middleware-style problem and the obvious fix does not fix it. It is
 *  recorded as an open, launch-blocking defect against the whole write surface
 *  rather than patched here, because a customer's browser hits the same window
 *  and the repair belongs in the request lifecycle, not in a test helper.
 *
 *  This is a BOUNDED wait, and it fails loudly rather than passing quietly: an
 *  account that never becomes readable is a real failure, not a slow one.
 */
export async function legalStateAfterVerification(
  api: APIRequestContext,
  headers: { authorization: string },
): Promise<{ documents: LegalDocumentState[] }> {
  let last = ''
  for (let attempt = 0; attempt < 25; attempt += 1) {
    const state = await api.get(`${API}/api/v1/legal/state`, { headers })
    if (state.status() === 200) return await state.json()
    last = await state.text()
    // Anything OTHER than the commit-visibility window is a real refusal and
    // must surface immediately rather than being waited out.
    if (!last.includes('email-verification-required')) break
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
  throw new Error(`legal state never became readable after verification: ${last}`)
}

export interface LegalDocumentState {
  document_type: string
  current_version: string
  acceptance_outstanding: boolean
}

/** Accept everything the SERVER says is outstanding. */
export async function acceptOutstanding(
  api: APIRequestContext,
  headers: { authorization: string },
): Promise<void> {
  const state = await legalStateAfterVerification(api, headers)
  for (const document of state.documents) {
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

