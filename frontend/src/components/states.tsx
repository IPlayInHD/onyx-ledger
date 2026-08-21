/* =========================================================================
   LOADING / EMPTY / ERROR STATES
   =========================================================================
   Every surface in this product has an intentional answer for "nothing yet",
   "still working", and "that failed" — because those are the states a customer
   actually meets on a slow morning, and a blank panel in a financial product
   reads as data loss.

   Error copy is HUMAN. The backend's machine code is preserved in the object
   for branching and support, but a stack trace, a SQL fragment or an internal
   identifier never reaches the screen.
   ========================================================================= */
import type { ReactNode } from 'react'
import { ApiError, NetworkError } from '@/api/client'

export function Skeleton({
  width = '100%',
  height = '1rem',
  className,
}: {
  width?: string
  height?: string
  className?: string
}) {
  return (
    <span
      className={`skeleton${className ? ` ${className}` : ''}`}
      style={{ display: 'block', width, height }}
      aria-hidden="true"
    />
  )
}

/** A loading region that ANNOUNCES itself. A silent skeleton leaves a screen
 *  reader with nothing at all while the page is working. */
export function LoadingBlock({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="stack stack-3" role="status" aria-live="polite">
      <span className="sr-only">{label}…</span>
      <Skeleton height="1.75rem" width="45%" />
      <Skeleton height="1rem" width="80%" />
      <Skeleton height="1rem" width="65%" />
    </div>
  )
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string
  body: string
  action?: ReactNode
}) {
  return (
    <div className="state-block">
      <h3 className="state-block__title">{title}</h3>
      <p className="state-block__body">{body}</p>
      {action}
    </div>
  )
}

/**
 * Turn any thrown value into something a person can act on.
 *
 * The mapping is deliberately conservative about 404: the backend answers
 * "not found" identically for a record that does not exist and one that
 * belongs to someone else, and the UI must not undo that by hinting the thing
 * exists somewhere.
 */
export function describeError(error: unknown): { title: string; body: string } {
  if (error instanceof NetworkError) {
    return {
      title: 'Onyx could not be reached',
      body: 'Your connection dropped or the service is unavailable. Nothing was changed. Try again in a moment.',
    }
  }
  if (error instanceof ApiError) {
    if (error.isNotFound) {
      return {
        title: 'Not found',
        body: 'This item could not be found. It may have been removed, or the link may be out of date.',
      }
    }
    if (error.isUnauthorized) {
      return {
        title: 'Your session has ended',
        body: 'Sign in again to continue. Nothing you saved has been lost.',
      }
    }
    if (error.isThrottled) {
      const wait = error.retryAfterSeconds
      return {
        title: 'Onyx is pacing this request',
        body: `This kind of work is limited so it stays fast for everyone.${
          wait ? ` Try again in about ${wait} seconds.` : ' Try again shortly.'
        }`,
      }
    }
    if (error.isConflict) {
      return {
        title: 'That could not be applied',
        body:
          error.detail && error.detail.length < 220
            ? humanReadableDetail(error.detail)
            : 'Something about this request conflicts with your current position. Review the details and try again.',
      }
    }
    if (error.isValidation) {
      return {
        title: 'Check the details',
        body: 'Some of the information supplied was not accepted. Correct the highlighted fields and try again.',
      }
    }
    if (error.status >= 500) {
      return {
        title: 'Onyx had a problem',
        body: 'Something went wrong on our side. Your data is unchanged. Please try again shortly.',
      }
    }
  }
  return {
    title: 'Something went wrong',
    body: 'The request could not be completed. Your data is unchanged.',
  }
}

/** Backend detail strings are enumerated machine phrases such as
 *  `invalid_scenario_specification: contribution lever requests …`. The prefix
 *  is for logs; the sentence after it is what a person should read. */
function humanReadableDetail(detail: string): string {
  const separator = detail.indexOf(': ')
  const body = separator > -1 ? detail.slice(separator + 2) : detail
  return body.charAt(0).toUpperCase() + body.slice(1)
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown
  onRetry?: () => void
}) {
  const { title, body } = describeError(error)
  return (
    <div className="state-block" role="alert">
      <h3 className="state-block__title">{title}</h3>
      <p className="state-block__body">{body}</p>
      {onRetry ? (
        <button type="button" className="btn btn--secondary" onClick={onRetry}>
          Try again
        </button>
      ) : null}
    </div>
  )
}

/** Panel-level async wrapper: one place that decides how the three states look
 *  so twelve screens do not each answer the question differently. */
export function AsyncBlock<T>({
  query,
  empty,
  children,
}: {
  query: { data: T | undefined; isPending: boolean; isError: boolean; error: unknown; refetch?: () => void }
  empty?: ReactNode
  children: (data: T) => ReactNode
}) {
  if (query.isPending) return <LoadingBlock />
  if (query.isError) return <ErrorState error={query.error} onRetry={query.refetch} />
  if (query.data === undefined) return <>{empty ?? null}</>
  return <>{children(query.data)}</>
}
