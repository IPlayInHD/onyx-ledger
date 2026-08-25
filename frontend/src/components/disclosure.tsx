/* =========================================================================
   PROGRESSIVE DISCLOSURE
   =========================================================================
   Beginner by default, depth on demand.

   The consumer lexicon gave every governed state three registers — a short
   consumer meaning, a plain explanation, and the formal term. This gives them
   somewhere to live on screen, so the simple reading is what a first-time
   filer gets and the precise one is still one deliberate action away.

     Level 1   always visible. Never a code, never behind a control.
     Level 2   one click: why am I seeing this, and what should I do?
     Level 3   supporting detail, only where the contract already provides it.
     Level 4   the technical/audit register.

   TWO CONTROLS, NOT FOUR. Levels 3 and 4 do not each get their own toggle at
   the top level — that is how a disclosure component turns into a stack of
   developer debug panels. Opening Level 2 is what reveals that deeper registers
   exist at all, so the default state stays quiet and the depth is discovered
   rather than displayed.

   THE ACTION NEVER MOVES. Anything the customer has to DO renders beside
   Level 1, outside every disclosure. Progressive disclosure is for explanation
   and detail; hiding the task behind `expand → expand → find the button` is the
   failure this pattern exists to avoid.

   NOTHING EMPTY EVER RENDERS. A level with no content produces no control and
   no region, so an item with only a Level 1 is a complete item rather than a
   row of dead toggles. That matters more than it sounds: the lexicon returns
   `null` for states nobody has written words for, and `null` must read as
   "say nothing", not as "render an empty box".
   ========================================================================= */
import { useId, useState, type ReactNode } from 'react'

export interface DisclosureProps {
  /** The consumer meaning. Always visible. Plain language, never a code. */
  level1: ReactNode
  /**
   * Why this is here and what to do about it. Omit — or pass `null`, which is
   * what the lexicon returns for an unmapped or suppressed state — and no
   * control is offered at all.
   */
  level2?: ReactNode | null
  /** Structured supporting detail. Only where the contract already has it. */
  detail?: ReactNode | null
  /** The technical / audit register. Demoted, never deleted. */
  technical?: ReactNode | null
  /**
   * What the customer must DO. Rendered beside Level 1 and never inside a
   * disclosure.
   */
  action?: ReactNode | null
  /** Labels the "why" control for assistive technology, e.g. the item's name. */
  about?: string
  /** Wording for the level-2 control. Defaults to "Why?" / "Hide". */
  whyLabel?: string
  detailLabel?: string
  technicalLabel?: string
  className?: string
}

function Nested({
  label,
  children,
  about,
}: {
  label: string
  children: ReactNode
  about?: string
}) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <div className="disclose__nested">
      <button
        aria-controls={`${id}-body`}
        aria-expanded={open}
        className="disclose__toggle disclose__toggle--nested"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        {open ? `Hide ${label.toLowerCase()}` : label}
        {about ? <span className="sr-only"> for {about}</span> : null}
      </button>
      {/* `hidden` rather than unmounting: the region keeps its identity for
          `aria-controls`, and a screen reader that has already been told the
          control owns this region finds it where it was promised. */}
      <div className="disclose__body" hidden={!open} id={`${id}-body`}>
        {children}
      </div>
    </div>
  )
}

/**
 * One finding, stated simply, with its depth reachable.
 *
 * `level1` is not optional and is not behind anything. If a caller has no
 * Level 1 to give, it has nothing a consumer can read, and the right answer is
 * to render no item — not an item that says nothing.
 */
export function Disclosure({
  level1,
  level2,
  detail,
  technical,
  action,
  about,
  whyLabel = 'Why?',
  detailLabel = 'Supporting details',
  technicalLabel = 'Technical details',
  className,
}: DisclosureProps) {
  const [open, setOpen] = useState(false)
  const id = useId()

  const hasWhy = level2 !== null && level2 !== undefined && level2 !== ''
  const hasDetail = detail !== null && detail !== undefined && detail !== false
  const hasTechnical = technical !== null && technical !== undefined && technical !== false
  /* Deeper registers are reached THROUGH level 2. Without it there is nothing
     to open, so a caller that supplies only `technical` gets no control — which
     is deliberate: the audit register is not a substitute for an explanation. */
  const expandable = hasWhy

  return (
    <div className={className ? `disclose ${className}` : 'disclose'}>
      <div className="disclose__head">
        <div className="disclose__level1">{level1}</div>
        {action ? <div className="disclose__action">{action}</div> : null}
      </div>

      {expandable ? (
        <>
          <button
            aria-controls={`${id}-why`}
            aria-expanded={open}
            className="disclose__toggle"
            onClick={() => setOpen((value) => !value)}
            type="button"
          >
            {open ? 'Hide explanation' : whyLabel}
            {about ? <span className="sr-only"> for {about}</span> : null}
          </button>

          <div className="disclose__why" hidden={!open} id={`${id}-why`}>
            <p className="disclose__level2">{level2}</p>
            {hasDetail ? (
              <Nested about={about} label={detailLabel}>
                {detail}
              </Nested>
            ) : null}
            {hasTechnical ? (
              <Nested about={about} label={technicalLabel}>
                {technical}
              </Nested>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  )
}

/**
 * A labelled row inside Level 3 or Level 4.
 *
 * A definition list rather than a table: these are name/value pairs, screen
 * readers announce them as such, and a two-column table would need a header
 * row that says nothing on a phone.
 */
export function DetailList({
  items,
}: {
  items: readonly { readonly label: string; readonly value: ReactNode }[]
}) {
  const shown = items.filter((entry) => entry.value !== null && entry.value !== undefined && entry.value !== '')
  if (shown.length === 0) return null
  return (
    <dl className="disclose__list">
      {shown.map((entry) => (
        <div className="disclose__pair" key={entry.label}>
          <dt>{entry.label}</dt>
          <dd>{entry.value}</dd>
        </div>
      ))}
    </dl>
  )
}
