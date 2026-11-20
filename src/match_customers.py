"""
Customer and contract matching (FR-05, FR-12).

Step A - customer names. Invoices spell customers differently from contracts
("BLUE CONSTRUCTION GMBH", "Blue Construction Corp.", "BlueConstruction GmbH").
Each name is normalised (case, punctuation, legal suffixes) and compared:
    1. manual mapping in data/manual_mappings.csv   -> always wins
    2. exact match on the normalised, space-free key -> score 100
    3. rapidfuzz similarity >= MATCH_AUTO_ACCEPT      -> accepted
    4. MATCH_REVIEW_FLOOR..AUTO_ACCEPT                -> suggestion only, NOT used until
                                                         someone adds it to manual_mappings.csv
Step B - contracts. A customer can hold several contracts, so each invoice
(all lines sharing an invoice_id) is assigned to the customer's contract that
was active on the invoice date and contains every product on the invoice.

Outputs: output/customer_matches.csv, output/invoice_contract_map.csv,
         output/unmatched_invoice_lines.csv
"""

import re

import pandas as pd
from rapidfuzz import fuzz, process

from src import config
from src.common import (ensure_output_dirs, escalations_applied, load_customers,
                        load_extracted_contracts, load_invoices, parse_date, price_after)

# Legal suffix -> countries where it is used (a GmbH is never an Australian Pty Ltd)
SUFFIX_COUNTRIES = {
    "llc": {"AE"}, "fz llc": {"AE"}, "fze": {"AE"}, "pty ltd": {"AU"},
    "limited": {"NZ", "IE"}, "ltd": {"NZ", "IE"}, "gmbh": {"DE"}, "ag": {"DE"},
    "bv": {"NL"}, "dac": {"IE"},
}


# ==========================================================================
# Name normalisation
# ==========================================================================

