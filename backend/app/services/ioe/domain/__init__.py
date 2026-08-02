"""IOE pure domain — no I/O, no clock, no framework, no generated identifiers.

Determinism here is total: given identical canonical inputs, these functions
produce identical outputs. The stateful orchestration that stores results,
evaluates rules, caches, invokes the engine, and manages workflow state lives in
the service layer, not in this package.
"""
