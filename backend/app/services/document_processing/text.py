"""Turning a stored document into text — or refusing to.

WHAT THIS EXISTS FOR. `extract_fields()` runs regexes over text. Until now the
only way to give it text was for the CALLER to supply it, which meant the one
step the phrase "Onyx read your document" describes did not exist: a browser
posting `fields` would have been the user typing numbers, recorded as though a
document had supplied them. This module is the missing half — bytes in, text
out — so the extractor can be pointed at what the customer actually uploaded.

THE RULE THAT SHAPES IT. A media type this cannot honestly read is REFUSED, by
name, and never returned as empty text. The difference matters more than it
looks: empty text runs through `extract_fields()` perfectly happily and yields
zero fields at zero confidence, which is indistinguishable from "we read your
T4 and it contained nothing". One is a capability gap; the other is a claim
about the customer's document. Refusing keeps them apart.

WHAT IT READS TODAY. `text/plain` and `text/csv`. That is a deliberate first
increment, not an oversight: a PDF with a text layer needs a PDF parser, and a
photograph of a T4 needs real OCR, and those are different problems with
different dependencies, different costs and different honest confidences. They
are named in `UNREADABLE_TODAY` so the gap is a list rather than a surprise.

DECODING IS STRICT. Bytes that are not valid UTF-8 are refused rather than
decoded with replacement characters. Lenient decoding cannot corrupt an ASCII
digit, but it can turn a thousands separator or a currency symbol into
something a regex reads differently, and a wrong amount that looks like a right
one is the worst outcome this pipeline can produce.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.admission.limits import MAX_EXTRACTION_TEXT_BYTES


class DocumentUnreadable(Exception):
    """This document cannot be turned into text.

    Carries a CLOSED reason code and never the document's contents. The text of
    a tax slip is the most sensitive thing in the product, and an exception
    message travels into logs and task failure records — Entry 11A's rule about
    exception text applies here with full force.
    """

    def __init__(self, reason: str, *, media_type: str | None = None) -> None:
        self.reason = reason
        self.media_type = media_type
        super().__init__(reason)


#: Reasons, closed. Anything reaching a consumer surface goes through the
#: lexicon; nothing here is written for a customer to read.
REASON_UNSUPPORTED_MEDIA = "UNSUPPORTED_MEDIA_TYPE"
REASON_NOT_TEXT = "BYTES_ARE_NOT_TEXT"
REASON_TOO_LARGE = "EXTRACTED_TEXT_TOO_LARGE"
REASON_EMPTY = "DOCUMENT_IS_EMPTY"

#: Media types the upload path accepts but this module cannot read yet. Listed
#: so the refusal is a known gap with a name, and so a test can assert that
#: every accepted upload type is either readable or deliberately listed here.
UNREADABLE_TODAY = frozenset({
    "application/pdf",   # needs a PDF text layer parser
    "image/jpeg",        # needs OCR
    "image/png",         # needs OCR
    "image/tiff",        # needs OCR
    "image/heic",        # needs OCR
})

#: What this module can read, and what it calls itself when it does. The engine
#: name is persisted on the extraction row, so it has to say which code produced
#: the text rather than being a generic label.
_TEXT_MEDIA = {
    "text/plain": "text-utf8",
    "text/csv": "text-utf8",
}


@dataclass(frozen=True)
class ExtractedText:
    """Text lifted from a document, and the engine that lifted it."""

    text: str
    engine: str


def _media_of(mime_type: str | None) -> str:
    """`text/plain; charset=utf-8` -> `text/plain`."""
    return (mime_type or "").split(";")[0].strip().lower()


def text_from(mime_type: str | None, data: bytes) -> ExtractedText:
    """Bytes from object storage to text, or `DocumentUnreadable`.

    Bounded by the same `MAX_EXTRACTION_TEXT_BYTES` the API route enforces. A
    25 MB upload is within the storage limit and would be 25 MB of text for the
    regexes to scan; the bound belongs wherever text is produced, not only
    where a client supplies it.
    """
    media = _media_of(mime_type)
    engine = _TEXT_MEDIA.get(media)
    if engine is None:
        raise DocumentUnreadable(REASON_UNSUPPORTED_MEDIA, media_type=media or None)

    if len(data) > MAX_EXTRACTION_TEXT_BYTES:
        raise DocumentUnreadable(REASON_TOO_LARGE, media_type=media)

    try:
        # `utf-8-sig` so a BOM written by a spreadsheet export is consumed
        # rather than becoming the first character of the first field.
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # The exception is re-raised as a closed reason WITHOUT chaining: a
        # UnicodeDecodeError's `str()` includes the offending bytes, which are
        # a fragment of the customer's tax document.
        raise DocumentUnreadable(REASON_NOT_TEXT, media_type=media) from None

    if not decoded.strip():
        raise DocumentUnreadable(REASON_EMPTY, media_type=media)

    return ExtractedText(text=decoded, engine=engine)
