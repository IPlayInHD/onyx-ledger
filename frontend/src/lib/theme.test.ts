/* =========================================================================
   THE APPEARANCE PREFERENCE MUST SURVIVE A RELOAD
   =========================================================================
   It did not. Applying the stored choice lived inside the settings screen's
   effect, so the palette was only correct while that screen was mounted: pick
   Dark, reload, get Light — until you wandered back into Settings. The whole
   point of a stored preference is the reload.
   ========================================================================= */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  THEME_STORAGE_KEY,
  applyStoredTheme,
  applyTheme,
  readStoredTheme,
  storeTheme,
} from './theme'

beforeEach(() => {
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  document.documentElement.removeAttribute('data-theme')
})

describe('startup', () => {
  it('applies a stored dark preference before anything renders', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark')
    applyStoredTheme()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('applies a stored light preference even when the system prefers dark', () => {
    // An explicit choice has to win in BOTH directions, or "Light" is just a
    // slower way of saying "match my system".
    localStorage.setItem(THEME_STORAGE_KEY, 'light')
    applyStoredTheme()
    expect(document.documentElement.dataset.theme).toBe('light')
  })

  it('leaves the attribute absent when the choice is to follow the system', () => {
    // The stylesheet reads an ABSENT attribute as "follow prefers-color-scheme".
    // Writing data-theme="system" would match neither branch and quietly pin
    // the light palette for someone whose device is dark.
    document.documentElement.dataset.theme = 'dark'
    localStorage.setItem(THEME_STORAGE_KEY, 'system')
    applyStoredTheme()
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })

  it('follows the system when nothing has been stored', () => {
    applyStoredTheme()
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })

  it('ignores a stored value that is not a theme', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'chartreuse')
    expect(readStoredTheme()).toBe('system')
    applyStoredTheme()
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })
})

describe('storage that refuses to work', () => {
  it('does not break the page when reading storage throws', () => {
    // Private windows and locked-down browsers throw on access rather than
    // returning null. A display preference must never take the app down.
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => applyStoredTheme()).not.toThrow()
    expect(readStoredTheme()).toBe('system')
  })

  it('does not break the page when writing storage throws', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('quota')
    })
    expect(() => storeTheme('dark')).not.toThrow()
  })
})

describe('round trip', () => {
  it('stores a choice and applies it on the next startup', () => {
    storeTheme('dark')
    document.documentElement.removeAttribute('data-theme') // simulate a reload
    applyStoredTheme()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('clears the stored choice when returning to system', () => {
    storeTheme('dark')
    storeTheme('system')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull()
    applyTheme('system')
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
  })
})
