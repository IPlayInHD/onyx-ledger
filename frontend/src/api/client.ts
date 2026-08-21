/* =========================================================================
   THE ONE API LAYER
   =========================================================================
   Every request to the certified backend goes through `request()`. There are
   no raw `fetch` calls in components, so authentication, error mapping,
   cancellation and retry policy are decided once instead of per screen.

   WHAT THIS LAYER DELIBERATELY DOES NOT DO: interpret tax. It moves typed
   payloads. Not one value here is computed, rounded, summed, compared or
   re-derived — the backend owns every business truth, and a formatter that
   "helpfully" recomputed a total would be a second tax authority wearing a
   utility's clothes.
   ========================================================================= */

/** Where the backend lives. In dev, Vite proxies /api to the FastAPI port.
 *  `VITE_API_BASE_URL` is a PUBLIC origin only — never a secret. No key,
 *  token or provider credential may ever be placed in a VITE_* variable,
 *  because everything in this bundle ships to the browser. */
const API_BASE: string = import.meta.env.VITE_API_BASE_URL ?? ''

export const API_PREFIX = '/api/v1'

/* ---------------------------------------------------------------- errors -- */

/** A backend failure, mapped to a stable shape the UI can branch on.
 *
 *  `code` preserves the backend's MACHINE semantics (its closed error-code
 *  vocabulary) while `message` is what a human reads. Keeping both is what
 *  lets a screen react correctly without ever printing an internal string at
 *  a customer. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string | null
  readonly detail: string | null
  readonly retryAfterSeconds: number | null

  constructor(init: {
    status: number
    message: string
    code?: string | null
    detail?: string | null
    retryAfterSeconds?: number | null
  }) {
    super(init.message)
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code ?? null
    this.detail = init.detail ?? null
    this.retryAfterSeconds = init.retryAfterSeconds ?? null
  }

  /** 404 is the backend's NON-ENUMERATING answer: "not yours" and "not there"
   *  are deliberately indistinguishable. The UI must therefore never say
   *  "this belongs to someone else" — it says the thing could not be found,
   *  which is exactly as much as we are entitled to know. */
  get isNotFound(): boolean {
    return this.status === 404
  }

  get isUnauthorized(): boolean {
    return this.status === 401
  }

  /** Admission control refused: rate, concurrency, or account lifecycle. */
  get isThrottled(): boolean {
    return this.status === 429
  }

  get isConflict(): boolean {
    return this.status === 409
  }

  get isValidation(): boolean {
    return this.status === 422
  }
}

/** The network itself failed — offline, DNS, connection reset. A different
 *  state from "the server answered with an error", and the UI says so. */
export class NetworkError extends Error {
  constructor(message = 'network-unavailable') {
    super(message)
    this.name = 'NetworkError'
  }
}

/* ------------------------------------------------------------- auth store -- */

/**
 * ACCESS TOKEN: memory only. It is never written to localStorage or
 * sessionStorage, so script injected into the page cannot read a long-lived
 * credential out of storage at rest.
 *
 * REFRESH TOKEN: sessionStorage, because the backend hands refresh tokens back
 * in the response body rather than as an httpOnly cookie, and a token held only
 * in memory would sign the customer out on every page reload. sessionStorage
 * bounds the exposure to the life of one tab instead of persisting across
 * sessions the way localStorage would.
 *
 * This is the best available posture WITHOUT changing the certified backend,
 * and it is deliberately recorded as a production-plumbing item: moving refresh
 * to an httpOnly, Secure, SameSite cookie is a backend contract change, not a
 * frontend one.
 */
const REFRESH_KEY = 'onyx.rt'

let accessToken: string | null = null
let onUnauthenticated: (() => void) | null = null

export const auth = {
  setAccessToken(token: string | null): void {
    accessToken = token
  },
  getAccessToken(): string | null {
    return accessToken
  },
  setRefreshToken(token: string | null): void {
    try {
      if (token === null) sessionStorage.removeItem(REFRESH_KEY)
      else sessionStorage.setItem(REFRESH_KEY, token)
    } catch {
      /* Private mode / disabled storage: the session simply does not survive a
         reload. Refusing to run would be a worse answer than that. */
    }
  },
  getRefreshToken(): string | null {
    try {
      return sessionStorage.getItem(REFRESH_KEY)
    } catch {
      return null
    }
  },
  clear(): void {
    accessToken = null
    auth.setRefreshToken(null)
  },
  /** Registered by the auth provider so a hard 401 can drop the session
   *  exactly once, from one place. */
  onUnauthenticated(handler: (() => void) | null): void {
    onUnauthenticated = handler
  },
}

