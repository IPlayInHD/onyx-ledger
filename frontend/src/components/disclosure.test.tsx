/* =========================================================================
   WHAT PROGRESSIVE DISCLOSURE PROMISES
   =========================================================================
   The default state has to be complete on its own — a first-time filer who
   never opens anything must still know what is happening and what to do — and
   every deeper register has to be reachable deliberately, by keyboard, with
   the same words a sighted user gets.
   ========================================================================= */
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe as suite, expect, it } from 'vitest'
import { DetailList, Disclosure } from './disclosure'

suite('the default state is Level 1 and the action', () => {
  it('shows Level 1 without expanding anything', () => {
    render(<Disclosure level1="We need a document" level2="Send it and Onyx can include this." />)
    expect(screen.getByText('We need a document')).toBeVisible()
  })

  it('keeps the action visible and outside every disclosure', async () => {
    render(
      <Disclosure
        action={<button type="button">Add it</button>}
        level1="We need a document"
        level2="Send it and Onyx can include this."
      />,
    )
    /* The task is reachable with no expansion at all. `expand → expand → find
       the button` is the interaction this asserts against. */
    expect(screen.getByRole('button', { name: 'Add it' })).toBeVisible()
  })

  it('hides Level 2 until asked', () => {
    render(<Disclosure level1="Up to date" level2="Read straight from your records." />)
    expect(screen.getByText('Read straight from your records.')).not.toBeVisible()
  })

  it('hides Level 3 and Level 4 until asked', () => {
    render(
      <Disclosure
        detail={<p>supporting</p>}
        level1="Up to date"
        level2="Read straight from your records."
        technical={<p>formal</p>}
      />,
    )
    /* ABSENT FROM THE ACCESSIBILITY TREE, not merely invisible. A control
       inside a `hidden` region must not be reachable by a screen reader or by
       Tab while it is closed, and `queryByRole` answers exactly that question. */
    expect(screen.queryByRole('button', { name: /supporting details/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /technical details/i })).toBeNull()
  })
})

suite('depth is reachable, one deliberate step at a time', () => {
  it('reveals Level 2 on activation', async () => {
    const user = userEvent.setup()
    render(<Disclosure level1="Up to date" level2="Read straight from your records." />)
    await user.click(screen.getByRole('button', { name: /why\?/i }))
    expect(screen.getByText('Read straight from your records.')).toBeVisible()
  })

  it('offers Level 3 and Level 4 only once Level 2 is open', async () => {
    const user = userEvent.setup()
    render(
      <Disclosure
        detail={<p>supporting detail</p>}
        level1="We need a document"
        level2="Send it and Onyx can include this."
        technical={<p>Evidence required</p>}
      />,
    )
    await user.click(screen.getByRole('button', { name: /why\?/i }))
    const detail = screen.getByRole('button', { name: /supporting details/i })
    expect(detail).toBeVisible()

    await user.click(detail)
    expect(screen.getByText('supporting detail')).toBeVisible()

    await user.click(screen.getByRole('button', { name: /technical details/i }))
    expect(screen.getByText('Evidence required')).toBeVisible()
  })

  it('collapses again', async () => {
    const user = userEvent.setup()
    render(<Disclosure level1="Up to date" level2="Read straight from your records." />)
    const toggle = screen.getByRole('button', { name: /why\?/i })
    await user.click(toggle)
    await user.click(screen.getByRole('button', { name: /hide explanation/i }))
    expect(screen.getByText('Read straight from your records.')).not.toBeVisible()
  })
})

