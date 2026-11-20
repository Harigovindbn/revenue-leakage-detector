"""
Power BI export (FR-10, FR-11): clean star-schema tables in output/powerbi/.

    fact_flags          one row per flag (leakage and over-billing, incl. hidden < USD 10)
    fact_invoice_lines  one row per invoice line, with match status and flag count
    dim_customer        customer master + country + tax rate
    dim_contract        extracted contract terms + clause wording family (US-06)
    dim_leak_type       L1-L5 with severity, rule and the process owner
    dim_date            calendar table (year, quarter, month)

Keys: date_key (YYYY-MM-DD), customer_key, contract_id, rule_id.
"""

import pandas as pd

from src import config
from src.common import ensure_output_dirs, load_customers, load_extracted_contracts, load_invoices
from src.match_customers import name_key

UNKNOWN = "UNMATCHED"

LEAK_TYPE_DETAILS = {
    # rule: (rule text, impact formula, process owner who can fix the cause)
    "L1": ("Invoice on/after escalation date billed at an earlier year's price",
           "(expected price - billed price) x quantity", "Billing ops: price-change calendar"),
    "L2": ("Discount still on the invoice after the discount end date",
           "discount amount on the line", "Billing ops: discount end-date setup"),
    "L3": ("Usage above the contracted tier with no overage line",
           "(usage - tier limit) x overage rate", "Billing ops: usage-to-invoice feed"),
    "L4": ("Unit price differs from contract price by > 0.5% with no explanation",
           "(contract price - billed price) x quantity", "Billing ops: price master data entry"),
    "L5": ("Tax rate differs from the customer's billing-country rate",
           "(correct rate - billed rate) x net amount", "Tax team: customer tax setup"),
}


def clause_family(text: str | None, kind: str) -> str:
    """Name the wording family of a clause, so leakage can be grouped by contract template."""
    if not text:
        return "None"
    t = text.lower()
    if kind == "escalation":
        for key, label in [("each anniversary", "A: 'increase on each anniversary' (no date)"),
                           ("annually thereafter", "B: 'on <date> and annually thereafter'"),
                           ("uplift", "C: 'annual uplift ... first anniversary (<date>)'"),
                           ("yearly adjustment", "D: 'yearly adjustment commencing <date>'")]:
            if key in t:
                return label
    else:
        for key, label in [("promotional", "A: 'promotional discount ... until <date>'"),
                           ("on or before", "B: '% off ... invoices dated on or before <date>'"),
                           ("introductory", "C: 'introductory pricing ... through <date>'")]:
            if key in t:
                return label
    return "Other wording"


def build_dim_customer(contracts: list[dict]) -> pd.DataFrame:
    cust = load_customers().rename(columns={"customer_id": "customer_key", "name": "customer_name",
                                            "billing_country": "country_code"})
    cust["country_name"] = cust["country_code"].map(lambda c: config.TAX_RATES[c][0])
    cust["tax_label"] = cust["country_code"].map(lambda c: config.TAX_RATES[c][1])
    cust["standard_tax_rate"] = cust["country_code"].map(lambda c: config.TAX_RATES[c][2])
    cust["contracts"] = cust["customer_name"].map(
        lambda n: sum(1 for c in contracts if c["customer_name"] == n))
    unknown = pd.DataFrame([{"customer_key": UNKNOWN, "customer_name": "(no matching contract)",
                             "country_code": None, "segment": None, "country_name": None,
                             "tax_label": None, "standard_tax_rate": None, "contracts": 0}])
    return pd.concat([cust, unknown], ignore_index=True)


def build_dim_contract(contracts: list[dict], key_by_name: dict) -> pd.DataFrame:
    rows = []
    for c in contracts:
        disc_ev = next((v for k, v in c["evidence"].items() if k.endswith(".discount_pct") and v), None)
        rows.append({
            "contract_id": c["contract_id"],
            "customer_key": key_by_name.get(name_key(c["customer_name"] or ""), UNKNOWN),
            "customer_name": c["customer_name"], "country_code": c["country"],
            "start_date": c["start_date"], "end_date": c["end_date"],
            "escalation_pct": c["escalation_pct"], "escalation_date": c["escalation_date"],
            "products": ", ".join(ln["product_code"] for ln in c["lines"]),
            "product_count": len(c["lines"]),
            "has_discount": any(ln["discount_pct"] for ln in c["lines"]),
            "has_usage_tier": any(ln["tier_limit"] for ln in c["lines"]),
            "escalation_wording": clause_family(c["evidence"].get("escalation_pct"), "escalation"),
            "discount_wording": clause_family(disc_ev, "discount"),
            "min_extraction_confidence": min(c["confidence"].values()) if c["confidence"] else None,
            "extractor": c["extractor"], "source_file": c["source_file"],
        })
    return pd.DataFrame(rows)


