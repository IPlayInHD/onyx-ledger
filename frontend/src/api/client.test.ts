/* =========================================================================
   API CLIENT TESTS
   =========================================================================
   The security-relevant behaviour of the one network layer: where credentials
   live, what is retried, and how a refusal is interpreted. A regression in any
   of these is a security regression, not a UX one.
   ========================================================================= */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, NetworkError, auth, pauseFor, request, shouldRetry } from './client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

beforeEach(() => {
  auth.clear()
  vi.restoreAllMocks()
})

afterEach(() => {
  auth.clear()
})

describe('credential storage', () => {
  it('never writes the access token to any web storage', () => {
    auth.setAccessToken('secret-access-token')
    // The access token is the credential that authorises every request. It
    // lives in memory only, so injected script cannot lift a long-lived
    // credential out of storage at rest.
    expect(JSON.stringify(sessionStorage)).not.toContain('secret-access-token')
    expect(JSON.stringify(localStorage)).not.toContain('secret-access-token')
    expect(auth.getAccessToken()).toBe('secret-access-token')
  })

  it('keeps the refresh token in sessionStorage, not localStorage', () => {
    // sessionStorage bounds exposure to one tab rather than persisting across
    // sessions. httpOnly cookies would be better and are a backend change.
    auth.setRefreshToken('refresh-token-value')
    expect(sessionStorage.getItem('onyx.rt')).toBe('refresh-token-value')
    expect(localStorage.getItem('onyx.rt')).toBeNull()
  })

  it('clears both credentials together', () => {
    auth.setAccessToken('a')
    auth.setRefreshToken('r')
    auth.clear()
    expect(auth.getAccessToken()).toBeNull()
    expect(auth.getRefreshToken()).toBeNull()
  })
})

describe('request', () => {
  it('sends the bearer token on authenticated calls', async () => {
    auth.setAccessToken('token-123')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await request('/users/me')

    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.headers.authorization).toBe('Bearer token-123')
  })

  it('omits the bearer token on anonymous calls', async () => {
    auth.setAccessToken('token-123')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await request('/auth/login', { method: 'POST', body: {}, anonymous: true })

    const init = fetchMock.mock.calls[0]?.[1]
    expect(init?.headers.authorization).toBeUndefined()
  })

  it('never sends ambient cookies, so cross-site requests carry no authority', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await request('/users/me')

    // With credentials omitted and auth carried in an explicit header, a
    // cross-site form post cannot act as the customer — CSRF has no ambient
    // authority to borrow.
    expect(fetchMock.mock.calls[0]?.[1]?.credentials).toBe('omit')
  })

  it('refuses to follow redirects', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await request('/users/me')

    expect(fetchMock.mock.calls[0]?.[1]?.redirect).toBe('error')
  })

  it('maps a backend refusal onto ApiError with its machine code preserved', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: 'unsupported_jurisdiction: province YT' }, 422),
      ),
    )

    await expect(request('/analysis', { method: 'POST', body: {} })).rejects.toMatchObject({
      status: 422,
      detail: 'unsupported_jurisdiction: province YT',
    })
  })

  it('treats a 404 as non-enumerating', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'Not found' }, 404)))

    try {
      await request('/ioe/scenarios/whatever')
      expect.unreachable('should have thrown')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      // "not yours" and "not there" are deliberately the same answer; the
      // client must not add a distinction the backend withheld.
      expect((error as ApiError).isNotFound).toBe(true)
    }
  })

  it('surfaces a transport failure as NetworkError, distinct from a server error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('failed to fetch')))
    await expect(request('/users/me')).rejects.toBeInstanceOf(NetworkError)
  })

  it('does not swallow an abort', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new DOMException('aborted', 'AbortError')),
    )
    await expect(request('/users/me')).rejects.toBeInstanceOf(DOMException)
  })

  it('returns undefined for an empty 204 rather than failing to parse', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })))
    await expect(request('/auth/logout', { method: 'POST' })).resolves.toBeUndefined()
  })
})

