"""BillShield — bill reading, tracking, and savings, isolated from tax code.

Slice 1 contains only the extraction contract and the private evaluation
harness. The import rules for this package are executable, not prose:
`tests/security/test_billshield_import_boundary.py` enforces that these
modules stay off the tax engines, tax IOE orchestration, document processing,
persistence, API, and worker surfaces, with exactly one deliberate exception —
the repository's sole canonicalization authority,
`app.services.ioe.domain.canonical` (integration plan §5.2).
"""
