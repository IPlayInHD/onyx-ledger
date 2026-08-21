/* =========================================================================
   APPEARANCE PREFERENCE
   =========================================================================
   One implementation, used by BOTH the settings screen and application
   startup. It lived only inside the settings screen once, which meant the
   preference was applied only while that screen was mounted: a customer chose
   Dark, reloaded, and got Light back until they returned to Settings. A
   preference that does not survive a reload is not a preference.
   ========================================================================= */

export type ThemePreference = 'system' | 'light' | 'dark'

export const THEME_STORAGE_KEY = 'onyx.theme'

/* Storage can throw outright — private windows, blocked site data, a locked
   down browser — so every access is guarded. A display preference is never
   worth breaking a page over, and it is deliberately the only thing this
   product keeps in the browser besides the session. */
export function readStoredTheme(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  } catch {
    /* fall through to the system default */
  }
  return 'system'
}

export function storeTheme(preference: ThemePreference): void {
  try {
    if (preference === 'system') {
      window.localStorage.removeItem(THEME_STORAGE_KEY)
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference)
    }
  } catch {
    /* The choice still applies to this page; it just will not be remembered. */
  }
}

/**
 * Put the preference on the document.
 *
 * `system` REMOVES the attribute rather than writing a value, because the
 * stylesheet treats an absent attribute as "follow prefers-color-scheme".
 * Writing `data-theme="system"` would match neither branch and silently pin
 * the light palette.
 */
export function applyTheme(preference: ThemePreference): void {
  const root = document.documentElement
  if (!root) return
  if (preference === 'system') {
    root.removeAttribute('data-theme')
  } else {
    root.dataset.theme = preference
  }
}

/** Called once at startup, before the first render, so a stored choice is in
 *  effect for the first paint instead of arriving a screen later. */
export function applyStoredTheme(): void {
  applyTheme(readStoredTheme())
}
