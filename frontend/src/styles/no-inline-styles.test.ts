/* =========================================================================
   THE POLICY AND THE CODE MUST AGREE
   =========================================================================
   The deployed Content-Security-Policy sets `style-src 'self'` — no
   'unsafe-inline'. That is only safe to ship while the application genuinely
   has no inline style attributes: the moment one reappears, the browser
   silently drops it and the layout quietly breaks in production, or somebody
   "fixes" it by adding 'unsafe-inline' back and the policy becomes decoration.

   125 inline style attributes were replaced by layout utility classes to make
   the strict policy possible. This test is what stops them creeping back.

   `style-src 'unsafe-inline'` is the half of CSP most often surrendered
   without argument. It does not permit script execution, so it reads as
   harmless — but it is what makes CSS-based data exfiltration and injected
   style defacement possible, and it costs nothing to keep out when the design
   system already speaks in classes.
   ========================================================================= */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = resolve(process.cwd(), 'src')

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full))
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      out.push(full)
    }
  }
  return out
}

describe('the bundle carries no inline styles', () => {
  it('no component sets a style attribute', () => {
    const offenders: string[] = []
    for (const file of sourceFiles(SRC)) {
      const text = readFileSync(file, 'utf8')
      // JSX `style={{ ... }}` and the plain HTML `style="..."` form.
      const jsx = text.match(/style=\{\{/g)?.length ?? 0
      const html = text.match(/\sstyle="/g)?.length ?? 0
      if (jsx + html > 0) {
        offenders.push(`${relative(process.cwd(), file)} — ${jsx + html} inline style(s)`)
      }
    }
    expect(
      offenders,
      `Inline styles break the shipped CSP (style-src 'self').\n` +
        `Use a layout utility class from styles/base.css instead:\n  ${offenders.join('\n  ')}`,
    ).toEqual([])
  })

  it('the index.html shell carries no inline style or script', () => {
    // A CSP of `script-src 'self'` also forbids inline <script>. The shell is
    // hand-written rather than generated, so it is the easiest place to add one
    // without noticing.
    const html = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8')
    expect(html).not.toMatch(/<style[\s>]/i)
    expect(html).not.toMatch(/\sstyle="/i)
    // A <script src="..."> is fine; a <script> with a body is not.
    const inlineScript = /<script(?![^>]*\bsrc=)[^>]*>[\s\S]*?\S[\s\S]*?<\/script>/i
    expect(html).not.toMatch(inlineScript)
  })
})
