"""
Rule engine for leak types L1-L5 (FR-06 to FR-08), as defined in the BRD
section "Leak types and business rules".

  L1 Missed price escalation  invoice on/after the escalation date, billed at an
                              earlier year's price          impact (expected - billed) x qty
  L2 Expired discount         discount on a line dated after the discount end date
                                                            impact = discount amount
  L3 Unbilled overage         usage above the tier with no (or too small) overage line
                                                            impact (usage - tier) x rate - overage billed
  L4 Wrong unit price         price differs from the expected contract price by > 0.5%
                              and no escalation or discount explains it
                                                            impact (expected - billed) x qty
  L5 Wrong tax rate           tax rate differs from the customer's billing-country rate
                                                            impact (correct - billed rate) x net

General rules: over-billing (negative impact) is reported separately as a
customer-relations risk; flags under USD 10 are logged but hidden; each flag
carries the contract clause it relied on; priority = impact x confidence.

Each rule is a small pure function (invoice line + contract terms in, flag or
None out) so it can be unit-tested with hand-made examples.
"""

import pandas as pd

from src import config
from src.common import (ensure_output_dirs, escalations_applied, load_extracted_contracts,
                        load_invoices, load_usage, money, parse_date, price_after)

REVIEWS_CSV = config.DATA_DIR / "flag_reviews.csv"
VALID_STATUSES = {"Unreviewed", "Confirmed", "False alarm", "Needs info"}


# ==========================================================================
# Flag construction
# ==========================================================================

def make_flag(rule_id: str, inv: dict | None, contract: dict, product_code: str,
              expected: float, billed: float, fields: list[str], detail: str,
              extra_evidence: str | None = None) -> dict:
    """Build one flag row. `fields` are the extracted fields the rule relied on."""
    impact = money(expected - billed)
    conf_values = [contract["confidence"].get(f, 1.0) for f in fields]
    evidence = [contract["evidence"].get(f) for f in fields]
    if extra_evidence:
        evidence.append(extra_evidence)
    evidence = " | ".join(dict.fromkeys(e for e in evidence if e))   # unique, in order
    return {
        "rule_id": rule_id,
        "invoice_line_id": inv["invoice_line_id"] if inv else None,
        "invoice_id": inv["invoice_id"] if inv else None,
        "invoice_date": inv["invoice_date"] if inv else None,
        "contract_id": contract["contract_id"],
        "customer": contract["customer_name"],
        "country": contract["country"],
        "product_code": product_code,
        "expected_amount": money(expected),
        "billed_amount": money(billed),
        "impact_usd": impact,
        "direction": "Under-billed" if impact > 0 else "Over-billed",
        "extraction_confidence": round(min(conf_values) if conf_values else 1.0, 2),
        "evidence_clause": evidence,
        "rule_detail": detail,
    }


# ==========================================================================
# The rules
# ==========================================================================

def rule_price(inv: dict, contract: dict, cl: dict) -> dict | None:
    """L1 (missed escalation) or L4 (wrong price); over-billing if the price is too high."""
    base, pct = cl.get("unit_price"), contract.get("escalation_pct")
    if not base:
        return None
    years = escalations_applied(contract.get("escalation_date"), inv["invoice_date"])
    expected = price_after(base, pct, years)
    billed = inv["unit_price"]
    if abs(billed - expected) / expected <= config.PRICE_TOLERANCE:
        return None

    qty = inv["quantity"]
    fields = [f"{cl['product_code']}.unit_price", "escalation_pct", "escalation_date"]
    if billed < expected:
        for k in range(years):              # billed at an earlier year's price -> escalation missed
            old = price_after(base, pct, k)
            if abs(billed - old) / old <= config.PRICE_TOLERANCE:
                detail = (f"Billed {billed:.2f} = price before {years - k} escalation(s) of {pct}%; "
                          f"expected {expected:.2f} from {contract['escalation_date']}")
                return make_flag("L1", inv, contract, cl["product_code"], expected * qty,
                                 billed * qty, fields, detail)
    detail = (f"Billed unit price {billed:.2f} vs expected {expected:.2f} "
              f"({(billed - expected) / expected:+.1%}); no escalation or discount explains it")
    return make_flag("L4", inv, contract, cl["product_code"], expected * qty, billed * qty,
                     fields, detail)


