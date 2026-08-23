/* =========================================================================
   FILLING A FORM THE WAY A PERSON DOES
   =========================================================================
   One helper, shared, because the failure it defends against is a property of
   the app shell rather than of any one spec.

   `fill()` writes the DOM value and dispatches `input`; React turns that into
   component state asynchronously. Every page in this bundle sits behind a
   single lazy `Suspense` boundary, so a fill can land in the window where the
   route's chunk has produced an input but React has not yet committed the
   component that owns it — and that first commit puts the controlled value
   back to the empty string it started at. Nothing is queued and nothing is
   retried by the browser: the typed value simply never became state.

   WAITING DOES NOT FIX IT, which is why this retries the fill rather than
   extending a timeout. Measured on CI-class hardware: roughly once per
   four-worker run, always on a sign-in or recovery form, never in an isolated
   repeat.

   The final assertion is deliberately unchanged. A field that genuinely
   refuses input still fails the test; only the discarded-first-fill case
   recovers.
   ========================================================================= */
import { expect, type Locator, type Page } from '@playwright/test'

/** Type into a field and WAIT FOR REACT TO HAVE ACCEPTED IT. */
export async function type(
  page: Page,
  label: RegExp | string,
  value: string,
): Promise<void> {
  await fillField(page.getByLabel(label), value)
}

/** Same contract, for a locator the caller already has. */
export async function fillField(field: Locator, value: string): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await field.fill(value)
    try {
      await expect(field).toHaveValue(value, { timeout: 2_000 })
      return
    } catch {
      /* React discarded it. Type it again rather than wait for a commit that
         is not coming. */
    }
  }
  await field.fill(value)
  await expect(field).toHaveValue(value)
}
