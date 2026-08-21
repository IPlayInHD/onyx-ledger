/* =========================================================================
   APP CHROME
   =========================================================================
   Brand bar, navigation rail, footer, and the tax-year context every data
   screen reads from. The year lives here rather than in each screen so the
   whole product is unambiguously talking about ONE tax year at a time — a
   position, an opportunity set and a scenario from different years would be a
   quietly wrong comparison.
   ========================================================================= */
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { Link, NavLink, useNavigate } from 'react-router-dom'
import { useAuth } from '@/auth/AuthProvider'

/* ------------------------------------------------------------- tax year -- */

/** Years the certified backend actually has governed reference data for. The
 *  list is explicit rather than derived from the clock: offering a year the
 *  engine cannot resolve would produce a fail-closed error the customer
 *  cannot act on. */
export const SUPPORTED_TAX_YEARS = [2025, 2026] as const
export type SupportedTaxYear = (typeof SUPPORTED_TAX_YEARS)[number]

interface YearState {
  taxYear: SupportedTaxYear
  setTaxYear: (year: SupportedTaxYear) => void
}
const YearContext = createContext<YearState | null>(null)

export function TaxYearProvider({ children }: { children: ReactNode }) {
  const [taxYear, setTaxYear] = useState<SupportedTaxYear>(2025)
  const value = useMemo(() => ({ taxYear, setTaxYear }), [taxYear])
  return <YearContext.Provider value={value}>{children}</YearContext.Provider>
}

export function useTaxYear(): YearState {
  const context = useContext(YearContext)
  if (!context) throw new Error('useTaxYear must be used inside TaxYearProvider')
  return context
}

/* ------------------------------------------------------------- wordmark -- */

export function Wordmark({ to = '/' }: { to?: string }) {
  return (
    <Link to={to} className="wordmark">
      <span className="wordmark__mark" aria-hidden="true" />
      <span className="wordmark__text">
        <b>Onyx</b> Ledger
      </span>
    </Link>
  )
}

/* ------------------------------------------------------------ nav config -- */

const PRIMARY_NAV = [
  { to: '/app', label: 'Overview', end: true },
  { to: '/app/position', label: 'Tax position' },
  { to: '/app/opportunities', label: 'Opportunities' },
  { to: '/app/twin', label: 'Decision Twin' },
  { to: '/app/evidence', label: 'Evidence' },
  { to: '/app/changes', label: 'What changed' },
]

/* --------------------------------------------------------- account menu -- */

function AccountMenu() {
  const { user, signOut } = useAuth()
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="accountmenu" ref={containerRef}>
      <button
        type="button"
        className="btn btn--ghost btn--sm"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((value) => !value)}
      >
        Account
        <span aria-hidden="true">{open ? '▴' : '▾'}</span>
      </button>
      {open ? (
        <div className="accountmenu__panel" role="menu">
          {user ? <div className="accountmenu__email">{user.email}</div> : null}
          <Link className="accountmenu__item" role="menuitem" to="/app/settings" onClick={() => setOpen(false)}>
            Settings &amp; privacy
          </Link>
          <Link className="accountmenu__item" role="menuitem" to="/trust" onClick={() => setOpen(false)}>
            Trust Centre
          </Link>
          <Link className="accountmenu__item" role="menuitem" to="/legal" onClick={() => setOpen(false)}>
            Legal &amp; policies
          </Link>
          <button
            type="button"
            className="accountmenu__item"
            role="menuitem"
            onClick={() => {
              setOpen(false)
              void signOut().then(() => navigate('/'))
            }}
          >
            Sign out
          </button>
        </div>
      ) : null}
    </div>
  )
}

/* --------------------------------------------------------------- footer -- */

export function SiteFooter() {
  return (
    <footer className="sitefooter no-print">
      <div className="shell-container">
        <div className="sitefooter__grid">
          <div className="stack stack-3">
            <Wordmark />
            <p className="text-sm text-secondary" style={{ maxWidth: '38ch' }}>
              Understand your tax position, see what a decision would change, and
              keep the evidence behind it organised.
            </p>
          </div>
          <nav aria-label="Trust and transparency" className="stack">
            <span className="eyebrow">Transparency</span>
            <Link to="/trust">Trust Centre</Link>
            <Link to="/legal/ai-transparency">AI transparency</Link>
            <Link to="/legal/tax-disclaimer">Tax disclaimer</Link>
            <Link to="/legal/accessibility">Accessibility</Link>
          </nav>
          <nav aria-label="Legal" className="stack">
            <span className="eyebrow">Legal</span>
            <Link to="/legal/terms">Terms of Service</Link>
            <Link to="/legal/privacy">Privacy Policy</Link>
            <Link to="/legal/security">Security</Link>
          </nav>
        </div>
        <p className="sitefooter__legal">
          Onyx Ledger provides software-generated tax information and estimates
          for Canadian federal and supported provincial tax. It is not a
          substitute for advice from a qualified tax professional, and it is not
          affiliated with or endorsed by the Canada Revenue Agency.
        </p>
      </div>
    </footer>
  )
}

/* ------------------------------------------------------------- app shell -- */

export function AppShell({ children }: { children: ReactNode }) {
  const { taxYear, setTaxYear } = useTaxYear()

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="brandbar no-print">
        <div className="shell-container brandbar__inner">
          <Wordmark to="/app" />
          <div className="row row-3">
            <label className="yearpick">
              <span className="eyebrow">Tax year</span>
              <select
                value={taxYear}
                onChange={(event) =>
                  setTaxYear(Number(event.target.value) as SupportedTaxYear)
                }
                aria-label="Tax year"
              >
                {SUPPORTED_TAX_YEARS.map((year) => (
                  <option key={year} value={year}>
                    {year}
                  </option>
                ))}
              </select>
            </label>
            <AccountMenu />
          </div>
        </div>
      </header>

      <nav className="navrail no-print" aria-label="Primary">
        <div className="shell-container">
          <div className="navrail__scroll">
            {PRIMARY_NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className="navlink"
              >
                {item.label}
              </NavLink>
            ))}
          </div>
        </div>
      </nav>

      <main className="shell__main" id="main">
        <div className="shell-container">{children}</div>
      </main>

      <SiteFooter />
    </div>
  )
}

export function PageHead({
  eyebrow,
  title,
  lede,
  actions,
}: {
  eyebrow?: string
  title: string
  lede?: string
  actions?: ReactNode
}) {
  return (
    <div className="pagehead">
      <div>
        {eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}
        <h1 className="pagehead__title">{title}</h1>
        {lede ? <p className="pagehead__lede">{lede}</p> : null}
      </div>
      {actions ? <div className="row row-2 wrap no-print">{actions}</div> : null}
    </div>
  )
}
