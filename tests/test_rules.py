"""Unit tests for leak rules L1-L5, using small hand-made contracts and invoice lines."""

from datetime import date

import pytest

from src.detect_leaks import rule_discount, rule_overage, rule_price, rule_tax


def make_contract(**overrides):
    """A one-product contract: 10 seats at USD 100, +5% from 1 Jan 2025, Germany."""
    line = {"product_code": "PLAT-BASE", "product_name": "Platform licence (per seat)",
            "quantity": 10, "unit_price": 100.0, "discount_pct": None, "discount_end_date": None,
            "tier_limit": None, "overage_rate": None}
    line.update(overrides.pop("line", {}))
    contract = {"contract_id": "K1", "customer_name": "Test Co GmbH", "country": "DE",
                "start_date": date(2024, 1, 1), "end_date": date(2026, 12, 31),
                "escalation_pct": 5.0, "escalation_date": date(2025, 1, 1),
                "lines": [line], "confidence": {}, "evidence": {"escalation_pct": "Fees rise 5% yearly."}}
    contract.update(overrides)
    contract["by_code"] = {line["product_code"]: line}
    return contract, line


def make_invoice(**overrides):
    inv = {"invoice_line_id": "L1", "invoice_id": "INV-1", "invoice_date": date(2024, 6, 1),
           "customer_name": "TEST CO", "product_code": "PLAT-BASE", "quantity": 10,
           "unit_price": 100.0, "discount_amount": 0.0, "net_amount": 1000.0, "tax_rate": 0.19}
    inv.update(overrides)
    return inv


# ---------- L1 missed escalation / L4 wrong price ----------

def test_correct_price_before_escalation_is_not_flagged():
    contract, line = make_contract()
    assert rule_price(make_invoice(), contract, line) is None


def test_correct_escalated_price_is_not_flagged():
    contract, line = make_contract()
    inv = make_invoice(invoice_date=date(2025, 3, 1), unit_price=105.0, net_amount=1050.0)
    assert rule_price(inv, contract, line) is None


def test_l1_old_price_after_escalation_date():
    contract, line = make_contract()
    inv = make_invoice(invoice_date=date(2025, 1, 1))          # escalation date itself counts
    flag = rule_price(inv, contract, line)
    assert flag["rule_id"] == "L1"
    assert flag["impact_usd"] == pytest.approx(50.0)           # (105 - 100) x 10
    assert flag["direction"] == "Under-billed"


def test_l1_second_year_compounds():
    contract, line = make_contract()
    inv = make_invoice(invoice_date=date(2026, 2, 1), unit_price=105.0)   # year-1 price in year 2
    flag = rule_price(inv, contract, line)
    assert flag["rule_id"] == "L1"
    assert flag["expected_amount"] == pytest.approx(1102.5)   # 100 x 1.05^2 = 110.25 x 10


def test_l4_keying_error():
    contract, line = make_contract()
    inv = make_invoice(unit_price=90.0, net_amount=900.0)
    flag = rule_price(inv, contract, line)
    assert flag["rule_id"] == "L4"
    assert flag["impact_usd"] == pytest.approx(100.0)


def test_l4_not_l1_when_price_is_only_close_to_old_price():
    """A 5% keying cut after a 5% escalation lands near, not on, last year's price."""
    contract, line = make_contract()
    inv = make_invoice(invoice_date=date(2025, 6, 1), unit_price=99.75)   # 105 x 0.95
    assert rule_price(inv, contract, line)["rule_id"] == "L4"


def test_difference_within_half_percent_is_tolerated():
    contract, line = make_contract()
    assert rule_price(make_invoice(unit_price=99.6), contract, line) is None


def test_price_too_high_is_over_billing():
    contract, line = make_contract()
    flag = rule_price(make_invoice(unit_price=110.0), contract, line)
    assert flag["direction"] == "Over-billed"
    assert flag["impact_usd"] == pytest.approx(-100.0)


# ---------- L2 expired discount ----------

def test_active_discount_is_not_flagged():
    contract, line = make_contract(line={"discount_pct": 10.0, "discount_end_date": date(2024, 6, 30)})
    inv = make_invoice(invoice_date=date(2024, 6, 1), discount_amount=100.0, net_amount=900.0)
    assert rule_discount(inv, contract, line) is None


