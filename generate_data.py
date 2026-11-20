"""
Revenue Leakage Detector - synthetic data generator.

Creates a realistic, deliberately messy dataset of B2B contracts and invoices
with revenue leaks planted on purpose, plus an answer key for testing.

Outputs (in ./data):
  customers.csv        customer master
  contracts.csv        contract headers (ground truth - the detector should
                       NOT read this; it should extract terms from the text files)
  contract_lines.csv   contract line terms (ground truth, same caveat)
  contracts/*.txt      contract documents with varied clause wording
  usage.csv            monthly usage for metered products
  invoices.csv         invoice lines as billed (with planted leaks + messy names)
  planted_leaks.csv    answer key: which lines leak, why, and by how much

Leak types (see BRD):
  L1 missed price escalation   L2 expired discount still applied
  L3 unbilled overage          L4 wrong unit price
  L5 wrong tax rate

Usage:  python generate_data.py [--seed 42] [--contracts 500] [--out data]
"""

import argparse
import csv
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

COUNTRIES = {
    # code: (name, tax label, standard rate, company suffixes)
    "AE": ("United Arab Emirates", "VAT", 0.05, ["LLC", "FZ-LLC", "FZE"]),
    "AU": ("Australia", "GST", 0.10, ["Pty Ltd"]),
    "NZ": ("New Zealand", "GST", 0.15, ["Limited", "Ltd"]),
    "DE": ("Germany", "VAT", 0.19, ["GmbH", "AG"]),
    "NL": ("Netherlands", "VAT", 0.21, ["B.V."]),
    "IE": ("Ireland", "VAT", 0.23, ["Limited", "DAC"]),
}
COUNTRY_WEIGHTS = {"AE": 25, "AU": 25, "NZ": 10, "DE": 15, "NL": 12, "IE": 13}

NAME_A = ["Blue", "North", "Silver", "Red", "Harbour", "Desert", "Summit", "Pacific",
          "Atlas", "Falcon", "Cedar", "Coral", "Iron", "Nova", "Orbit", "Pioneer",
          "Quantum", "River", "Sterling", "Titan", "Union", "Vertex", "Willow", "Zenith",
          "Apex", "Bright", "Crescent", "Delta", "Emerald", "Granite"]
NAME_B = ["Logistics", "Health", "Retail", "Foods", "Energy", "Finance", "Systems",
          "Media", "Construction", "Travel", "Labs", "Analytics", "Motors", "Telecom",
          "Hospitality", "Pharma", "Insurance", "Education", "Properties", "Trading"]

PRODUCTS = {
    # code: (name, list price per unit/month, metered?, tier sizes, overage rate)
    "PLAT-BASE": ("Platform licence (per seat)", 45.00, False, None, None),
    "PLAT-PRO":  ("Platform Pro licence (per seat)", 85.00, False, None, None),
    "SUPP-PREM": ("Premium support (flat)", 1200.00, False, None, None),
    "DATA-STOR": ("Data storage (per TB)", 60.00, False, None, None),
    "API-CALLS": ("API calls bundle", 900.00, True, [100_000, 250_000, 500_000], 0.006),
    "SMS-MSG":   ("SMS messages bundle", 400.00, True, [20_000, 50_000, 100_000], 0.015),
}

START_RANGE = (date(2024, 1, 1), date(2025, 9, 30))
CUTOFF = date(2026, 10, 31)          # last invoice month in the dataset

# Probability that a given contract / line / period gets each leak
P_L1_CONTRACT = 0.08   # escalation never applied for this contract
P_L2_LINE = 0.35       # of lines that HAVE a discount, it keeps running past end date
P_L3_PERIOD = 0.30     # of over-tier periods, overage line is not billed
P_L4_LINE = 0.006      # random keying error on any invoice line
P_L5_CUSTOMER = 0.05   # customer billed at another country's tax rate

P_NAME_VARIANT = 0.18  # customer name written differently on invoices
P_ORPHAN_LINE = 0.02   # invoice line whose customer has no contract

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


