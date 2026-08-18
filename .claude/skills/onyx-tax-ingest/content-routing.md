# Content routing — what each format may become

Format determines what a file is *allowed* to turn into. The rule underneath
all three routes:

> Structure may become knowledge. Prose may only become provenance.

## 0. Establishing format — MIME type alone is not enough

Drive frequently reports a generic `application/octet-stream` for text files it
did not sniff, and a generic type is **not** evidence that a file is
unsupported. Route on the balance of three signals:

1. **filename extension** — weak on its own (this library has doubled and
   space-padded extensions), but informative
2. **declared MIME type** — informative when specific, meaningless when generic
3. **content inspection** — the magic bytes or first line, which is decisive

`%PDF-` is a PDF whatever Drive called it. A file whose first line is
`openapi: 3.0.3` is YAML whatever Drive called it.

**Never reject a supported file merely because Drive supplied a generic MIME
type.** Conversely, never trust a specific MIME type over contradicting content
— the bytes win.

## PDF — provenance first, structure second

A PDF is registered as a **source** before anything is extracted from it. That
ordering is not bureaucratic: the citation you will attach must name a source
version that already exists, and extracting first means writing knowledge with
nowhere to hang its authority.

**Permitted:** register it, cite precise locators within it, and hand-author
structured knowledge whose meaning a reviewer can trace to those locators.

**Forbidden:**

- Treating raw prose as executable tax logic. A paragraph is not a rule.
- Any runtime read. The evaluator must never open a PDF, and no published
  object may carry a path or URL to be fetched later. Documents are **recorded,
  never retrieved**.
- RAG, embeddings or retrieval as a tax authority. Retrieval may help a human
  find a provision; it may not decide one.
- Publishing an extraction because it "looks right". Extraction produces a
  **draft**; validators and a reviewer decide.

Extraction quality varies wildly across producers. Table extraction in
particular fails quietly — a merged cell or a footnote row can shift a whole
column. Treat every extracted table as a candidate to be checked against a
worked example, not as data.

Expect PDFs that are scans with no text layer. That is a blocker for extraction,
not a reason to guess: register the source, cite it, and escalate the provision.

## CSV — the most direct path to governed reference data

CSV is the one format that can go almost straight into governed reference data,
which is exactly why its validation is strictest.

**Requirements:**

- **Decimal, never float.** Parse from the string form. A JSON/CSV number that
  passes through a float is already not the value that was written, and
  eligibility that turns on a threshold must not turn on binary rounding.
- **Strict schema and types.** Column identity is declared, not inferred from
  position.
- **Validate ordering, ranges and duplicates** before proposing anything:
  bracket thresholds strictly increasing, intervals non-overlapping and
  contiguous from zero, a terminal open-ended bracket, rates within range, no
  duplicate semantic key for the same scope.
- **Prefer direct governed reference-data import** over routing values through
  a rule. Brackets, limits and indexed parameters are read by the engine
  directly; a rule that merely restates them adds a second place to be wrong.

### Inspect structure before assuming a header

**Never assume line 1 is the header.** Government and central-bank exports are
frequently **multi-section**: banner rows, a licence block, a metadata block,
and one or more embedded header rows before the real table — sometimes several
unrelated tables in one file.

The failure mode this prevents is the dangerous kind: a naive
`csv.reader`/`read_csv` takes a banner line as the header and **succeeds**,
producing a structurally valid table of nonsense. Nothing raises.

So, generically:

1. read the file's structure first — how many sections, where each begins
2. locate the **data section** explicitly, by its banner or by shape
3. take the header from that section, not from the file
4. confirm the rows below it are rectangular and match the header width
5. cross-check any in-file series/column listing against the header names

Also handle a **UTF-8 BOM**: decode with `utf-8-sig`, or the first column name
carries an invisible prefix and will never match anything.

Do not hardcode any particular publisher's layout. Detect the structure of the
file in front of you.

**Published reference data is immutable.** The reference tables are unique on
their semantic key and carry no version column, so publication INSERTs and
refuses a key that already exists. Correcting a published value is not
something this skill does — escalate it.

## JSON / YAML — candidate manifests, not authority

Structured input is a **candidate manifest**. Machine-readability is not
authority, and this is the format where that mistake is easiest to make.

**Requirements:**

- **Unknown fields are rejected**, at every nesting level. A field the pipeline
  ignores is a claim its author made that nothing acted on.
- **A manifest references provenance by registry identity** — citation ids. It
  never restates an issuer, title, URL or section text. Duplicating source
  metadata creates a second record of what the law says, and the two will
  diverge.
- **No executable logic.** No expression to `eval`, no callable, no SQL, no
  Python. Conditions and formulas use the engine's constrained vocabulary or
  they do not publish.
- **Every value traces back to a registered source.** A structured file is a
  convenient encoding of a claim, not evidence for it.

### AI-generated structured input

Treat any JSON/YAML that an AI produced — including anything you produce — as
**unverified candidate interpretation** until each item traces to a registered
authoritative source. It gets *more* scrutiny than a PDF, not less, precisely
because it arrives in the shape the pipeline wants.

Watch for self-declared uncertainty in the file itself: keys along the lines of
`still_unverified_todo` or `deprecated_do_not_build` are the author telling you
which parts are not ready. Honour them. A machine-readable file that says it is
unverified is still unverified.

Such a file may **never** override legislation, regulations, official guidance
or governed reference data merely because it parses cleanly.

## Routing summary

| Format | May become | Never |
|---|---|---|
| PDF | registered source, citations, hand-authored drafts traceable to locators | executable logic, runtime read, retrieval authority |
| CSV | governed reference data, structured rows | float values, inferred schema, in-place correction of published data |
| JSON / YAML | candidate manifest, draft knowledge | authority by itself, unknown fields, arbitrary expressions |

## Non-negotiables at runtime

Whatever the route, the published result must satisfy all of:

- no runtime PDF read
- no runtime web lookup
- no runtime AI call
- no arbitrary executable logic
- no production object without governed provenance

These are the five zeros in the batch report. A non-zero value is not a metric
that drifted; it means something bypassed the pipeline.
