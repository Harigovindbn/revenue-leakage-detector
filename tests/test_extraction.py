"""Tests for the regex extractor and the grounding check that protects against LLM errors."""

from datetime import date

from src.extract_terms import RegexExtractor, finalize

CONTRACT = """MASTER SUBSCRIPTION AGREEMENT

Agreement No.: K99999
Customer: Test Labs Pty Ltd
Registered country: Australia
Effective Date: 01 March 2024
Term: until 28 February 2027

1. SERVICES AND FEES
- Platform licence (per seat) [PLAT-BASE]: 20 units at USD 42.50 per unit per month
- API calls bundle [API-CALLS]: USD 880.00 per month

2. PRICE ADJUSTMENT
Fees shall increase by 4% on each anniversary of the Effective Date.

3. DISCOUNTS
Introductory pricing: Platform licence (per seat) is discounted by 15 percent through 31 August 2024, after which standard rates apply.

4. USAGE ALLOWANCES
The monthly allowance for API calls bundle is 250,000 units; excess units are charged at 0.006 USD each.
"""


def extract():
    return finalize("K99999", RegexExtractor().extract(CONTRACT), CONTRACT, "regex")


def test_regex_reads_header_and_lines():
    r = extract()
    assert r["customer_name"] == "Test Labs Pty Ltd"
    assert r["country"] == "AU"
    assert r["start_date"] == date(2024, 3, 1)
    lines = {ln["product_code"]: ln for ln in r["lines"]}
    assert lines["PLAT-BASE"]["quantity"] == 20 and lines["PLAT-BASE"]["unit_price"] == 42.50
    assert lines["API-CALLS"]["quantity"] == 1


def test_anniversary_wording_derives_escalation_date():
    r = extract()
    assert r["escalation_pct"] == 4.0
    assert r["escalation_date"] == date(2025, 3, 1)
    assert r["confidence"]["escalation_date"] == 0.9          # derived, not stated


def test_discount_and_tier_linked_to_product_by_name():
    lines = {ln["product_code"]: ln for ln in extract()["lines"]}
    assert lines["PLAT-BASE"]["discount_pct"] == 15.0
    assert lines["PLAT-BASE"]["discount_end_date"] == date(2024, 8, 31)
    assert lines["API-CALLS"]["tier_limit"] == 250_000 and lines["API-CALLS"]["overage_rate"] == 0.006


def test_every_field_has_evidence():
    r = extract()
    assert r["evidence"]["PLAT-BASE.discount_pct"].startswith("Introductory pricing")
    assert all(c >= 0.8 for c in r["confidence"].values())


def test_grounding_caps_a_hallucinated_value():
    """An LLM that invents a price the contract never states must land in the review queue."""
    raw = RegexExtractor().extract(CONTRACT)
    raw["lines"][0]["unit_price"] = 45.00                      # not in the text
    raw["confidence"]["PLAT-BASE.unit_price"] = 0.99           # model claims to be sure
    r = finalize("K99999", raw, CONTRACT, "test")
    assert r["confidence"]["PLAT-BASE.unit_price"] <= 0.5


def test_grounding_catches_an_omitted_clause():
    """If the contract has a tier clause but the extractor returned nothing, confidence is 0."""
    raw = RegexExtractor().extract(CONTRACT)
    api = next(ln for ln in raw["lines"] if ln["product_code"] == "API-CALLS")
    api["tier_limit"] = None
    r = finalize("K99999", raw, CONTRACT, "test")
    assert r["confidence"]["API-CALLS.tier_limit"] == 0.0
