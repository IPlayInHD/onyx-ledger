"""Keys under which user-authored display text travels in `DisplayContextV1`.

Anything under these keys is DATA a user typed — a label, a note, a nickname.
It is never an instruction to a renderer, and the deterministic renderer does
not emit it at all. A model-backed provider receives it only inside clearly
delimited data sections, and the leakage validator rejects output that echoes
it verbatim.
"""
UNTRUSTED_SCENARIO_LABEL_KEY = "scenario.label"
UNTRUSTED_SCENARIO_NOTE_KEY = "scenario.note"