def month_starts(start: date, end: date):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = add_months(d, 1)


def rand_date(rng, a: date, b: date) -> date:
    return date.fromordinal(rng.randint(a.toordinal(), b.toordinal()))


def money(x: float) -> float:
    return round(x + 1e-9, 2)


def name_variant(rng, name: str) -> str:
    """Return a messy version of a company name, like real billing data."""
    base, *suffix = name.rsplit(" ", 1) if " " in name else (name,)
    options = [
        name.upper(),
        base,                                   # suffix dropped
        base + " Corp.",
        name.replace(" ", "  ", 1),             # double space
        base.replace(" ", "") + (" " + suffix[0] if suffix else ""),
        name + ".",
    ]
    return rng.choice(options)


# --------------------------------------------------------------------------
# Contract text templates (varied wording on purpose)
# --------------------------------------------------------------------------

ESCALATION_TEMPLATES = [
    "Fees shall increase by {pct}% on each anniversary of the Effective Date.",
    "On {date} and annually thereafter, all unit prices will rise by {pct} percent.",
    "The Supplier may apply an annual uplift of {pct}% to the Fees, effective from the first anniversary ({date}).",
    "Pricing is subject to a yearly adjustment of {pct}% commencing {date}.",
]
DISCOUNT_TEMPLATES = [
    "A promotional discount of {pct}% applies to {product} until {end}.",
    "Customer will receive {pct}% off the contracted price of {product} for invoices dated on or before {end}.",
    "Introductory pricing: {product} is discounted by {pct} percent through {end}, after which standard rates apply.",
]
TIER_TEMPLATES = [
    "{product} includes up to {tier:,} units per month. Usage above this allowance is billed at USD {rate} per unit.",
    "The monthly allowance for {product} is {tier:,} units; excess units are charged at {rate} USD each.",
    "Overage: any {product} usage beyond {tier:,} units in a calendar month incurs a fee of USD {rate} per additional unit.",
]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Customer:
    customer_id: str
    name: str
    country: str
    segment: str
    invoice_name: str
    billed_tax_country: str


@dataclass
class ContractLine:
    product_code: str
    quantity: int
    unit_price: float
    discount_pct: float = 0.0
    discount_end: date | None = None
    tier_limit: int | None = None
    overage_rate: float | None = None
    leak_l2: bool = False


@dataclass
class Contract:
    contract_id: str
    customer: Customer
    start: date
    end: date
    escalation_pct: float
    escalation_date: date
    lines: list = field(default_factory=list)
    leak_l1: bool = False


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def make_customers(rng, n):
    customers, used = [], set()
    codes = list(COUNTRY_WEIGHTS)
    weights = [COUNTRY_WEIGHTS[c] for c in codes]
    for i in range(n):
        country = rng.choices(codes, weights)[0]
        while True:
            base = f"{rng.choice(NAME_A)} {rng.choice(NAME_B)}"
            if base not in used:
                used.add(base)
                break
        name = f"{base} {rng.choice(COUNTRIES[country][3])}"
        invoice_name = name_variant(rng, name) if rng.random() < P_NAME_VARIANT else name
        billed_tax_country = country
        if rng.random() < P_L5_CUSTOMER:
            billed_tax_country = rng.choice([c for c in codes if c != country])
        customers.append(Customer(
            customer_id=f"C{i + 1:04d}", name=name, country=country,
            segment=rng.choice(["SMB", "Mid-market", "Enterprise"]),
            invoice_name=invoice_name, billed_tax_country=billed_tax_country,
        ))
    return customers


