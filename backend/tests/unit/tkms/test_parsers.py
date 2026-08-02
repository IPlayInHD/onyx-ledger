"""Parser framework — golden output, format equivalence, registry, safety."""
from decimal import Decimal

import pytest

from app.core.exceptions import NotFound, ValidationError
from app.services.tkms.domain.models import ExtractedRule
from app.services.tkms.parsers import build_default_registry
from app.services.tkms.parsers.csv_parser import GovernmentCsvParser
from app.services.tkms.parsers.json_parser import GovernmentJsonParser
from app.services.tkms.parsers.manual_parser import ManualRuleParser
from app.services.tkms.parsers.xml_parser import GovernmentXmlParser

# The same logical rule expressed in each flat format.
CSV = (
    "code,name,category,jurisdiction,tax_year,max_amount,reduction_rate,source_url\n"
    "MEDICAL,Medical credit,credit,FED,2025,2759,0.03,https://cra/med\n"
)
JSON = (
    '[{"rule_code":"MEDICAL","name":"Medical credit","category":"credit",'
    '"jurisdiction":"FED","tax_year":2025,"max_amount":"2759",'
    '"reduction_rate":"0.03","source_url":"https://cra/med"}]'
)
XML = (
    "<rules><rule><code>MEDICAL</code><name>Medical credit</name>"
    "<category>credit</category><jurisdiction>FED</jurisdiction>"
    "<tax_year>2025</tax_year><max_amount>2759</max_amount>"
    "<reduction_rate>0.03</reduction_rate><source_url>https://cra/med</source_url>"
    "</rule></rules>"
)


def _only(parser, text) -> ExtractedRule:
    rs = parser.extract_rules(text)
    assert rs.warnings == (), rs.warnings
    assert len(rs) == 1
    return rs.rules[0]


def test_flat_formats_produce_identical_contract():
    r_csv = _only(GovernmentCsvParser(), CSV)
    r_json = _only(GovernmentJsonParser(), JSON)
    r_xml = _only(GovernmentXmlParser(), XML)
    # identical canonical payloads → format-independent contract
    assert r_csv.as_payload() == r_json.as_payload() == r_xml.as_payload()
    assert r_csv.rule_code == "MEDICAL"
    assert r_csv.category == "credit"
    assert r_csv.tax_year == 2025
    assert r_csv.max_amount == Decimal("2759")
    assert r_csv.reduction_rate == Decimal("0.03")
    # effective_date defaults to Jan 1 of the tax year at payload/promotion time
    assert r_csv.effective_date is None
    assert r_csv.effective_date_or_default() == "2025-01-01"
    assert r_csv.as_payload()["effective_date"] == "2025-01-01"


def test_json_carries_formula_and_conditions():
    payload = (
        '{"rule_code":"RRSP_DED","name":"RRSP deduction","category":"deduction",'
        '"jurisdiction":"FED","tax_year":2025,'
        '"formula":{"code":"RRSP_2025","expression":"min income 0.18 *","inputs":[["income","income.total"]]},'
        '"eligibility_conditions":[{"fact_key":"profile.age","operator":"lt","value_type":"number","value_number":"71"}]}'
    )
    r = _only(GovernmentJsonParser(), payload)
    assert r.formula is not None
    assert r.formula.code == "RRSP_2025"
    assert r.formula.inputs == (("income", "income.total"),)
    assert len(r.eligibility_conditions) == 1
    assert r.eligibility_conditions[0].fact_key == "profile.age"
    assert r.eligibility_conditions[0].operator == "lt"


def test_manual_parser_accepts_canonical_payload_roundtrip():
    original = ExtractedRule(
        rule_code="TFSA_LIMIT", name="TFSA limit", category="limit",
        jurisdiction="FED", tax_year=2025, contribution_limit=Decimal("7000"),
    )
    import json

    text = json.dumps([original.as_payload()])
    r = _only(ManualRuleParser(), text)
    assert r.rule_code == "TFSA_LIMIT"
    assert r.contribution_limit == Decimal("7000")
    assert r.confidence == Decimal("1.0")   # human-authored


def test_missing_required_field_is_reported_not_crashed():
    rs = GovernmentCsvParser().extract_rules(
        "code,name,category,jurisdiction\nX,X,credit,FED\n"  # no tax_year
    )
    assert len(rs) == 0
    assert any("tax_year" in w for w in rs.warnings)


def test_bad_category_rejected():
    rs = GovernmentJsonParser().extract_rules(
        '[{"rule_code":"X","name":"X","category":"nonsense","jurisdiction":"FED","tax_year":2025}]'
    )
    assert len(rs) == 0
    assert any("invalid category" in w for w in rs.warnings)


def test_rate_out_of_range_warns_but_keeps_rule():
    rs = GovernmentCsvParser().extract_rules(
        "code,name,category,jurisdiction,tax_year,reduction_rate\nX,X,credit,FED,2025,1.5\n"
    )
    assert len(rs) == 1
    assert any("outside 0..1" in w for w in rs.warnings)


def test_xml_external_entity_is_disabled():
    xxe = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        "<rules><rule><code>&x;</code></rule></rules>"
    )
    with pytest.raises(ValidationError):
        GovernmentXmlParser().extract_rules(xxe)


def test_invalid_json_raises_validation_error():
    with pytest.raises(ValidationError):
        GovernmentJsonParser().extract_rules("{not json")


# ---- registry ----
def test_registry_resolves_by_source_and_format():
    reg = build_default_registry()
    assert reg.resolve(source="generic", fmt="csv").name == "government_csv"
    assert reg.resolve(source="cra", fmt="pdf").name == "cra_pdf"
    # unknown source falls back to a generic parser for the format
    assert reg.resolve(source="unknown", fmt="json").name == "government_json"


def test_registry_resolve_by_name_and_version():
    reg = build_default_registry()
    p = reg.resolve_by_name("government_csv", "1.0.0")
    assert p.version == "1.0.0"
    with pytest.raises(NotFound):
        reg.resolve_by_name("government_csv", "9.9.9")
    with pytest.raises(NotFound):
        reg.resolve_by_name("does_not_exist")


def test_registry_unknown_format_raises():
    reg = build_default_registry()
    with pytest.raises(NotFound):
        reg.resolve(source="generic", fmt="parquet")


def test_stub_parsers_are_registered_but_not_implemented():
    reg = build_default_registry()
    names = {p["name"] for p in reg.available()}
    assert {"cra_html", "cra_pdf", "finance_html", "provincial_html"} <= names
    with pytest.raises(NotImplementedError):
        reg.resolve_by_name("cra_html").extract_rules("<html></html>")
