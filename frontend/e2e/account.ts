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

  return { email, headers }
}