def test_l2_discount_after_end_date():
    contract, line = make_contract(line={"discount_pct": 10.0, "discount_end_date": date(2024, 6, 30)})
    inv = make_invoice(invoice_date=date(2024, 7, 1), discount_amount=100.0, net_amount=900.0)
    flag = rule_discount(inv, contract, line)
    assert flag["rule_id"] == "L2"
    assert flag["impact_usd"] == pytest.approx(100.0)


def test_l2_discount_with_no_discount_clause():
    contract, line = make_contract()
    inv = make_invoice(discount_amount=50.0, net_amount=950.0)
    flag = rule_discount(inv, contract, line)
    assert flag["rule_id"] == "L2" and "no discount" in flag["rule_detail"]


def test_missing_discount_owed_is_over_billing():
    contract, line = make_contract(line={"discount_pct": 10.0, "discount_end_date": date(2024, 12, 31)})
    flag = rule_discount(make_invoice(), contract, line)
    assert flag["direction"] == "Over-billed"
    assert flag["impact_usd"] == pytest.approx(-100.0)


# ---------- L3 unbilled overage ----------

def _metered():
    return make_contract(line={"product_code": "API-CALLS", "product_name": "API calls bundle",
                               "quantity": 1, "unit_price": 900.0,
                               "tier_limit": 100_000, "overage_rate": 0.006})


def test_l3_usage_over_tier_without_overage_line():
    contract, line = _metered()
    usage = {"contract_id": "K1", "product_code": "API-CALLS", "period": "2024-06", "units_used": 120_000}
    flag = rule_overage(usage, contract, line, make_invoice(product_code="API-CALLS"), 0.0)
    assert flag["rule_id"] == "L3"
    assert flag["impact_usd"] == pytest.approx(120.0)          # 20,000 x 0.006


def test_overage_billed_in_full_is_not_flagged():
    contract, line = _metered()
    usage = {"contract_id": "K1", "product_code": "API-CALLS", "period": "2024-06", "units_used": 120_000}
    assert rule_overage(usage, contract, line, make_invoice(), 120.0) is None


def test_usage_within_tier_is_not_flagged():
    contract, line = _metered()
    usage = {"contract_id": "K1", "product_code": "API-CALLS", "period": "2024-06", "units_used": 90_000}
    assert rule_overage(usage, contract, line, make_invoice(), 0.0) is None


def test_l3_without_any_invoice_keeps_a_traceable_key():
    contract, line = _metered()
    usage = {"contract_id": "K1", "product_code": "API-CALLS", "period": "2024-06", "units_used": 150_000}
    flag = rule_overage(usage, contract, line, None, 0.0)
    assert flag["invoice_line_id"] == "K1-API-CALLS-2024-06"


# ---------- L5 wrong tax rate ----------

def test_correct_tax_rate_is_not_flagged():
    contract, _ = make_contract()
    assert rule_tax(make_invoice(tax_rate=0.19), contract) is None


def test_l5_under_charged_tax():
    contract, _ = make_contract(country="AE")                  # UAE VAT 5%
    flag = rule_tax(make_invoice(tax_rate=0.0), contract)
    assert flag["rule_id"] == "L5"
    assert flag["impact_usd"] == pytest.approx(50.0)           # (0.05 - 0) x 1000


def test_l5_over_charged_tax_is_over_billing():
    contract, _ = make_contract(country="AE")
    flag = rule_tax(make_invoice(tax_rate=0.10), contract)     # README example: 10% instead of 5%
    assert flag["direction"] == "Over-billed"
    assert flag["impact_usd"] == pytest.approx(-50.0)


# ---------- evidence and confidence ----------

def test_flag_carries_evidence_and_lowest_field_confidence():
    contract, line = make_contract()
    contract["confidence"] = {"PLAT-BASE.unit_price": 1.0, "escalation_pct": 0.9, "escalation_date": 0.7}
    flag = rule_price(make_invoice(invoice_date=date(2025, 2, 1)), contract, line)
    assert flag["extraction_confidence"] == 0.7
    assert "Fees rise 5% yearly." in flag["evidence_clause"]
