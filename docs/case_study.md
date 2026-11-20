# Case study: finding USD 2M of unbilled revenue by checking every invoice against its contract

*Revenue Leakage Detector · business analysis portfolio project · synthetic data*

## The problem

A B2B software company bills about USD 45M a year to 300 customers in six countries under 500 subscription contracts. The contracts promise annual price increases, time-limited discounts, usage allowances with overage fees, and local VAT or GST. Billing staff key those terms into the billing system by hand when a contract is signed.

After that, nobody checks invoices against contracts. Contracts live with Sales and Legal; invoices live with Finance. Errors surface only when a customer complains about being over-charged, or never, when the customer is under-charged. Industry estimates put this kind of revenue leakage at 1–5% of revenue.

The CFO's question was: **how much are we losing, where, and what should we fix first?**

## Approach

I ran the project as a business analyst would: requirements first, then a tool to answer the question, then evidence that the tool can be trusted.

1. **Requirements.** I wrote a [Business Requirements Document](Business_Requirements_Document.pdf) covering stakeholders, a RACI matrix, the as-is and to-be contract-to-cash process, and five leak rules (L1–L5) with a dollar-impact formula for each. It also holds 12 functional and 5 non-functional requirements and 8 user stories with acceptance criteria. The headline acceptance criterion: catch at least 90% of leaks, with fewer than 1 in 10 flags a false alarm.
2. **Test data with an answer key.** Real contracts are confidential, so I generated 500 contracts and 25,199 invoice lines with leaks planted on purpose and recorded in an answer key. The data is messy on purpose, like real data: customer names spelt differently on invoices, clauses worded four different ways, and 2% of invoice lines with no contract.
3. **Detection pipeline (Python).**
   - *Extract* the billing terms from each contract into structured data, using either a rule-based extractor or an LLM. Every extracted value is checked against the contract text, and values that cannot be found go to a human review queue.
   - *Match* each invoice to its customer and contract, despite names like `BLUE CONSTRUCTION GMBH` vs `Blue Construction Corp.`.
   - *Apply the five rules* to every invoice line. Each flag carries its dollar impact, a confidence score and the exact contract sentence it relied on.
4. **Evaluation.** I compared every flag against the answer key before trusting any number.
5. **Reporting.** I exported a star-schema data model and wrote a build guide for a three-page Power BI dashboard (executive summary, flag review, root causes and tax).

## Results

| | |
| --- | --- |
| **Leakage found** | **USD 2.02M across 4,722 flags, 2.0% of billed revenue** |
| Last 12 months | USD 1.15M, **2.5% of revenue, and rising** |
| Over-billing (reported separately) | USD 113K across 218 flags |
| Recall (planted leaks caught) | **100%** (target ≥ 90%) |
| Precision (flags that are real) | **99.98%**, one false alarm (target ≥ 90%) |
| Leakage value captured | 100% of planted USD |
| Invoices matched to a contract | 98.0% (target ≥ 97%); every unmatched line is a genuine no-contract line |

Leakage is **growing** each year (USD 0.20M in 2024, 0.85M in 2025, 0.96M in the first ten months of 2026). Errors compound: a discount that should have ended keeps running every month, and a missed price increase is missed again the next year. It is also **concentrated**: 10% of contracts account for 70% of the leakage, so a short review list recovers most of the money.

## Top root causes

| # | Root cause | Share | What the data shows |
| --- | --- | --- | --- |
| 1 | **Discounts never switched off** (L2) | 55% · USD 1.12M | 102 of 279 discounted contract lines (37%) kept the discount after its end date. The median line was still discounted **16 months** after it should have stopped. |
| 2 | **Wrong tax set-up on the customer record** (L5) | 18% · USD 364K, plus all USD 113K of over-billing | 14 customers are billed at another country's tax rate on **every** invoice. This is a one-time set-up error, not a one-off slip. 12 are under-charged; 2 UAE customers are over-charged. |
| 3 | **Usage overage not billed** (L3) | 11% · USD 227K | 31% of months where a customer exceeded the usage allowance had no overage charge. |
| 4 | **Annual price increases not applied** (L1) | 11% · USD 214K | 51 contracts (10%) were never moved to the escalated price. |
| 5 | **Prices keyed incorrectly** (L4) | 5% · USD 95K | 128 invoice lines across 111 contracts, typically 5–50% below the contract price. |

