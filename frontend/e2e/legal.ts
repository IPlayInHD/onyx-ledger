/* =========================================================================
   CLEARING THE LEGAL GATE IN A BROWSER
   =========================================================================
   Shared because two specs need it and because the interaction is the part
   worth stating once: every outstanding document has its OWN checkbox and its
   OWN button, and the button stays disabled until the checkbox is ticked.

   That shape is deliberate on the page and this helper does not shortcut it —
   it ticks and clicks exactly as a person would, so a regression that
   pre-ticked a box or enabled a button early fails here rather than being
   papered over by a helper that clicked straight through.
   ========================================================================= */
import { expect, type Page } from '@playwright/test'

/** Block until the screen has the server's answer.
 *
 *  COUNTING BEFORE THIS IS A LIE. The page renders a loading block while it
 *  asks the backend what is outstanding, and during that moment there are zero
 *  Accept buttons — indistinguishable, to a naive count, from "everything is
 *  already accepted". A helper that read the count then would tick nothing,
 *  return happily, and leave the caller asserting against a gate it never
 *  cleared.
 *
 *  WAITS FOR A POSITIVE SIGNAL, and the first version of this did not — it
 *  waited for the loading text to be ABSENT, which is also true one moment
 *  earlier while the route's lazy chunk is still in flight and the component
 *  does not exist at all. It passed instantly and the caller then read a page
 *  that said "Loading the current documents…". Absence proves nothing here;
 *  something has to be present.
 *
 *  The footer, with its sign-out escape, renders exactly when the state has
 *  arrived. The alert covers the other terminal outcome — the read failed —
 *  so this returns on either, and never on the moment in between.
 */
export async function waitForLegalScreen(page: Page): Promise<void> {
  await expect(
    page
      .getByRole('button', { name: /^Sign out$/ })
      .or(page.getByRole('alert'))
      .first(),
    'the acceptance screen never finished loading',
  ).toBeVisible({ timeout: 30_000 })
}

/** Tick and accept every outstanding document on `/legal/accept`. */
export async function acceptOutstandingLegal(page: Page): Promise<void> {
  // Buttons are labelled `Accept <title>`; the count is whatever the SERVER
  // says is outstanding, never a number written here.
  for (;;) {
    await waitForLegalScreen(page)
    const buttons = page.getByRole('button', { name: /^Accept / })
    const remaining = await buttons.count()
    if (remaining === 0) break

    const button = buttons.first()
    const label = (await button.textContent())?.trim() ?? ''

    // The box first — the button is disabled until it is ticked, which is the
    // property that makes the record mean something.
    const checkbox = page.getByRole('checkbox').first()
    await expect(checkbox).not.toBeChecked()
    await checkbox.check()
    await expect(button).toBeEnabled()

    await button.click()
    await expect(
      page.getByRole('button', { name: label }),
      `${label} still offered after it was accepted`,
    ).toHaveCount(0, { timeout: 30_000 })
  }
}
