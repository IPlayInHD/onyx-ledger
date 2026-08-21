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
import { ApiError, auth, request } from '@/api/client'
import { authApi, type TokenPair } from '@/api/endpoints'

export interface SessionUser {
  id: string
  email: string
  status: string
}

type Status = 'restoring' | 'authenticated' | 'anonymous'

interface AuthState {
  status: Status
  user: SessionUser | null
  signIn: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>('restoring')
  const [user, setUser] = useState<SessionUser | null>(null)

  const adopt = useCallback((tokens: TokenPair) => {
    auth.setAccessToken(tokens.access_token)
    auth.setRefreshToken(tokens.refresh_token)
  }, [])

  const drop = useCallback(() => {
    auth.clear()
    setUser(null)
    setStatus('anonymous')
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
      try {
        const tokens = await request<TokenPair>('/auth/refresh', {
          method: 'POST',
          body: { refresh_token: refreshToken },
          anonymous: true,
          skipAuthRefresh: true,
        })
        adopt(tokens)
        const me = await authApi.me()
        if (cancelled) return
        setUser(me)
        setStatus('authenticated')
      } catch {
        if (!cancelled) drop()
      }
    }

    void restore()
    return () => {
      cancelled = true
    }
  }, [adopt, drop])

  const signIn = useCallback(
    async (email: string, password: string) => {
      const tokens = await authApi.login(email, password)
      adopt(tokens)
      const me = await authApi.me()
      setUser(me)
      setStatus('authenticated')
    },
    [adopt],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      await authApi.register(email, password)
      // Registering does not sign you in on the backend; log in explicitly so
      // there is exactly one code path that establishes a session.
      const tokens = await authApi.login(email, password)
      adopt(tokens)
      const me = await authApi.me()
      setUser(me)
      setStatus('authenticated')
    },
    [adopt],
  )

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
    () => ({ status, user, signIn, register, signOut }),
    [status, user, signIn, register, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