/* --------------------------------------------------------------- request -- */

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface RequestOptions {
  method?: Method
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  signal?: AbortSignal
  /** Skip the refresh-and-retry dance (used by the refresh call itself). */
  skipAuthRefresh?: boolean
  /** Send without an Authorization header (register, login, public copy). */
  anonymous?: boolean
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${API_BASE}${API_PREFIX}${path}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

async function parseError(response: Response): Promise<ApiError> {
  let detail: string | null = null
  let code: string | null = null
  let retryAfter: number | null = null
  try {
    const payload = (await response.json()) as Record<string, unknown>
    // The backend's DomainError shape: {type,title,status,detail,...}. Its
    // `detail` is a closed enumerated string, never a stack trace.
    const raw = payload['detail']
    detail = typeof raw === 'string' ? raw : raw ? JSON.stringify(raw) : null
    const errorCode = payload['error_code'] ?? payload['code'] ?? payload['reason']
    code = typeof errorCode === 'string' ? errorCode : null
    const retry = payload['retry_after_seconds']
    retryAfter = typeof retry === 'number' ? retry : null
  } catch {
    /* A non-JSON error body (a proxy's HTML 502) is not shown to the user. */
  }
  return new ApiError({
    status: response.status,
    message: detail ?? `request-failed-${response.status}`,
    code,
    detail,
    retryAfterSeconds: retryAfter,
  })
}

let refreshInFlight: Promise<boolean> | null = null

/** Exchange the refresh token for a new access token. Concurrent callers share
 *  one in-flight attempt: a page that fires six queries at once must not spend
 *  six single-use refresh tokens and invalidate its own session (the backend
 *  treats refresh-token reuse as an attack and revokes the family). */
async function refreshSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight
  const token = auth.getRefreshToken()
  if (!token) return false

  refreshInFlight = (async () => {
    try {
      const response = await fetch(buildUrl('/auth/refresh'), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ refresh_token: token }),
      })
      if (!response.ok) {
        auth.clear()
        return false
      }
      const payload = (await response.json()) as {
        access_token?: string
        refresh_token?: string
      }
      if (!payload.access_token) {
        auth.clear()
        return false
      }
      auth.setAccessToken(payload.access_token)
      // Rotation: the backend issues a NEW refresh token and single-uses the
      // old one. Storing the new one is what keeps the next refresh working.
      if (payload.refresh_token) auth.setRefreshToken(payload.refresh_token)
      return true
    } catch {
      return false
    } finally {
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = 'GET', body, query, signal, skipAuthRefresh, anonymous } = options

  const headers: Record<string, string> = {}
  if (body !== undefined) headers['content-type'] = 'application/json'
  if (!anonymous) {
    const token = auth.getAccessToken()
    if (token) headers['authorization'] = `Bearer ${token}`
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
      // No cookies are used for auth, so no credentials are sent. This also
      // means CSRF is not reachable through ambient authority: a cross-site
      // form post carries no Authorization header.
      credentials: 'omit',
      redirect: 'error',
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new NetworkError()
  }

  if (response.status === 401 && !skipAuthRefresh && !anonymous) {
    const refreshed = await refreshSession()
    if (refreshed) {
      return request<T>(path, { ...options, skipAuthRefresh: true })
    }
    auth.clear()
    onUnauthenticated?.()
    throw await parseError(response)
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T

  const text = await response.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

/* ------------------------------------------------------------ retry rules -- */

/**
 * Retry is a correctness decision, not a reliability tweak.
 *
 * A GET may be retried: it is safe and idempotent. A POST may NOT be retried by
 * default — every expensive write on this backend (analysis, optimization,
 * scenario, explanation) is admission-controlled and some are idempotency-keyed,
 * so a blind retry either burns the caller's own concurrency budget or races
 * the request it is retrying.
 *
 * Nothing is ever retried after 4xx: the backend refused on purpose, and asking
 * again with the same input gets the same refusal.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false
  if (error instanceof NetworkError) return true
  if (error instanceof ApiError) {
    // 429 is handled by honouring Retry-After at the call site, not by an
    // immediate retry that would deepen the throttle.
    return error.status >= 500 && error.status < 600
  }
  return false
}