suite('absent levels produce nothing at all', () => {
  it('offers no control when there is no Level 2', () => {
    render(<Disclosure level1="Does not apply to you" />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('treats a null Level 2 — what the lexicon returns for an unmapped or ' +
    'suppressed state — as no Level 2', () => {
    render(<Disclosure level1="Does not apply to you" level2={null} />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders no Level 3 section when detail is absent', async () => {
    const user = userEvent.setup()
    render(<Disclosure level1="Up to date" level2="Because." technical={<p>formal</p>} />)
    await user.click(screen.getByRole('button', { name: /why\?/i }))
    expect(screen.queryByRole('button', { name: /supporting details/i })).toBeNull()
    expect(screen.getByRole('button', { name: /technical details/i })).toBeVisible()
  })

  it('offers no depth at all when only a technical register is supplied', () => {
    /* Deliberate: the audit register is not a substitute for an explanation,
       so a caller with `technical` and no `level2` gets no control. */
    render(<Disclosure level1="Up to date" technical={<p>formal</p>} />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('drops empty rows from a detail list rather than rendering blanks', () => {
    render(
      <DetailList
        items={[
          { label: 'Kept', value: 'yes' },
          { label: 'Null', value: null },
          { label: 'Empty', value: '' },
        ]}
      />,
    )
    expect(screen.getByText('Kept')).toBeVisible()
    expect(screen.queryByText('Null')).toBeNull()
    expect(screen.queryByText('Empty')).toBeNull()
  })

  it('renders nothing when every row is empty', () => {
    const { container } = render(<DetailList items={[{ label: 'Null', value: null }]} />)
    expect(container.querySelector('dl')).toBeNull()
  })
})

suite('accessibility', () => {
  it('uses real buttons with expanded state', async () => {
    const user = userEvent.setup()
    render(<Disclosure level1="Up to date" level2="Because." />)
    const toggle = screen.getByRole('button', { name: /why\?/i })
    expect(toggle).toHaveAttribute('type', 'button')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    expect(screen.getByRole('button', { name: /hide explanation/i })).toHaveAttribute(
      'aria-expanded',
      'true',
    )
  })

  it('points aria-controls at the region it actually opens', () => {
    const { container } = render(<Disclosure level1="Up to date" level2="Because." />)
    const toggle = screen.getByRole('button', { name: /why\?/i })
    const controls = toggle.getAttribute('aria-controls')
    expect(controls, 'the toggle names no region').toBeTruthy()
    const region = container.querySelector(`#${CSS.escape(controls!)}`)
    expect(region, 'aria-controls points at nothing').not.toBeNull()
    expect(within(region as HTMLElement).getByText('Because.')).toBeInTheDocument()
  })

  it('is operable from the keyboard alone', async () => {
    const user = userEvent.setup()
    render(<Disclosure level1="Up to date" level2="Read straight from your records." />)
    await user.tab()
    expect(screen.getByRole('button', { name: /why\?/i })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByText('Read straight from your records.')).toBeVisible()
  })

  it('gives each control a distinct accessible name when several are on a page', () => {
    /* Two items with identical wording are indistinguishable in a screen
       reader's control list. `about` is what tells them apart. */
    render(
      <>
        <Disclosure about="Putting money into an RRSP" level1="A" level2="B" />
        <Disclosure about="Claiming your tuition" level1="C" level2="D" />
      </>,
    )
    expect(screen.getByRole('button', { name: /why\? for Putting money into an RRSP/i })).toBeVisible()
    expect(screen.getByRole('button', { name: /why\? for Claiming your tuition/i })).toBeVisible()
  })

  it('says the same thing to everyone', async () => {
    /* The previous entry briefly handed screen-reader users MORE internal
       jargon than sighted users. There is no sr-only alternative text here at
       all: the visible words and the announced words are one string. */
    const user = userEvent.setup()
    const { container } = render(
      <Disclosure about="an item" level1="We need a document" level2="Send it and Onyx can include this." />,
    )
    await user.click(screen.getByRole('button', { name: /why\?/i }))
    const srOnly = [...container.querySelectorAll('.sr-only')].map((n) => n.textContent)
    expect(
      srOnly.every((text) => (text ?? '').trim() === 'for an item'),
      `unexpected screen-reader-only text: ${JSON.stringify(srOnly)}`,
    ).toBe(true)
  })
})