def build_dim_date(dates: pd.Series) -> pd.DataFrame:
    d = pd.date_range(pd.to_datetime(dates).min().replace(day=1),
                      pd.to_datetime(dates).max() + pd.offsets.MonthEnd(0), freq="D")
    return pd.DataFrame({
        "date_key": d.strftime("%Y-%m-%d"), "date": d.date, "year": d.year,
        "quarter": "Q" + d.quarter.astype(str), "year_quarter": d.year.astype(str) + "-Q" + d.quarter.astype(str),
        "month": d.month, "month_name": d.strftime("%b"), "year_month": d.strftime("%Y-%m"),
    })


def run() -> None:
    ensure_output_dirs()
    out = config.POWERBI_DIR
    contracts = load_extracted_contracts()
    dim_customer = build_dim_customer(contracts)
    key_by_name = {name_key(n): k for n, k in zip(dim_customer["customer_name"], dim_customer["customer_key"])
                   if k != UNKNOWN}

    # dim_contract
    dim_contract = build_dim_contract(contracts, key_by_name)

    # fact_invoice_lines
    inv = load_invoices()
    cmap = pd.read_csv(config.OUTPUT_DIR / "invoice_contract_map.csv", dtype={"invoice_line_id": str})
    flags = pd.read_csv(config.OUTPUT_DIR / "flags_all.csv", dtype={"invoice_line_id": str})
    fact_lines = inv.merge(cmap[["invoice_line_id", "contract_id", "match_score", "match_method",
                                 "unmatched_reason"]], on="invoice_line_id", how="left")
    fact_lines = fact_lines.merge(dim_contract[["contract_id", "customer_key"]], on="contract_id", how="left")
    fact_lines["customer_key"] = fact_lines["customer_key"].fillna(UNKNOWN)
    fact_lines["match_status"] = fact_lines["contract_id"].notna().map({True: "Matched", False: "Unmatched"})
    fact_lines["flag_count"] = fact_lines["invoice_line_id"].map(flags["invoice_line_id"].value_counts()).fillna(0).astype(int)
    fact_lines["date_key"] = pd.to_datetime(fact_lines["invoice_date"]).dt.strftime("%Y-%m-%d")
    fact_lines = fact_lines.rename(columns={"customer_name": "invoice_customer_name"})
    fact_lines = fact_lines[["invoice_line_id", "invoice_id", "date_key", "customer_key", "contract_id",
                             "invoice_customer_name", "product_code", "description", "quantity", "unit_price",
                             "discount_amount", "net_amount", "tax_rate", "tax_amount", "match_status",
                             "match_method", "match_score", "unmatched_reason", "flag_count"]]

    # fact_flags
    ff = flags.merge(dim_contract[["contract_id", "customer_key"]], on="contract_id", how="left")
    ff["customer_key"] = ff["customer_key"].fillna(UNKNOWN)
    # L3 flags with no invoice in that month carry the period in their key
    period_date = ff["invoice_line_id"].str.extract(r"(\d{4}-\d{2})$")[0] + "-01"
    ff["date_key"] = pd.to_datetime(ff["invoice_date"].fillna(period_date)).dt.strftime("%Y-%m-%d")
    ff["is_visible"] = ~ff["below_threshold"]
    ff["tax_direction"] = ff.apply(lambda r: ("Under-charged" if r.impact_usd > 0 else "Over-charged")
                                   if r.rule_id == "L5" else None, axis=1)
    ff = ff[["flag_id", "invoice_line_id", "invoice_id", "date_key", "customer_key", "contract_id",
             "invoice_customer_name", "product_code", "rule_id", "direction", "tax_direction",
             "expected_amount", "billed_amount", "impact_usd", "confidence", "priority_score",
             "is_visible", "status", "reviewed_by", "note", "evidence_clause", "rule_detail"]]

    # dim_leak_type
    dim_leak = pd.DataFrame([{"rule_id": r, "leak_type": n, "severity": s,
                              "rule": LEAK_TYPE_DETAILS[r][0], "impact_formula": LEAK_TYPE_DETAILS[r][1],
                              "process_owner": LEAK_TYPE_DETAILS[r][2], "sort_order": i}
                             for i, (r, (n, s)) in enumerate(config.LEAK_TYPES.items(), 1)])

    dim_date = build_dim_date(pd.concat([fact_lines["date_key"], ff["date_key"]]))

    tables = {"fact_flags": ff, "fact_invoice_lines": fact_lines, "dim_customer": dim_customer,
              "dim_contract": dim_contract, "dim_leak_type": dim_leak, "dim_date": dim_date}
    for name, df in tables.items():
        df.to_csv(out / f"{name}.csv", index=False)
        print(f"  {name:<20} {len(df):>7,} rows")
    print(f"Power BI tables written to {out.relative_to(config.ROOT)}")


if __name__ == "__main__":
    run()