def rule_discount(inv: dict, contract: dict, cl: dict) -> dict | None:
    """L2: a discount on a line dated after the discount end date (or with no discount clause)."""
    code = cl["product_code"]
    pct, end = cl.get("discount_pct"), cl.get("discount_end_date")
    gross = inv["quantity"] * inv["unit_price"]
    fields = [f"{code}.discount_pct", f"{code}.discount_end_date"]
    active = bool(pct) and end is not None and inv["invoice_date"] <= end

    if inv["discount_amount"] > 0 and not active:
        detail = (f"Discount {inv['discount_amount']:.2f} applied on {inv['invoice_date']}, "
                  + (f"but the {pct:g}% discount ended {end}" if pct else "but the contract has no discount for this product"))
        extra = None if pct else f"No discount clause for {cl.get('product_name') or code} in {contract['contract_id']}"
        return make_flag("L2", inv, contract, code, gross, inv["net_amount"], fields, detail, extra)

    if active and inv["discount_amount"] == 0:   # customer missed a discount they are owed
        expected = gross * (1 - pct / 100)
        detail = f"{pct:g}% discount valid until {end} was not applied"
        return make_flag("L2", inv, contract, code, expected, inv["net_amount"], fields, detail)
    return None


def rule_tax(inv: dict, contract: dict) -> dict | None:
    """L5: tax rate differs from the standard rate of the customer's billing country."""
    country = contract.get("country")
    if country not in config.TAX_RATES:
        return None
    name, label, correct = config.TAX_RATES[country]
    billed_rate = inv["tax_rate"]
    if abs(billed_rate - correct) <= config.TAX_RATE_TOLERANCE:
        return None
    net = inv["net_amount"]
    detail = f"Tax charged at {billed_rate:.0%}; {name} {label} is {correct:.0%}"
    extra = f"Config: {name} standard {label} rate = {correct:.0%}"
    return make_flag("L5", inv, contract, inv["product_code"], correct * net, billed_rate * net,
                     ["country"], detail, extra)


def rule_overage(usage_row: dict, contract: dict, cl: dict, base_line: dict | None,
                 overage_billed: float) -> dict | None:
    """L3: usage above the tier, with the overage not billed (or billed short)."""
    tier, rate = cl.get("tier_limit"), cl.get("overage_rate")
    if not tier or not rate:
        return None
    over = usage_row["units_used"] - tier
    if over <= 0:
        return None
    expected = money(over * rate)
    if abs(expected - overage_billed) < 0.01:
        return None
    code = cl["product_code"]
    detail = (f"{usage_row['period']}: used {usage_row['units_used']:,} units vs allowance {tier:,} "
              f"-> {over:,} x {rate} = {expected:.2f}; overage billed {overage_billed:.2f}")
    flag = make_flag("L3", base_line, contract, code, expected, overage_billed,
                     [f"{code}.tier_limit", f"{code}.overage_rate"], detail)
    if base_line is None:   # no invoice at all for that month: keep a traceable key
        flag["invoice_line_id"] = f"{contract['contract_id']}-{code}-{usage_row['period']}"
    return flag


# ==========================================================================
# Orchestration
# ==========================================================================

def _prepare_contracts() -> dict[str, dict]:
    contracts = {}
    for c in load_extracted_contracts():
        for k in ("start_date", "end_date", "escalation_date"):
            c[k] = parse_date(c[k])
        for ln in c["lines"]:
            ln["discount_end_date"] = parse_date(ln["discount_end_date"])
        c["by_code"] = {ln["product_code"]: ln for ln in c["lines"]}
        contracts[c["contract_id"]] = c
    return contracts


def _apply_reviews(flags: pd.DataFrame) -> pd.DataFrame:
    """Bring in reviewer decisions from data/flag_reviews.csv (FR-09 / US-04)."""
    if not REVIEWS_CSV.exists():
        REVIEWS_CSV.write_text("flag_id,status,reviewed_by,reviewed_on,note\n", encoding="utf-8")
    reviews = pd.read_csv(REVIEWS_CSV, dtype=str)
    reviews = reviews[reviews["status"].isin(VALID_STATUSES)].drop_duplicates("flag_id", keep="last")
    flags = flags.drop(columns=["status"], errors="ignore").merge(
        reviews[["flag_id", "status", "reviewed_by", "note"]], on="flag_id", how="left")
    flags["status"] = flags["status"].fillna("Unreviewed")
    return flags


