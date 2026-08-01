from decimal import Decimal

from app.services.document_processing.ocr import extract_fields


def test_structured_extraction_high_confidence():
    fields, conf = extract_fields("T4", fields={"employmentIncome": "68000", "taxWithheld": "11800"})
    assert fields["employmentIncome"] == Decimal("68000")
    assert conf == 0.99


def test_text_extraction_t4():
    text = "T4 Statement\nBox 14 Employment income 72,000.00\nBox 22 Income tax deducted 12,500.00"
    fields, conf = extract_fields("T4", text=text)
    assert fields["employmentIncome"] == Decimal("72000.00")
    assert conf >= 0.75  # the expected box was found


def test_text_extraction_medical_receipt():
    fields, conf = extract_fields("MEDICAL", text="Pharmacy receipt\nTotal: $3,000.00")
    assert fields["medicalExpenses"] == Decimal("3000.00")


def test_unknown_type_returns_empty():
    fields, conf = extract_fields("UNKNOWN", text="whatever")
    assert fields == {} and conf == 0.0