def _clean(name: str) -> str:
    s = str(name).lower().replace("&", " and ")
    s = re.sub(r"(?<=\b\w)\.(?=\w\.)", "", s)       # "b.v." -> "bv."
    s = s.replace(".", " ").replace("-", " ").replace(",", " ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def legal_suffix(name: str) -> str | None:
    """The legal-form suffix at the end of a name, e.g. 'pty ltd', or None."""
    s = _clean(name)
    for suf in config.LEGAL_SUFFIXES:
        if s == suf or s.endswith(" " + suf):
            return suf
    return None


def normalize_name(name: str) -> str:
    """'BLUE CONSTRUCTION GmbH.' -> 'blue construction' (case, punctuation, suffixes removed)."""
    s = _clean(name)
    changed = True
    while changed:                                   # strip stacked suffixes: "x corp ltd"
        changed = False
        for suf in config.LEGAL_SUFFIXES:
            if s.endswith(" " + suf):
                s, changed = s[: -len(suf) - 1].strip(), True
                break
    return s


def name_key(name: str) -> str:
    """Space-free key, so 'BlueConstruction' equals 'Blue Construction'.

    Also strips a suffix glued to the name when spaces were lost
    ('HarbourAnalyticsPty Ltd' -> 'harbouranalytics')."""
    key = normalize_name(name).replace(" ", "")
    for suf in config.LEGAL_SUFFIXES:
        glued = suf.replace(" ", "")
        if len(glued) >= 3 and key.endswith(glued) and len(key) - len(glued) >= 6:
            return key[: -len(glued)]
    return key


def suffixes_conflict(a: str, b: str) -> bool:
    """True when both names carry legal suffixes that cannot belong to the same country."""
    sa, sb = legal_suffix(a), legal_suffix(b)
    if not sa or not sb or sa not in SUFFIX_COUNTRIES or sb not in SUFFIX_COUNTRIES:
        return False
    return not (SUFFIX_COUNTRIES[sa] & SUFFIX_COUNTRIES[sb])


# ==========================================================================
# Step A: match invoice names to contract customers
# ==========================================================================

def load_manual_mappings() -> dict[str, str]:
    """invoice_customer_name -> contract customer name, maintained by the Billing team."""
    if not config.MANUAL_MAPPINGS_CSV.exists():
        config.MANUAL_MAPPINGS_CSV.write_text(
            "invoice_customer_name,contract_customer_name,approved_by,note\n", encoding="utf-8")
    df = pd.read_csv(config.MANUAL_MAPPINGS_CSV, dtype=str).fillna("")
    return {r.invoice_customer_name: r.contract_customer_name for r in df.itertuples()
            if r.invoice_customer_name}


def match_name(inv_name: str, contract_names: list[str], key_index: dict[str, str],
               manual: dict[str, str]) -> dict:
    """Match one invoice customer name. Returns matched name, score, method and status."""
    if inv_name in manual:
        target = manual[inv_name]
        return {"matched_customer": target or None, "score": 100.0, "method": "manual",
                "status": "matched" if target else "manually_unmatched"}

    key = name_key(inv_name)
    if key in key_index:
        target = key_index[key]
        if suffixes_conflict(inv_name, target):
            return {"matched_customer": None, "suggested_customer": target, "score": 85.0,
                    "method": "suffix_conflict", "status": "needs_review"}
        return {"matched_customer": target, "score": 100.0, "method": "exact_normalised",
                "status": "matched"}

    best = process.extractOne(normalize_name(inv_name), {n: normalize_name(n) for n in contract_names},
                              scorer=fuzz.token_sort_ratio)
    if best:
        _, score, target = best
        if score >= config.MATCH_AUTO_ACCEPT and not suffixes_conflict(inv_name, target):
            return {"matched_customer": target, "score": round(score, 1), "method": "fuzzy",
                    "status": "matched"}
        if score >= config.MATCH_REVIEW_FLOOR:
            return {"matched_customer": None, "suggested_customer": target, "score": round(score, 1),
                    "method": "fuzzy", "status": "needs_review"}
    return {"matched_customer": None, "score": round(best[1], 1) if best else 0.0,
            "method": "none", "status": "unmatched"}


# ==========================================================================
# Step B: assign each invoice to one contract
# ==========================================================================

def _base_code(code: str) -> str:
    return code[:-4] if code.endswith("-OVG") else code


def _price_gap(contract: dict, lines: pd.DataFrame) -> float:
    """How far billed prices are from any valid contract price (0 = perfect fit)."""
    by_code = {ln["product_code"]: ln for ln in contract["lines"]}
    gap = 0.0
    for r in lines.itertuples():
        cl = by_code.get(r.product_code)
        if cl is None or not cl.get("unit_price"):
            continue
        years = escalations_applied(contract["escalation_date"], r.invoice_date)
        options = [price_after(cl["unit_price"], contract["escalation_pct"], k) for k in range(years + 1)]
        gap += min(abs(r.unit_price - p) / p for p in options)
        if cl.get("quantity") and r.quantity != cl["quantity"]:
            gap += 1.0
    return gap


def assign_contracts(invoices: pd.DataFrame, contracts_by_customer: dict[str, list[dict]]) -> pd.DataFrame:
    """Return invoice_line_id -> contract_id with a reason when no contract fits."""
    rows = []
    for inv_id, grp in invoices.groupby("invoice_id", sort=False):
        customer = grp["matched_customer"].iat[0]
        inv_date = grp["invoice_date"].iat[0]
        products = {_base_code(c) for c in grp["product_code"]}
        contract_id, reason = None, None
        if pd.isna(customer):
            reason = "customer not matched"
        else:
            candidates = [c for c in contracts_by_customer.get(customer, [])
                          if c["start_date"] and c["start_date"] <= inv_date
                          and (c["end_date"] is None or inv_date <= c["end_date"])
                          and products <= {ln["product_code"] for ln in c["lines"]}]
            if not candidates:
                reason = "no active contract covers these products"
            else:
                base_lines = grp[~grp["product_code"].str.endswith("-OVG")]
                contract_id = min(candidates, key=lambda c: _price_gap(c, base_lines))["contract_id"]
        rows += [{"invoice_line_id": lid, "contract_id": contract_id, "unmatched_reason": reason}
                 for lid in grp["invoice_line_id"]]
    return pd.DataFrame(rows)


# ==========================================================================
# Run
# ==========================================================================

def run() -> pd.DataFrame:
    ensure_output_dirs()
    invoices = load_invoices()
    contracts = load_extracted_contracts()
    for c in contracts:                               # JSON dates -> date objects
        for k in ("start_date", "end_date", "escalation_date"):
            c[k] = parse_date(c[k])

    contract_names = sorted({c["customer_name"] for c in contracts if c["customer_name"]})
    key_index = {name_key(n): n for n in contract_names}
    manual = load_manual_mappings()

    # Customer master lookup (customer_id, segment) by normalised name
    master = load_customers()
    master_by_key = {name_key(r.name): r for r in master.itertuples()}

    # --- Step A
    name_counts = invoices["customer_name"].value_counts()
    match_rows = []
    for inv_name, n_lines in name_counts.items():
        m = match_name(inv_name, contract_names, key_index, manual)
        cust = master_by_key.get(name_key(m["matched_customer"])) if m["matched_customer"] else None
        match_rows.append({"invoice_customer_name": inv_name, **m,
                           "customer_id": cust.customer_id if cust else None,
                           "invoice_lines": int(n_lines)})
    matches = pd.DataFrame(match_rows)
    cols = ["invoice_customer_name", "matched_customer", "suggested_customer", "customer_id",
            "score", "method", "status", "invoice_lines"]
    matches = matches.reindex(columns=cols).sort_values(["status", "score"])
    matches.to_csv(config.OUTPUT_DIR / "customer_matches.csv", index=False)

    # --- Step B
    invoices = invoices.merge(matches[["invoice_customer_name", "matched_customer", "customer_id",
                                       "score", "method"]],
                              left_on="customer_name", right_on="invoice_customer_name", how="left")
    by_customer = {}
    for c in contracts:
        by_customer.setdefault(c["customer_name"], []).append(c)
    assigned = assign_contracts(invoices, by_customer)
    out = invoices.merge(assigned, on="invoice_line_id")
    out = out[["invoice_line_id", "invoice_id", "invoice_date", "customer_name", "matched_customer",
               "customer_id", "score", "method", "contract_id", "unmatched_reason"]]
    out = out.rename(columns={"score": "match_score", "method": "match_method"})
    out.to_csv(config.OUTPUT_DIR / "invoice_contract_map.csv", index=False)
    out[out["contract_id"].isna()].to_csv(config.OUTPUT_DIR / "unmatched_invoice_lines.csv", index=False)

    # --- Report (FR-12)
    n_names = len(matches)
    names_ok = (matches["status"] == "matched").sum()
    lines_cust = out["matched_customer"].notna().mean()
    lines_contract = out["contract_id"].notna().mean()
    print(f"Invoice customer names: {n_names} | matched: {names_ok} "
          f"| needs review: {(matches['status'] == 'needs_review').sum()} "
          f"| unmatched: {(matches['status'] == 'unmatched').sum()}")
    print("Match methods:", matches.loc[matches.status == "matched", "method"].value_counts().to_dict())
    print(f"Invoice lines matched to a customer: {lines_cust:.2%} | "
          f"matched to a contract: {lines_contract:.2%} | "
          f"unmatched lines: {out['contract_id'].isna().sum():,} (see unmatched_invoice_lines.csv)")
    print("Unmatched reasons:", out["unmatched_reason"].value_counts().to_dict())
    return out


if __name__ == "__main__":
    run()
