"""The extraction contract: what "read a bill correctly" means.

Everything here is a CANDIDATE. Provider output is untrusted input (plan
§6.1); it becomes a `BillExtractionV1` only through the strict parser, and it
becomes truth only when a person confirms it — nothing in this package may
ever be treated as a statement about what a document actually contains.
"""
