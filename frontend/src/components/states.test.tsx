/* =========================================================================
   STATE / ERROR RENDERING TESTS
   =========================================================================
   Error copy is a place products leak. These assert that a customer gets a
   sentence they can act on, and that nothing internal — a stack trace, a SQL
   fragment, an enumerated code, an identifier — travels with it.
   ========================================================================= */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ApiError, NetworkError } from '@/api/client'
import { EmptyState, ErrorState, LoadingBlock, describeError } from './states'

describe('describeError', () => {
  it('distinguishes a dropped connection from a server failure', () => {
    const network = describeError(new NetworkError())
    expect(network.title).toMatch(/could not be reached/i)
    // Reassuring the customer that nothing changed is the point: a failed
    // request must not leave them wondering whether it half-happened.
    expect(network.body).toMatch(/nothing was changed/i)

    const server = describeError(new ApiError({ status: 500, message: 'boom' }))
    expect(server.title).not.toEqual(network.title)
    expect(server.body).toMatch(/unchanged/i)
  })

  it('describes a 404 without hinting the record exists elsewhere', () => {
    const { title, body } = describeError(
      new ApiError({ status: 404, message: 'Not found' }),
    )
    expect(title).toMatch(/not found/i)
    // The backend answers "not yours" and "not there" identically on purpose.
    expect(body.toLowerCase()).not.toMatch(/another (user|account)|belongs to|permission|forbidden/)
  })

  it('explains a throttle as pacing rather than as an error, with the wait', () => {
    const { title, body } = describeError(
      new ApiError({ status: 429, message: 'slow down', retryAfterSeconds: 30 }),
    )
    expect(title).toMatch(/pacing/i)
    expect(body).toContain('30')
  })

  it('turns a machine conflict detail into a readable sentence', () => {
    const { body } = describeError(
      new ApiError({
        status: 409,
        message: 'x',
        detail:
          'invalid_scenario_specification: contribution lever requests 9000 against available FHSA_ROOM of 8000',
      }),
    )
    // The machine prefix is for logs; the customer reads the sentence.
    expect(body).not.toMatch(/^invalid_scenario_specification/)
    expect(body).toMatch(/contribution lever requests 9000/i)
  })

  it('never surfaces a raw exception, stack trace or SQL', () => {
    const nasty = new Error(
      'Traceback (most recent call last):\n  File "/app/x.py", line 3\nsqlalchemy.exc.ProgrammingError: SELECT * FROM finance.income_source',
    )
    const { title, body } = describeError(nasty)
    for (const text of [title, body]) {
      expect(text).not.toMatch(/traceback|sqlalchemy|SELECT |\.py|File "/i)
    }
  })

  it('has a safe answer for something that is not an Error at all', () => {
    const { title, body } = describeError('a bare string')
    expect(title).toBeTruthy()
    expect(body).toBeTruthy()
  })
})

describe('ErrorState', () => {
  it('announces itself to assistive technology', () => {
    render(<ErrorState error={new NetworkError()} />)
    // Without role=alert a screen-reader user is left on a page that silently
    // changed to a failure.
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('offers a retry only when the caller can actually retry', () => {
    const { rerender } = render(<ErrorState error={new NetworkError()} />)
    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()

    rerender(<ErrorState error={new NetworkError()} onRetry={() => {}} />)
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })
})

describe('LoadingBlock', () => {
  it('announces that work is in progress rather than showing a silent skeleton', () => {
    render(<LoadingBlock label="Loading your position" />)
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent(/loading your position/i)
  })
})

describe('EmptyState', () => {
  it('explains the emptiness and can carry an action', () => {
    render(
      <EmptyState
        title="No analysis yet"
        body="Add your income and Onyx will work out where you stand."
        action={<button type="button">Add details</button>}
      />,
    )
    expect(screen.getByRole('heading', { name: /no analysis yet/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /add details/i })).toBeInTheDocument()
  })
})
