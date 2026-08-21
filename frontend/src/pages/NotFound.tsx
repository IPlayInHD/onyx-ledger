/* =========================================================================
   NOT FOUND (404)
   =========================================================================
   This route catches every unmatched address, so it renders for signed-in
   customers and anonymous visitors alike. That rules out AppShell — it would
   show a navigation rail and an account menu to someone with no session — and
   it is why both ways back are offered rather than assuming which one applies.

   The tone is deliberately flat. A wrong address is not a mistake the customer
   made, it is not an occasion for a joke, and in a product that holds someone's
   financial position the first thing to say is that nothing has broken.
   ========================================================================= */
import { Link } from 'react-router-dom'
import { PageHead, SiteFooter, Wordmark } from '@/components/Shell'

export default function NotFound() {
  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>

      <header className="brandbar no-print">
        <div className="shell-container brandbar__inner">
          <Wordmark to="/" />
          <Link className="btn btn--ghost" to="/sign-in">
            Sign in
          </Link>
        </div>
      </header>

      <main className="shell__main" id="main">
        <div className="shell-container">
          <div style={{ maxWidth: '62ch' }}>
            <PageHead
              eyebrow="Page not found"
              title="There is nothing at this address."
              lede="The link may be out of date, or the page may have moved. Nothing has gone wrong with your account, and none of your figures have changed."
            />

            <section className="panel" aria-labelledby="next-heading">
              <div className="panel__header">
                <h2 className="section-title" id="next-heading">
                  Where to go from here
                </h2>
              </div>
              <div className="panel__body stack stack-5">
                <p className="text-sm text-secondary">
                  Your tax position is where you left it. If you are not signed
                  in, Onyx will ask you to sign in first, and the home page
                  explains what the product does before you do.
                </p>
                <div className="row row-3 wrap">
                  <Link className="btn btn--primary" to="/app">
                    Go to your tax position
                  </Link>
                  <Link className="btn btn--secondary" to="/">
                    Back to the home page
                  </Link>
                </div>
              </div>
            </section>
          </div>
        </div>
      </main>

      <SiteFooter />
    </div>
  )
}
