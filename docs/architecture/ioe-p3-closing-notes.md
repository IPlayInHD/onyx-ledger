# IOE Phase P3 — Closing Notes

Two details recorded before P3 closes. Both concern migration `0029`
(`23_ioe_support_scores.sql`).

---

## 1. Exact conversion: `display_support_score` → `confidence_score`

### The conversion

```sql
NEW.confidence_score := round(NEW.display_support_score)::smallint;
```

Stated precisely, in two separate steps:

| Step | Operation | Rounding |
|---|---|---|
| 1 | `round(numeric)` — no scale argument, i.e. round to **0 decimal places** | **Half away from zero** |
| 2 | `::smallint` — cast of an already-integral numeric | **None. Exact.** |

**No implicit cast rounding is relied upon.** The rounding is performed by the explicit `round()` call in step 1, which returns a numeric with scale 0. The `::smallint` cast in step 2 therefore converts a value that is already a whole number, and is exact by construction. Writing `display_support_score::smallint` — which *would* rely on the cast's implicit rounding — is not used and must not be introduced.

### Pinned rounding mode

`round(numeric)` in PostgreSQL rounds **half away from zero**. `display_support_score` is constrained by `candidate_display_support_range` to `[0, 100]`, so it is never negative, and over a non-negative domain **half away from zero is identical to `ROUND_HALF_UP`** — the same mode used by the `Decimal` policy throughout the IOE domain (`canonical.py`, `savings.py`, `confidence.py`).

The two layers therefore agree by construction rather than by coincidence: a support score rounded in Python and the same score rounded in PostgreSQL produce the same integer.

### Verified boundary behaviour

| `display_support_score` | `confidence_score` | Note |
|---|---|---|
| `62.40` | `62` | below the midpoint → down |
| `62.50` | `63` | exact midpoint → **up** (half away from zero) |
| `62.60` | `63` | above the midpoint → up |
| `63.50` | `64` | **up**, not to even — this is *not* banker's rounding |
| `0.50` | `1` | midpoint at the floor of the range |
| `99.50` | `100` | midpoint at the ceiling of the range |
| `100.00` | `100` | maximum, unchanged |
| `0.00` | `0` | minimum, unchanged |

`63.50 → 64` is the discriminating case: banker's rounding (`ROUND_HALF_EVEN`) would produce `64` here but `62` for `62.50`. Both midpoints rounding **up** confirms half-away-from-zero.

Covered by `test_confidence_score_conversion_boundaries` (executed against the live trigger, not asserted in the abstract).

### Range safety

`round(100.00) = 100` fits `smallint` comfortably, and `candidate_confidence_score_check` independently constrains the result to `[0, 100]`. No overflow path exists from a value that satisfies the display range check.

---

## 2. What the CHECK constraint actually does

The P3 commit described `candidate_confidence_matches_display` as catching "a direct SQL write that somehow bypassed the derivation." **That framing was imprecise and is corrected here.**

### Ordinary direct SQL does *not* bypass the trigger

A `BEFORE INSERT OR UPDATE` trigger fires for every ordinary write, whatever the client: `psql`, another service, an ad-hoc script, or a migration. There is no "direct SQL" path that skips it in normal operation. Any such write has its `confidence_score` **derived**, and a supplied value is simply overwritten — this is verified by `test_confidence_score_cannot_diverge_from_display`, which submits `99` alongside `62.40` and observes `62` persisted.

### The CHECK is an independent row-level invariant

Its role is not to catch a bypass of the trigger. It constrains the **row itself**, independently of how the row came to exist:

```sql
CHECK (display_support_score IS NULL
       OR confidence_score IS NULL
       OR confidence_score = round(display_support_score)::smallint)
```

This holds even when the derivation does not run:

| Situation | Trigger runs? | CHECK still enforces? |
|---|---|---|
| Ordinary INSERT/UPDATE from any client | **Yes** | Yes |
| `ALTER TABLE … DISABLE TRIGGER` (table owner / superuser) | No | **Yes** |
| `SET session_replication_role = 'replica'` (superuser; logical-replication apply path) | No | **Yes** |
| Trigger dropped or its function replaced by a faulty version in a later migration | No / incorrectly | **Yes** |
| Restore or bulk load that disables triggers for speed | No | **Yes** |

### The two mechanisms have different jobs

- **Trigger — derivation.** Guarantees the value is *produced* correctly, so application code cannot write a conflicting one even by mistake. This is the everyday guarantee.
- **CHECK — invariant.** Guarantees no row can *exist* in a divergent state, regardless of which path created it. This is the guarantee that survives the trigger being disabled, dropped, or replaced.

This is defence in depth in the accurate sense: two controls with genuinely different failure modes, not one control plus a restatement of it. The privileged and exceptional paths listed above are precisely the cases the CHECK exists for — they are unusual, they require elevated rights, and they are exactly when a derivation-only guarantee would silently lapse.

### Residual limitation, stated plainly

A `CHECK` constraint is validated on write. It does **not** re-validate existing rows if the constraint itself is later dropped, nor does it prevent a superuser from adding rows with `ALTER TABLE … DISABLE TRIGGER ALL` *and* dropping the constraint. No table-level control defends against a superuser acting deliberately; that boundary is covered by role separation and audit (architecture §25), not by constraints.
