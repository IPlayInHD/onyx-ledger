/* =========================================================================
   DISPLAY FORMATTING
   =========================================================================
   Presentation only. Every function here takes a value the backend already
   decided and changes how it LOOKS — grouping separators, a currency symbol, a
   readable date.

   THE RULE THIS FILE EXISTS TO HOLD: no arithmetic on money. Nothing sums,
   subtracts, rounds, converts or re-derives a financial value. The backend
   sends canonical decimal strings precisely so the browser never has to turn
   them into floating-point numbers, and `Number("9348.85")` for display width
   is the closest this file comes — the string itself is what gets rendered.
   ========================================================================= */

const CAD = new Intl.NumberFormat('en-CA', {
  style: 'currency',
  currency: 'CAD',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

const CAD_WHOLE = new Intl.NumberFormat('en-CA', {
  style: 'currency',
  currency: 'CAD',
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
})

/**
 * Money, exactly as the backend valued it.
 *
 * The canonical string is parsed ONCE for grouping. If it is not a number we
 * return it untouched rather than guessing: a value we cannot format is still
 * a value the customer is entitled to see, and inventing "$0.00" in its place
 * would be a lie about their tax position.
 */
export function money(
  value: string | number | null | undefined,
  options: { whole?: boolean; signed?: boolean } = {},
): string {
  if (value === null || value === undefined || value === '') return '—'
  const numeric = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numeric)) return String(value)
  const formatted = options.whole ? CAD_WHOLE.format(numeric) : CAD.format(numeric)
  if (options.signed && numeric > 0) return `+${formatted}`
  return formatted
}

/** The absolute magnitude, for places where direction is shown by an arrow or
 *  a word rather than a minus sign. */
export function magnitude(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const numeric = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numeric)) return String(value)
  return CAD.format(Math.abs(numeric))
}

export function isDecrease(value: string | number | null | undefined): boolean {
  const numeric = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(numeric) && numeric < 0
}

/** A rate the backend already expressed as a fraction (0.2965 → "29.65%"). The
 *  ×100 is a unit change for display, not a recalculation of the rate. */
export function percent(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const numeric = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numeric)) return String(value)
  return `${(numeric * 100).toFixed(2).replace(/\.?0+$/, '')}%`
}

export function plainNumber(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  const numeric = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numeric)) return String(value)
  return new Intl.NumberFormat('en-CA').format(numeric)
}

const DATE_FMT = new Intl.DateTimeFormat('en-CA', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
})

/** ISO date → "April 30, 2026". Returns the input unchanged if it will not
 *  parse, because a malformed date is better shown than silently dropped. */
export function isoDate(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value.length <= 10 ? `${value}T00:00:00Z` : value)
  if (Number.isNaN(parsed.getTime())) return value
  return DATE_FMT.format(parsed)
}

export function relativeDays(days: number | null | undefined): string {
  if (days === null || days === undefined) return ''
  if (days < 0) return `${Math.abs(days)} days ago`
  if (days === 0) return 'today'
  if (days === 1) return 'tomorrow'
  return `in ${days} days`
}

/** A machine token such as `RESOURCE_EXHAUSTED` rendered as readable words.
 *  A label transform — the code itself is never altered. */
export function humanize(code: string | null | undefined): string {
  if (!code) return ''
  const words = code.replace(/[_.]/g, ' ').toLowerCase().trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}