def make_contracts(rng, customers, n):
    contracts = []
    for i in range(n):
        cust = customers[i % len(customers)] if i < len(customers) else rng.choice(customers)
        start = date(*rand_date(rng, *START_RANGE).timetuple()[:2], 1)
        end = add_months(start, rng.choice([24, 36]))
        esc_pct = rng.choice([3, 3.5, 4, 5, 6, 7])
        c = Contract(
            contract_id=f"K{i + 1:05d}", customer=cust, start=start,
            end=date.fromordinal(end.toordinal() - 1),
            escalation_pct=esc_pct, escalation_date=add_months(start, 12),
            leak_l1=rng.random() < P_L1_CONTRACT,
        )
        for code in rng.sample(list(PRODUCTS), rng.randint(1, 3)):
            pname, list_price, metered, tiers, overage = PRODUCTS[code]
            price = money(list_price * rng.uniform(0.85, 1.0))  # negotiated price
            qty = 1 if metered or code == "SUPP-PREM" else rng.randint(5, 250)
            line = ContractLine(code, qty, price)
            if metered:
                line.tier_limit = rng.choice(tiers)
                line.overage_rate = overage
            if rng.random() < 0.30:
                line.discount_pct = rng.choice([5, 10, 15, 20])
                line.discount_end = date.fromordinal(
                    add_months(start, rng.randint(3, 9)).toordinal() - 1)
                line.leak_l2 = rng.random() < P_L2_LINE
            c.lines.append(line)
        contracts.append(c)
    return contracts


def contract_text(rng, c: Contract) -> str:
    cust = c.customer
    lines = [
        "MASTER SUBSCRIPTION AGREEMENT",
        "",
        f"Agreement No.: {c.contract_id}",
        f"Customer: {cust.name}",
        f"Registered country: {COUNTRIES[cust.country][0]}",
        f"Effective Date: {c.start:%d %B %Y}",
        f"Term: until {c.end:%d %B %Y}",
        "",
        "1. SERVICES AND FEES",
        "The Customer subscribes to the following services. Fees are in USD, exclusive of tax, billed monthly in arrears.",
        "",
    ]
    for ln in c.lines:
        pname = PRODUCTS[ln.product_code][0]
        if ln.quantity > 1:
            lines.append(f"- {pname} [{ln.product_code}]: {ln.quantity} units at USD {ln.unit_price:,.2f} per unit per month")
        else:
            lines.append(f"- {pname} [{ln.product_code}]: USD {ln.unit_price:,.2f} per month")
    lines += ["", "2. PRICE ADJUSTMENT",
              rng.choice(ESCALATION_TEMPLATES).format(
                  pct=c.escalation_pct, date=f"{c.escalation_date:%d %B %Y}"), ""]
    disc = [ln for ln in c.lines if ln.discount_pct]
    if disc:
        lines.append("3. DISCOUNTS")
        for ln in disc:
            lines.append(rng.choice(DISCOUNT_TEMPLATES).format(
                pct=ln.discount_pct, product=PRODUCTS[ln.product_code][0],
                end=f"{ln.discount_end:%d %B %Y}"))
        lines.append("")
    tiers = [ln for ln in c.lines if ln.tier_limit]
    if tiers:
        lines.append("4. USAGE ALLOWANCES")
        for ln in tiers:
            lines.append(rng.choice(TIER_TEMPLATES).format(
                product=PRODUCTS[ln.product_code][0], tier=ln.tier_limit, rate=ln.overage_rate))
        lines.append("")
    lines += ["5. TAXES",
              f"All Fees are exclusive of {COUNTRIES[cust.country][1]}, which will be charged at the applicable rate.",
              "", "6. GENERAL",
              "This Agreement is governed by the laws of the Customer's registered country.", ""]
    return "\n".join(lines)


def expected_price(c: Contract, ln: ContractLine, month: date) -> float:
    years = 0
    d = c.escalation_date
    while d <= month:
        years += 1
        d = add_months(d, 12)
    return money(ln.unit_price * (1 + c.escalation_pct / 100) ** years)


