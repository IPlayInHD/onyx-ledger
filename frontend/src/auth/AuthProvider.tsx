/* =========================================================================
   SESSION STATE
   =========================================================================
   One provider owns "who is signed in". Screens read it; they never poke at
   tokens, and no component reads storage directly.

   The session is restored on load by SPENDING THE REFRESH TOKEN, not by
   trusting anything in storage: the browser has no way to know whether a
   stored token is still valid, and the backend is the only thing entitled to
   answer that. If the exchange fails, the session is simply not restored.
   ========================================================================= */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { ApiError, NetworkError, auth, pauseFor, request } from '@/api/client'
import { authApi, recoveryApi, type TokenPair } from '@/api/endpoints'

export interface SessionUser {
  id: string
  email: string
  status: string
}

/**
 * `unverified` IS A SIGNED-IN STATE, and keeping it separate from both of its
 * neighbours is the whole point.
 *
 * Not `anonymous`: the credentials were accepted, the tokens are real, and
 * `/auth/verification` — the one endpoint that clears this state — needs them.
 * Folding it into `anonymous` would send someone back to a sign-in form that
 * is going to succeed and change nothing, which is the "generic login failure"
 * the customer cannot act on.
 *
 * Not `authenticated`: the product surface is refused by the backend, so
 * rendering it would fill the screen with failed requests.
 */
type Status = 'restoring' | 'authenticated' | 'unverified' | 'anonymous'

interface AuthState {
  status: Status
  user: SessionUser | null
  /** The address the pending link was sent to, so the "check your inbox"
   *  screen can name it. Known because the customer just typed it. */
  pendingEmail: string | null
  signIn: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  /** Ask for another verification link. Throws `ApiError` — a 429 carries the
   *  wait, and the screen shows it rather than guessing. */
  resendVerification: () => Promise<void>
  /** Re-read the account after a link is redeemed in THIS tab, so the app
   *  opens without making the customer sign in again. */
  recheck: () => Promise<void>
}

/* Restoring a session may wait out a real throttle. The bound is generous
   because the alternative is showing the sign-in screen to someone who is
   already signed in and holds a valid token. */
const ATTEMPTS = 3
const RESTORE_MAX_WAIT_MS = 30_000

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>('restoring')
  const [user, setUser] = useState<SessionUser | null>(null)
  const [pendingEmail, setPendingEmail] = useState<string | null>(null)

  const adopt = useCallback((tokens: TokenPair) => {
    auth.setAccessToken(tokens.access_token)
    auth.setRefreshToken(tokens.refresh_token)
  }, [])

  const drop = useCallback(() => {
    auth.clear()
    setUser(null)
    setPendingEmail(null)
    setStatus('anonymous')
  }, [])

  /**
   * Read the account behind the tokens we now hold, and settle into the state
   * it implies. ONE function, called from sign-in, registration and session
   * restore, because three copies of "what does a 403 here mean" would drift
   * and only one of them would be tested.
   *
   * The tokens are KEPT on a verification refusal. They are valid — the
   * account simply may not act yet — and they are exactly what the resend
   * endpoint requires.
   */
  const settle = useCallback(async (knownEmail?: string) => {
    try {
      const me = await authApi.me()
      setUser(me)
      setPendingEmail(null)
      setStatus('authenticated')
    } catch (error) {
      if (error instanceof ApiError && error.isVerificationRequired) {
        setUser(null)
        if (knownEmail) setPendingEmail(knownEmail)
        setStatus('unverified')
        return
      }
      throw error
    }
  }, [])

  /* A 401 that survives a refresh attempt means the session is gone —
     revoked, expired, or the account was deleted or disabled. One handler
     drops it, so every screen behaves identically. */
  useEffect(() => {
    auth.onUnauthenticated(() => {
      setUser(null)
      setStatus('anonymous')
    })
    return () => auth.onUnauthenticated(null)
  }, [])

  useEffect(() => {
    let cancelled = false

    async function restore() {
      const refreshToken = auth.getRefreshToken()
      if (!refreshToken) {
        if (!cancelled) setStatus('anonymous')
        return
      }

      // `/auth/refresh` shares the platform's authentication budget: reloading
      // the page while that budget is spent answers 429, and a customer must
      // not be signed out because the service asked them to wait.
      //
      // The pause is THE ONE THE SERVICE STATED, not a guess. A fixed two
      // seconds was far shorter than the window takes to refill, so both
      // attempts were refused and a customer holding a perfectly valid token
      // landed on the sign-in screen anyway.
      for (let attempt = 0; attempt < ATTEMPTS; attempt += 1) {
        try {
          const tokens = await request<TokenPair>('/auth/refresh', {
            method: 'POST',
            body: { refresh_token: auth.getRefreshToken() ?? refreshToken },
            anonymous: true,
            skipAuthRefresh: true,
          })
          adopt(tokens)
          // No address to pass: a restored session knows the tokens, not what
          // was typed to get them. The verification screen handles that by
          // saying "your address" rather than inventing one.
          await settle()
          if (cancelled) return
          return
        } catch (error) {
          const transient =
            error instanceof NetworkError ||
            (error instanceof ApiError && (error.isThrottled || error.status >= 500))

          if (!transient) {
            // The credential itself was refused. This session is over.
            if (!cancelled) drop()
            return
          }
          if (attempt < ATTEMPTS - 1) {
            const stated = error instanceof ApiError ? error.retryAfterSeconds : null
            await new Promise((resolve) =>
              setTimeout(resolve, pauseFor(stated, RESTORE_MAX_WAIT_MS)),
            )
            if (cancelled) return
            continue
          }
          // Still could not reach a verdict. Go to the signed-out view WITHOUT
          // destroying the refresh token: nothing has told us it is invalid,
          // and keeping it lets the next attempt succeed.
          if (!cancelled) {
            setUser(null)
            setStatus('anonymous')
          }
        }
      }
    }

    void restore()
    return () => {
      cancelled = true
    }
  }, [adopt, drop, settle])

  const signIn = useCallback(
    async (email: string, password: string) => {
      const tokens = await authApi.login(email, password)
      adopt(tokens)
      await settle(email)
    },
    [adopt, settle],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      await authApi.register(email, password)
      // Registering does not sign you in on the backend; log in explicitly so
      // there is exactly one code path that establishes a session.
      const tokens = await authApi.login(email, password)
      adopt(tokens)
      // Settles into `unverified`, which is now the ordinary outcome of
      // registering — the account exists and has not confirmed its address.
      await settle(email)
    },
    [adopt, settle],
  )

  const resendVerification = useCallback(async () => {
    await recoveryApi.sendVerification()
  }, [])

  const recheck = useCallback(async () => {
    await settle()
  }, [settle])

  const signOut = useCallback(async () => {
    try {
      await authApi.logout()
    } catch (error) {
      // A failed logout call must still clear the client session: leaving the
      // customer looking signed in because the network blipped is worse than
      // a server-side session that expires on its own.
      if (!(error instanceof ApiError)) {
        /* network — fall through to clearing anyway */
      }
    } finally {
      drop()
    }
  }, [drop])

  const value = useMemo<AuthState>(
    () => ({
      status,
      user,
      pendingEmail,
      signIn,
      register,
      signOut,
      resendVerification,
      recheck,
    }),
    [status, user, pendingEmail, signIn, register, signOut, resendVerification, recheck],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
