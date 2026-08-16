# Defect taxonomy

Classify a significant unexpected failure **before** changing any code. The
classification determines what a legitimate fix looks like, and skipping it is
how a real production defect gets "fixed" by weakening the test that caught it.

| Class | Definition |
|---|---|
| `PRODUCTION_DEFECT` | Shipped code produces a wrong or unsafe result for a real user |
| `DESIGN_DEFECT` | The architecture cannot express what is required; no local fix is correct |
| `COMPATIBILITY_DEFECT` | A change breaks a historical version, sealed artifact, or golden contract |
| `PRIVACY_DEFECT` | Data is retained, exposed, or purged contrary to governed policy |
| `SECURITY_DEFECT` | Tenancy, privilege, or access boundary can be violated |
| `TEST_DEFECT` | The test asserts something the contract never promised, or depends on state it did not establish |
| `GATE_DEFECT` | The certification harness itself is wrong — bad restore, missing reset, wrong scope |
| `PERFORMANCE_DEFECT` | Correct results at unacceptable cost: N+1, accidental O(n²), row-by-row writes |
| `POLICY` | Not a defect; a governed decision that must be made or confirmed by the user |

## How to classify honestly

**Do not fix a test defect by changing correct production semantics.** If the
test asserts something the contract never promised, the test is wrong — say so
and fix the test, with a regression that pins the corrected expectation.

**Do not label a production defect a test or gate defect because CI found it.**
Where the failure was discovered says nothing about where the defect lives.

**"It does not reproduce" is evidence, not a verdict.** Report the number of
attempts and the conditions. A single non-reproducing failure in a genuine
safety assertion stays open as an observation; it does not become a flake
because re-running was convenient.

**Measure before you claim a mechanism.** A plausible root cause that you have
not tested is a hypothesis. Shipping a fix for an unmeasured hypothesis produces
a change that fixes nothing while claiming a cause — and it removes the evidence
that would have found the real one.

## What to do with an unrelated defect

Record it, classify it, and leave it alone. Fix it only if it blocks this
entry's certification, or the user explicitly authorizes the work.

This matters most during certification: an unrelated edit while a gate is
running invalidates the run, because the gate no longer describes the tree you
are about to certify.