def build_invoices(rng, contracts, customers):
    invoices, usage_rows, leaks = [], [], []
    inv_counter = 0
    line_counter = 0

    def add_line(c, month, inv_id, code, desc, qty, exp_price, bill_price,
                 exp_disc, bill_disc, leak_types, extra_impact=0.0):
        nonlocal line_counter
        line_counter += 1
        cust = c.customer
        correct_rate = COUNTRIES[cust.country][2]
        billed_rate = COUNTRIES[cust.billed_tax_country][2]
        bill_price = bill_price
        net = money(qty * bill_price - bill_disc)
        exp_net = money(qty * exp_price - exp_disc)
        line_id = f"L{line_counter:07d}"
        invoices.append({
            "invoice_line_id": line_id, "invoice_id": inv_id,
            "invoice_date": month.isoformat(), "customer_name": cust.invoice_name,
            "product_code": code, "description": desc, "quantity": qty,
            "unit_price": bill_price, "discount_amount": money(bill_disc),
            "net_amount": net, "tax_rate": billed_rate,
            "tax_amount": money(net * billed_rate), "currency": "USD",
        })
        lt = list(leak_types)
        if billed_rate != correct_rate:
            lt.append("L5")
        for t in lt:
            if t == "L5":
                exp, billed = money(exp_net * correct_rate), money(net * billed_rate)
            elif t == "L3":
                exp, billed = extra_impact, 0.0
            else:
                exp, billed = exp_net, net
            leaks.append({
                "invoice_line_id": line_id, "invoice_id": inv_id,
                "contract_id": c.contract_id, "leak_type": t,
                "expected_amount": exp, "billed_amount": billed,
                "impact_usd": money(exp - billed),
            })

    for c in contracts:
        for month in month_starts(c.start, min(c.end, CUTOFF)):
            inv_counter += 1
            inv_id = f"INV-{month:%Y%m}-{inv_counter:06d}"
            for ln in c.lines:
                pname = PRODUCTS[ln.product_code][0]
                exp_p = expected_price(c, ln, month)
                bill_p = exp_p
                leak_types = []
                if c.leak_l1 and month >= c.escalation_date:
                    bill_p = ln.unit_price
                    leak_types.append("L1")
                if rng.random() < P_L4_LINE:
                    bill_p = money(bill_p * rng.choice([0.9, 0.95, 0.5]))
                    leak_types = [t for t in leak_types if t != "L1"] + ["L4"]
                qty = ln.quantity
                disc_active = ln.discount_pct and month <= ln.discount_end
                exp_disc = money(qty * exp_p * ln.discount_pct / 100) if disc_active else 0.0
                bill_disc = money(qty * bill_p * ln.discount_pct / 100) if disc_active else 0.0
                if ln.discount_pct and not disc_active and ln.leak_l2:
                    bill_disc = money(qty * bill_p * ln.discount_pct / 100)
                    leak_types.append("L2")
                add_line(c, month, inv_id, ln.product_code, pname, qty,
                         exp_p, bill_p, exp_disc, bill_disc, leak_types)

                if ln.tier_limit:
                    # usage hovers around the tier; ~25% of months go over
                    used = int(ln.tier_limit * rng.uniform(0.55, 1.35))
                    usage_rows.append({
                        "customer_id": c.customer.customer_id, "contract_id": c.contract_id,
                        "product_code": ln.product_code, "period": f"{month:%Y-%m}",
                        "units_used": used})
                    over = used - ln.tier_limit
                    if over > 0:
                        amount = money(over * ln.overage_rate)
                        if rng.random() < P_L3_PERIOD:
                            # overage never billed: record leak against the base line
                            line_id = invoices[-1]["invoice_line_id"]
                            leaks.append({
                                "invoice_line_id": line_id, "invoice_id": inv_id,
                                "contract_id": c.contract_id, "leak_type": "L3",
                                "expected_amount": amount, "billed_amount": 0.0,
                                "impact_usd": amount})
                        else:
                            add_line(c, month, inv_id, ln.product_code + "-OVG",
                                     f"{pname} overage ({over:,} units)", over,
                                     ln.overage_rate, ln.overage_rate, 0.0, 0.0, [])

    # orphan lines: billed to customers with no contract (matching test)
    n_orphans = int(len(invoices) * P_ORPHAN_LINE)
    for _ in range(n_orphans):
        line_counter += 1
        month = rand_date(rng, START_RANGE[0], CUTOFF).replace(day=1)
        country = rng.choice(list(COUNTRIES))
        net = money(rng.uniform(100, 3000))
        invoices.append({
            "invoice_line_id": f"L{line_counter:07d}",
            "invoice_id": f"INV-{month:%Y%m}-X{line_counter:06d}",
            "invoice_date": month.isoformat(),
            "customer_name": f"{rng.choice(NAME_A)} {rng.choice(NAME_B)} {rng.choice(COUNTRIES[country][3])}",
            "product_code": "PS-ONEOFF", "description": "Professional services (one-off)",
            "quantity": 1, "unit_price": net, "discount_amount": 0.0, "net_amount": net,
            "tax_rate": COUNTRIES[country][2], "tax_amount": money(net * COUNTRIES[country][2]),
            "currency": "USD"})

    rng.shuffle(invoices)
    invoices.sort(key=lambda r: r["invoice_date"])
    return invoices, usage_rows, leaks


