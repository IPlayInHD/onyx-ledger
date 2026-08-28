"""The private evaluation harness — §10's launch dependency.

Everything here runs offline against an EXTERNAL corpus root the caller names
explicitly. The repository holds only what §10.1 permits: the schema, the
synthetic fixtures, a content-addressed manifest with opaque ids, aggregate
reports, and this tooling. No real bill, label content, filename, or customer
data may enter git, logs, reports, CI artifacts, or an exception message.
"""
