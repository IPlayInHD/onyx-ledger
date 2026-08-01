from decimal import Decimal

from app.services.data_ingestion.pipeline import extract, validate_and_transform


def test_extract_json_csv_xml_equivalent():
    j = '[{"code":"X","name":"X credit","category":"credit","jurisdiction":"FED","tax_year":"2025"}]'
    c = "code,name,category,jurisdiction,tax_year\nX,X credit,credit,FED,2025\n"
    x = ('<rules><rule><code>X</code><name>X credit</name><category>credit</category>'
         '<jurisdiction>FED</jurisdiction><tax_year>2025</tax_year></rule></rules>')
    for payload, fmt in [(j, "json"), (c, "csv"), (x, "xml")]:
        rules, errors = validate_and_transform(extract(payload, fmt))
        assert errors == []
        assert rules[0].code == "X" and rules[0].tax_year == 2025


def test_validation_flags_missing_and_bad():
    rows = [
        {"code": "A", "name": "A", "category": "credit", "jurisdiction": "FED"},  # missing tax_year
        {"code": "B", "name": "B", "category": "nonsense", "jurisdiction": "FED", "tax_year": "2025"},
        {"code": "C", "name": "C", "category": "credit", "jurisdiction": "ON", "tax_year": "1700"},
    ]
    rules, errors = validate_and_transform(rows)
    assert rules == []
    assert len(errors) == 3


def test_rate_out_of_range_warns_but_accepts():
    rows = [{"code": "R", "name": "R", "category": "credit", "jurisdiction": "FED",
             "tax_year": "2025", "reduction_rate": "1.5"}]
    rules, errors = validate_and_transform(rows)
    assert errors == []
    assert rules[0].reduction_rate == Decimal("1.5")
    assert any("outside 0..1" in w for w in rules[0].warnings)