Causes 1, 2 and 4 come from the same place in the process: **a contract term with a future date or a set-up value is keyed once at signature and never revisited**. The billing system does not know when the discount ends or when the price rises.

One finding I did **not** act on: contracts that state the escalation as "on each anniversary", with no explicit date, missed their increase more often (14% vs 6–11% for other wordings). It is tempting to blame the wording, but in this dataset escalations were missed at random, so the gap is chance on small numbers (22 contracts). With real data I would test this before asking Legal to change the template.

## Recommendations

Recovering past revenue matters, but stopping the leak matters more. Most of the fixes are process changes, not code.

| Priority | Recommendation | Owner | Addresses |
| --- | --- | --- | --- |
| 1 | **Make discount end dates mandatory and self-expiring** in the billing system. Until then, run a monthly "discounts ending next month" report. | Billing ops | L2 (55%) |
| 2 | **Derive the tax code from the registered country at customer onboarding**, with a second-person check. Correct the 14 customers now and file VAT/GST amendments. | Tax team | L5 (18%), all over-billing |
| 3 | **Start the price-increase calendar from the contract system**: a billing task 30 days before each escalation date. | Billing ops + Sales ops | L1 (11%) |
| 4 | **Automate the usage-to-invoice feed**, and add a month-end check that every over-tier month has an overage line. | Billing ops + IT | L3 (11%) |
| 5 | **Load contract prices from the contract record**, not by hand, and block invoice approval when a price differs from the contract by more than 0.5%. | Billing ops | L4 (5%) |
| 6 | **Print the contract number on every invoice.** This removes the one remaining ambiguity in matching (a customer with two overlapping contracts for the same product). | Billing ops | Matching, audit trail |
| 7 | **Run the detector monthly** before invoices are sent, not once a year. Reviewers work through the flags in priority order (impact × confidence) and mark each one Confirmed, False alarm or Needs info. | Billing / AR | All |

On recovery: start with the top 50 contracts (70% of the value), have account managers warn customers before re-billing, and agree with the Controller how far back to re-bill. Customers accept a correction for last quarter more readily than for two years ago.

## Regex vs LLM: what I learned

I built two contract extractors behind the same interface: a free rule-based (regex) baseline and an LLM with structured JSON output (Claude API, or a local model through Ollama).

- On these templated contracts, the **regex extractor was perfect** (100% of 8,958 fields) and ran in about one second.
- The **local 3B LLM** (free, nothing leaves the laptop) got 90.4% of fields right on a 50-contract sample. Its typical errors were putting the usage allowance in the quantity field and missing overage terms.
- The important design result: **no wrong LLM value reached the leak rules unchecked.** Every value is looked up in the contract text, and missing clauses are detected too. Anything the model invented or skipped went to the review queue instead of creating a false flag.

The lesson I would bring to a real project: use the cheapest method that meets the accuracy target, measure it against labelled examples, and put a verification step between any AI output and a financial decision. Regex works when contracts come from a few templates. Free-form contracts from many sources are where an LLM earns its cost, and the review queue tells you how much human time it saves.

## Limitations

- **Synthetic data.** Leaks were planted by rules I wrote, so near-perfect detection is expected; it proves the logic, not real-world accuracy. Real contracts bring scanned pages, amendments, side letters and free-form clauses.
- **Five leak types only.** Missed renewals, unbilled one-off services and credit notes are out of scope (BRD Phase 2).
- **Standard tax rates only.** Reduced rates and reverse-charge rules are excluded, as agreed in the BRD.
- **Power BI is read-only.** Reviewer decisions are captured in a CSV file. A production version would use a SharePoint list or Power Apps form.

## Next steps

1. Build the Power BI dashboard from the [guide](powerbi_dashboard_guide.md) and run UAT with 2–3 finance professionals, one script per user story.
2. Pilot on a sample of 50 real, anonymised contracts with the Claude extractor and hand-labelled answers, to measure LLM accuracy and review-queue workload on messy documents.
3. Add the Phase 2 leak types (missed renewals, unbilled professional services).
4. Agree a recovery policy with the Controller and track recovered USD as the main KPI.
