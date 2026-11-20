# Revenue Leakage Detector

**Finding money companies have earned but never billed by checking every invoice against the contract it should follow.**

Revenue leakage is revenue a company is owed under its contracts but never invoices or collects. Industry estimates commonly put it at 1–5% of revenue. It goes unnoticed because contract terms sit in PDFs owned by Sales and Legal, while invoices sit in the billing system owned by Finance, and nobody systematically checks one against the other.

This project is an end-to-end business analysis case study: requirements, process redesign, a synthetic dataset, a detection engine and a Finance dashboard.

![As-is vs to-be contract-to-cash process](docs/process_as_is_to_be.png)

---

## Business problem

| Leak type | Example | Rule |
| --- | --- | --- |
| **L1** Missed price escalation | Contract says +4% each year, invoices stay at the old price | Billed price < contract price × (1 + escalation %) after the anniversary |
| **L2** Expired discount still applied | 15% intro discount ended in March, still on invoices in August | Discount on an invoice dated after the discount end date |
| **L3** Unbilled overage | Customer used 320K API calls on a 250K plan, no overage charged | Usage > tier limit and no overage line |
| **L4** Wrong unit price | Price keyed as 40.50 instead of 45.00 | Billed price differs from contract by > 0.5% with no explanation |
| **L5** Wrong tax rate | UAE customer billed 10% instead of 5% VAT | Tax rate ≠ rate for the customer's billing country |

Tax checks cover **UAE VAT (5%)**, **Australia GST (10%)**, **New Zealand GST (15%)** and EU VAT for Germany, the Netherlands and Ireland.

## Business analysis deliverables

- 📄 [Business Requirements Document](docs/Business_Requirements_Document.pdf): scope, stakeholder analysis, RACI, as-is/to-be process, business rules, 12 functional and 5 non-functional requirements, 8 user stories with acceptance criteria, data model, KPIs, risk register and UAT plan
- 🔄 As-is and to-be contract-to-cash process (above)

## Dataset

Real contract and invoice data is confidential, so `generate_data.py` builds a realistic synthetic dataset. Leaks are planted deliberately and recorded in an answer key, so detection accuracy can be measured.

| | |
| --- | --- |
| Customers | 300 across 6 countries |
| Contracts | 500 (text documents with clause wording that varies on purpose) |
| Invoice lines | ~25,200 (Jan 2024 – Oct 2026) |
| Total billed | ~USD 99M |
| Planted leakage | ~USD 1.96M (~2.0% of revenue) across ~5,000 invoice lines |

**Deliberately messy, like real data:**
- 18% of customers appear under a different name on invoices (`ACME LLC` vs `Acme Corp.`)
- 2% of invoice lines have no matching contract
- The same clause appears in several wordings ("increase by 4% on each anniversary" / "yearly adjustment of 4% commencing…")

| File | Contents |
| --- | --- |
| `data/contracts/*.txt` | Contract documents. **The detector reads these.** |
| `data/invoices.csv` | Invoice lines as billed. **The detector reads these.** |
| `data/usage.csv` | Monthly usage for metered products |
| `data/customers.csv` | Customer master |
| `data/contracts.csv`, `data/contract_lines.csv` | Ground-truth contract terms, used only to grade extraction |
| `data/planted_leaks.csv` | Answer key, used only to grade detection |

## How to run

```bash
# Python 3.10+, standard library only
python generate_data.py --seed 42 --out data
```

Change `--seed` for a different dataset or `--contracts` to change its size.

## Roadmap

- [x] Business Requirements Document
- [x] As-is / to-be process design
- [x] Synthetic dataset with planted leaks and answer key
- [ ] LLM extraction of contract terms (structured JSON + confidence scores)
- [ ] Fuzzy matching of invoice customers to contracts
- [ ] Rule engine for leak types L1–L5, ranked by dollar impact × confidence
- [ ] Accuracy evaluation against the answer key (target: ≥ 90% recall and precision)
- [ ] Power BI dashboard for Finance
- [ ] User acceptance testing with finance professionals
- [ ] Case study write-up: findings, root causes, recommendations

## Skills demonstrated

Requirements elicitation · stakeholder analysis · process mapping · business rules definition · user stories & acceptance criteria · data modelling · Python · SQL · LLMs for document extraction · Power BI · UAT

## Author

**Hari** · MSc in Artificial Intelligence · aspiring Business Analyst
[LinkedIn](https://www.linkedin.com/in/your-profile) · [Email](mailto:you@example.com)
