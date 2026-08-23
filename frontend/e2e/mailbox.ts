/* =========================================================================
   READING WHAT THE BACKEND WOULD HAVE SENT
   =========================================================================
   The E2E backend runs with `ONYX_EMAIL_CAPTURE_DIR` set, so every
   transactional message lands as a JSON file both processes can see. This is
   how a test opens a verification or reset link — the same way a customer
   does, by reading the message rather than by being handed a token.

   WHY NOT SET THE STATUS IN THE DATABASE. It would be two lines and it would
   mean the browser suite never exercises the transition that every signed-in
   journey now depends on. The mechanism has to be the real one or the coverage
   is imaginary.

   SCOPED BY RECIPIENT, ALWAYS. Addresses are unique per test; picking "the
   newest file" would make a test depend on whatever ran beside it, and this
   suite runs fully parallel.
   ========================================================================= */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { expect } from '@playwright/test'

const CAPTURE_DIR = process.env.ONYX_E2E_CAPTURE_DIR ?? '/tmp/onyx_e2e_mail'

export type MailKind = 'EMAIL_VERIFICATION' | 'PASSWORD_RESET' | 'PASSWORD_CHANGED'

export interface CapturedMessage {
  to: string
  kind: MailKind
  subject: string
  text: string
  html: string
}

function readAll(): CapturedMessage[] {
  let names: string[]
  try {
    names = readdirSync(CAPTURE_DIR)
  } catch {
    return []
  }
  const found: CapturedMessage[] = []
  for (const name of names) {
    if (!name.endsWith('.json')) continue
    try {
      found.push(JSON.parse(readFileSync(join(CAPTURE_DIR, name), 'utf8')) as CapturedMessage)
    } catch {
      /* A file caught mid-write. The poll below will see it next time. */
    }
  }
  return found
}

export function messagesFor(to: string, kind?: MailKind): CapturedMessage[] {
  return readAll().filter((m) => m.to === to && (!kind || m.kind === kind))
}

/** Wait for one message to arrive.
 *
 *  POLLING IS REQUIRED, not laziness: the backend schedules every send as a
 *  background task that runs AFTER the response, precisely so a reset request
 *  cannot be timed to reveal whether an address has an account. The 202 comes
 *  back before the file exists.
 */
export async function waitForMessage(
  to: string,
  kind: MailKind,
  { timeoutMs = 15_000 }: { timeoutMs?: number } = {},
): Promise<CapturedMessage> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const found = messagesFor(to, kind)
    const newest = found.at(-1)
    if (newest) return newest
    if (Date.now() > deadline) {
      throw new Error(
        `no ${kind} message for ${to} within ${timeoutMs}ms. ` +
          `Capture dir: ${CAPTURE_DIR}`,
      )
    }
    await new Promise((resolve) => setTimeout(resolve, 200))
  }
}

/** The token out of a link. Matches the query parameter, so restyling a
 *  template cannot break the suite. */
export function tokenFrom(message: CapturedMessage): string {
  const token = /[?&]token=([A-Za-z0-9_-]+)/.exec(message.text)?.[1]
  expect(token, `no ?token= in the ${message.kind} message`).toBeTruthy()
  return token as string
}