def detect(invoices: pd.DataFrame, contract_map: pd.DataFrame, contracts: dict[str, dict],
           usage: pd.DataFrame) -> pd.DataFrame:
    """Run L1-L5 over every matched invoice line and every usage period."""
    df = invoices.merge(contract_map[["invoice_line_id", "contract_id", "match_score"]],
                        on="invoice_line_id")
    df = df[df["contract_id"].notna()]
    flags = []

    # L1, L2, L4, L5: line by line
    for inv in df.to_dict("records"):
        contract = contracts.get(inv["contract_id"])
        if contract is None:
            continue
        match_conf = (inv["match_score"] or 100) / 100
        line_flags = [rule_tax(inv, contract)]
        if not inv["product_code"].endswith("-OVG"):
            cl = contract["by_code"].get(inv["product_code"])
            if cl:
                line_flags += [rule_price(inv, contract, cl), rule_discount(inv, contract, cl)]
        for f in line_flags:
            if f:
                f["invoice_customer_name"] = inv["customer_name"]
                f["confidence"] = round(min(f["extraction_confidence"], match_conf), 2)
                flags.append(f)

    # L3: period by period from the usage feed
    df["period"] = pd.to_datetime(df["invoice_date"]).dt.strftime("%Y-%m")
    lines_by_key = {}
    for inv in df.to_dict("records"):
        lines_by_key.setdefault((inv["contract_id"], inv["period"]), []).append(inv)
    for u in usage.to_dict("records"):
        contract = contracts.get(u["contract_id"])
        if contract is None:
            continue
        cl = contract["by_code"].get(u["product_code"])
        if cl is None:
            continue
        month_lines = lines_by_key.get((u["contract_id"], u["period"]), [])
        base = next((x for x in month_lines if x["product_code"] == u["product_code"]), None)
        ovg = sum(x["net_amount"] for x in month_lines if x["product_code"] == u["product_code"] + "-OVG")
        f = rule_overage(u, contract, cl, base, ovg)
        if f:
            f["invoice_customer_name"] = base["customer_name"] if base else None
            match_conf = (base["match_score"] / 100) if base else 1.0
            f["confidence"] = round(min(f["extraction_confidence"], match_conf), 2)
            flags.append(f)

    out = pd.DataFrame(flags)
    out["leak_type"] = out["rule_id"].map(lambda r: config.LEAK_TYPES[r][0])
    out["priority_score"] = (out["impact_usd"].abs() * out["confidence"]).round(2)
    out["below_threshold"] = out["impact_usd"].abs() < config.MIN_FLAG_USD
    out["flag_id"] = out["rule_id"] + "-" + out["invoice_line_id"].astype(str)
    out = _apply_reviews(out)
    cols = ["flag_id", "invoice_line_id", "invoice_id", "invoice_date", "customer", "invoice_customer_name",
            "contract_id", "country", "product_code", "rule_id", "leak_type", "direction",
            "expected_amount", "billed_amount", "impact_usd", "confidence", "extraction_confidence",
            "priority_score", "evidence_clause", "rule_detail", "below_threshold", "status",
            "reviewed_by", "note"]
    return out[cols].sort_values("priority_score", ascending=False).reset_index(drop=True)


def run() -> pd.DataFrame:
    ensure_output_dirs()
    contract_map = pd.read_csv(config.OUTPUT_DIR / "invoice_contract_map.csv",
                               dtype={"invoice_line_id": str})
    flags = detect(load_invoices(), contract_map, _prepare_contracts(), load_usage())

    flags.to_csv(config.OUTPUT_DIR / "flags_all.csv", index=False)                 # full log
    visible = flags[~flags["below_threshold"]]
    leakage = visible[visible["direction"] == "Under-billed"]
    over = visible[visible["direction"] == "Over-billed"]
    leakage.to_csv(config.OUTPUT_DIR / "flags.csv", index=False)                    # default view
    over.to_csv(config.OUTPUT_DIR / "overbilling_flags.csv", index=False)
    confirmed = leakage[leakage["status"] == "Confirmed"]                           # US-08 export
    confirmed[["invoice_id", "invoice_line_id", "customer", "rule_id", "leak_type", "expected_amount",
               "billed_amount", "impact_usd", "evidence_clause"]].rename(
        columns={"impact_usd": "difference"}).to_csv(
        config.OUTPUT_DIR / "confirmed_flags_export.csv", index=False)

    print(f"Flags logged: {len(flags):,} | hidden (< USD {config.MIN_FLAG_USD:.0f}): "
          f"{flags['below_threshold'].sum():,}")
    print(f"Leakage flags (default view): {len(leakage):,}  USD {leakage['impact_usd'].sum():,.0f}")
    print(f"Over-billing flags (reported separately): {len(over):,}  "
          f"USD {-over['impact_usd'].sum():,.0f}")
    summary = leakage.groupby("rule_id")["impact_usd"].agg(["count", "sum"]).round(0)
    print(summary.to_string())
    return flags


if __name__ == "__main__":
    run()