def write_csv(path: Path, rows, fields=None):
    rows = list(rows)
    fields = fields or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--contracts", type=int, default=500)
    ap.add_argument("--customers", type=int, default=300)
    ap.add_argument("--out", default="data")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.out)
    (out / "contracts").mkdir(parents=True, exist_ok=True)

    customers = make_customers(rng, args.customers)
    contracts = make_contracts(rng, customers, args.contracts)
    invoices, usage, leaks = build_invoices(rng, contracts, customers)

    write_csv(out / "customers.csv", ({
        "customer_id": c.customer_id, "name": c.name, "billing_country": c.country,
        "segment": c.segment} for c in customers))
    write_csv(out / "contracts.csv", ({
        "contract_id": c.contract_id, "customer_id": c.customer.customer_id,
        "start_date": c.start, "end_date": c.end, "escalation_pct": c.escalation_pct,
        "escalation_date": c.escalation_date, "billing_country": c.customer.country,
        "contract_file": f"contracts/{c.contract_id}.txt"} for c in contracts))
    write_csv(out / "contract_lines.csv", ({
        "contract_id": c.contract_id, "product_code": ln.product_code,
        "quantity": ln.quantity, "unit_price": ln.unit_price,
        "discount_pct": ln.discount_pct or "", "discount_end_date": ln.discount_end or "",
        "tier_limit": ln.tier_limit or "", "overage_rate": ln.overage_rate or ""}
        for c in contracts for ln in c.lines))
    for c in contracts:
        (out / "contracts" / f"{c.contract_id}.txt").write_text(contract_text(rng, c), encoding="utf-8")
    write_csv(out / "usage.csv", usage)
    write_csv(out / "invoices.csv", invoices)
    write_csv(out / "planted_leaks.csv", leaks, [
        "invoice_line_id", "invoice_id", "contract_id", "leak_type",
        "expected_amount", "billed_amount", "impact_usd"])

    # summary
    total_net = sum(r["net_amount"] for r in invoices)
    by_type = {}
    for l in leaks:
        t = by_type.setdefault(l["leak_type"], [0, 0.0])
        t[0] += 1
        t[1] += l["impact_usd"]
    total_leak = sum(v[1] for v in by_type.values())
    print(f"Customers: {len(customers)}  Contracts: {len(contracts)}  "
          f"Invoice lines: {len(invoices):,}  Usage rows: {len(usage):,}")
    print(f"Total billed (net): USD {total_net:,.0f}")
    print(f"Planted leaks: {len(leaks):,} flags, USD {total_leak:,.0f} "
          f"({total_leak / total_net:.2%} of billed revenue)")
    for t in sorted(by_type):
        print(f"  {t}: {by_type[t][0]:>5,} lines  USD {by_type[t][1]:>12,.0f}")


if __name__ == "__main__":
    main()
