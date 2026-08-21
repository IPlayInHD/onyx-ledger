/* =========================================================================
   ROUTES
   =========================================================================
   Public surfaces (landing, trust, legal) are always reachable — including to
   a signed-out visitor and to a crawler — because a product that asks people
   for financial facts must let them read how it works and what it promises
   BEFORE they sign up.

   Product surfaces sit behind `RequireAuth`. Route-level lazy loading keeps
   the first paint small: a visitor reading the landing page never downloads
   the Decision Twin.
   ========================================================================= */
import { lazy, Suspense, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AppShell } from '@/components/Shell'
import { LoadingBlock } from '@/components/states'
import { useAuth } from '@/auth/AuthProvider'

const Landing = lazy(() => import('@/pages/Landing'))
const SignIn = lazy(() => import('@/pages/SignIn'))
const SignUp = lazy(() => import('@/pages/SignUp'))
const Onboarding = lazy(() => import('@/pages/Onboarding'))
const Overview = lazy(() => import('@/pages/Overview'))
const Position = lazy(() => import('@/pages/Position'))
const Opportunities = lazy(() => import('@/pages/Opportunities'))
const DecisionTwin = lazy(() => import('@/pages/DecisionTwin'))
const BeforeYouAct = lazy(() => import('@/pages/BeforeYouAct'))
const Evidence = lazy(() => import('@/pages/Evidence'))
const WhatChanged = lazy(() => import('@/pages/WhatChanged'))
const Settings = lazy(() => import('@/pages/Settings'))
const TrustCentre = lazy(() => import('@/pages/TrustCentre'))
const LegalIndex = lazy(() => import('@/pages/legal/LegalIndex'))
const LegalDocument = lazy(() => import('@/pages/legal/LegalDocument'))
const NotFound = lazy(() => import('@/pages/NotFound'))

function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const location = useLocation()

  // While the refresh token is being exchanged we render a loading state
  // rather than a redirect: bouncing a signed-in customer to /sign-in on every
  // reload because the exchange had not finished yet would look like the
  // product forgetting them.
  if (status === 'restoring') {
    return (
      <div className="shell-container" style={{ padding: 'var(--space-9) 0' }}>
        <LoadingBlock label="Restoring your session" />
      </div>
    )
  }
  if (status === 'anonymous') {
    return <Navigate to="/sign-in" replace state={{ from: location.pathname }} />
  }
  return <AppShell>{children}</AppShell>
}

function Fallback() {
  return (
    <div className="shell-container" style={{ padding: 'var(--space-9) 0' }}>
      <LoadingBlock />
    </div>
  )
}

export default function App() {
  return (
    <Suspense fallback={<Fallback />}>
      <Routes>
        {/* public */}
        <Route path="/" element={<Landing />} />
        <Route path="/sign-in" element={<SignIn />} />
        <Route path="/sign-up" element={<SignUp />} />
        <Route path="/trust" element={<TrustCentre />} />
        <Route path="/legal" element={<LegalIndex />} />
        <Route path="/legal/:documentId" element={<LegalDocument />} />

        {/* product */}
        <Route path="/app" element={<RequireAuth><Overview /></RequireAuth>} />
        <Route path="/app/onboarding" element={<RequireAuth><Onboarding /></RequireAuth>} />
        <Route path="/app/position" element={<RequireAuth><Position /></RequireAuth>} />
        <Route path="/app/opportunities" element={<RequireAuth><Opportunities /></RequireAuth>} />
        <Route path="/app/opportunities/:sourceId" element={<RequireAuth><Opportunities /></RequireAuth>} />
        <Route path="/app/twin" element={<RequireAuth><DecisionTwin /></RequireAuth>} />
        <Route path="/app/twin/:scenarioId" element={<RequireAuth><DecisionTwin /></RequireAuth>} />
        <Route path="/app/before-you-act/:scenarioId" element={<RequireAuth><BeforeYouAct /></RequireAuth>} />
        <Route path="/app/evidence" element={<RequireAuth><Evidence /></RequireAuth>} />
        <Route path="/app/changes" element={<RequireAuth><WhatChanged /></RequireAuth>} />
        <Route path="/app/settings" element={<RequireAuth><Settings /></RequireAuth>} />

        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  )
}