describe('session refresh', () => {
  it('exchanges the refresh token once and replays the original request', async () => {
    auth.setAccessToken('expired')
    auth.setRefreshToken('refresh-1')

    const fetchMock = vi
      .fn()
      // 1: original call rejected
      .mockResolvedValueOnce(jsonResponse({ detail: 'expired' }, 401))
      // 2: refresh succeeds and ROTATES the refresh token
      .mockResolvedValueOnce(
        jsonResponse({ access_token: 'fresh', refresh_token: 'refresh-2' }),
      )
      // 3: replayed original call
      .mockResolvedValueOnce(jsonResponse({ id: 'user-1' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(request('/users/me')).resolves.toEqual({ id: 'user-1' })

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(auth.getAccessToken()).toBe('fresh')
    // Storing the rotated token matters: the backend single-uses refresh
    // tokens and treats reuse as an attack on the whole session family.
    expect(auth.getRefreshToken()).toBe('refresh-2')
  })

  it('clears the session when the refresh itself is rejected', async () => {
    auth.setAccessToken('expired')
    auth.setRefreshToken('stale')

    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ detail: 'expired' }, 401))
        .mockResolvedValueOnce(jsonResponse({ detail: 'revoked' }, 401)),
    )

    await expect(request('/users/me')).rejects.toBeInstanceOf(ApiError)
    expect(auth.getAccessToken()).toBeNull()
    expect(auth.getRefreshToken()).toBeNull()
  })

  it('keeps the session when the refresh is THROTTLED rather than rejected', async () => {
    // The regression this pins: /auth/refresh shares the platform's auth
    // budget, so reloading during a burst answers 429. Treating that as a
    // revoked session destroyed a perfectly good credential and signed the
    // customer out because of their own traffic — found by an E2E persona run.
    auth.setAccessToken('expired')
    auth.setRefreshToken('still-valid')

    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ detail: 'expired' }, 401))
        // both refresh attempts are paced
        .mockResolvedValue(jsonResponse({ detail: 'slow down' }, 429)),
    )

    await expect(request('/users/me')).rejects.toBeInstanceOf(ApiError)
    expect(auth.getRefreshToken()).toBe('still-valid')
  })

  it('keeps the session when the refresh hits a server error', async () => {
    auth.setAccessToken('expired')
    auth.setRefreshToken('still-valid')

    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ detail: 'expired' }, 401))
        .mockResolvedValue(jsonResponse({ detail: 'boom' }, 503)),
    )

    await expect(request('/users/me')).rejects.toBeInstanceOf(ApiError)
    expect(auth.getRefreshToken()).toBe('still-valid')
  })

  it('still ends the session when the refresh token is genuinely refused', async () => {
    // The negative control: the fix must not make an invalid credential
    // immortal. A 401 on refresh is a real verdict and ends the session.
    auth.setAccessToken('expired')
    auth.setRefreshToken('revoked')

    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValueOnce(jsonResponse({ detail: 'expired' }, 401))
        .mockResolvedValueOnce(jsonResponse({ detail: 'revoked' }, 401)),
    )

    await expect(request('/users/me')).rejects.toBeInstanceOf(ApiError)
    expect(auth.getRefreshToken()).toBeNull()
  })

  it('does not attempt a refresh when there is no refresh token', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ detail: 'nope' }, 401))
    vi.stubGlobal('fetch', fetchMock)

    await expect(request('/users/me')).rejects.toBeInstanceOf(ApiError)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('honouring a stated pause', () => {
  // The regression: the restore path waited a flat two seconds, which is far
  // shorter than the auth window takes to refill. Every attempt was refused
  // and a customer holding a valid token was shown the sign-in screen anyway.
  it('waits the pause the service asked for, plus a moment', () => {
    // Returning the instant the window opens tends to land on the boundary.
    expect(pauseFor(20, 30_000)).toBe(21_000)
  })

  it('never waits longer than the caller can afford', () => {
    // An in-flight API call cannot sit for a minute just because a limiter
    // suggested it.
    expect(pauseFor(120, 5_000)).toBe(5_000)
  })

  it('falls back to a short pause when the service did not say', () => {
    expect(pauseFor(null, 30_000)).toBe(2_000)
    expect(pauseFor(Number.NaN, 30_000)).toBe(2_000)
  })

  it('does not turn a nonsense negative pause into time travel', () => {
    expect(pauseFor(-5, 30_000)).toBe(1_000)
  })
})

describe('retry policy', () => {
  it('retries a transport failure', () => {
    expect(shouldRetry(0, new NetworkError())).toBe(true)
  })

  it('retries a server error', () => {
    expect(shouldRetry(0, new ApiError({ status: 503, message: 'unavailable' }))).toBe(true)
  })

  it('never retries a deliberate refusal', () => {
    // The backend refused on purpose. Asking again with the same input gets the
    // same answer and spends the caller's admission budget doing it.
    for (const status of [400, 401, 403, 404, 409, 422, 429]) {
      expect(shouldRetry(0, new ApiError({ status, message: 'refused' }))).toBe(false)
    }
  })

  it('gives up after two attempts', () => {
    expect(shouldRetry(2, new NetworkError())).toBe(false)
  })
})
