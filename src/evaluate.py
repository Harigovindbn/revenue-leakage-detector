"""
Evaluation against the answer key (NFR-01 and the BRD success metrics).

This is the ONLY module allowed to read the ground truth:
  data/planted_leaks.csv              -> detection precision / recall / USD captured
  data/contracts.csv, contract_lines.csv -> field-level extraction accuracy

A flag is a true positive when (invoice_line_id, rule_id) matches a planted
leak. Leakage (customer under-billed) and over-billing are scored separately,
as the BRD reports them separately.

Every extractor whose output exists (output/extracted_contracts_<name>.json)
is evaluated, so the regex baseline and the LLM can be compared directly.
Writes output/evaluation_report.md plus false positive / negative files.
"""

import json
from datetime import date

import pandas as pd

from src import config
from src.common import load_customers, load_invoices, load_usage, parse_date
from src.detect_leaks import detect, prepare_contracts
from src.match_customers import build_matches

HEADER_FIELDS = ["customer_name", "country", "start_date", "end_date", "escalation_pct", "escalation_date"]
LINE_FIELDS = ["quantity", "unit_price", "discount_pct", "discount_end_date", "tier_limit", "overage_rate"]
TARGET = 0.90


# ==========================================================================
# Extraction accuracy
# ==========================================================================

def _same(a, b) -> bool:
    """Compare an extracted value with the truth (numbers with a small tolerance)."""
    if a is None or (isinstance(a, float) and pd.isna(a)) or a == "":
        a = None
    if b is None or (isinstance(b, float) and pd.isna(b)) or b == "":
        b = None
    if a is None or b is None:
        return a is None and b is None
    if isinstance(b, date) or isinstance(a, date):
        return parse_date(a) == parse_date(b)
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def load_truth() -> tuple[dict, dict]:
    contracts = pd.read_csv(config.TRUTH_CONTRACTS_CSV)
    names = load_customers().set_index("customer_id")["name"]
    lines = pd.read_csv(config.TRUTH_CONTRACT_LINES_CSV)
    header = {}
    for r in contracts.itertuples():
        header[r.contract_id] = {
            "customer_name": names[r.customer_id], "country": r.billing_country,
            "start_date": parse_date(r.start_date), "end_date": parse_date(r.end_date),
            "escalation_pct": r.escalation_pct, "escalation_date": parse_date(r.escalation_date)}
    line_truth = {}
    for r in lines.to_dict("records"):
        r["discount_end_date"] = parse_date(r["discount_end_date"])
        line_truth[(r["contract_id"], r["product_code"])] = r
    return header, line_truth


def evaluate_extraction(records: list[dict], header_truth: dict, line_truth: dict) -> dict:
    """Field-level accuracy, plus how many wrong values were still marked confident."""
    rows = []
    ids = {r["contract_id"] for r in records}
    for r in records:
        t = header_truth[r["contract_id"]]
        for f in HEADER_FIELDS:
            rows.append((f, _same(r.get(f), t[f]), r["confidence"].get(f, 0)))
        got = {ln["product_code"]: ln for ln in r["lines"]}
        for (cid, code), tl in line_truth.items():
            if cid != r["contract_id"]:
                continue
            ln = got.get(code)
            for f in LINE_FIELDS:
                ok = ln is not None and _same(ln.get(f), tl[f])
                rows.append((f, ok, r["confidence"].get(f"{code}.{f}", 0) if ln else 0))
    df = pd.DataFrame(rows, columns=["field", "correct", "confidence"])
    n_truth_lines = sum(1 for (cid, _) in line_truth if cid in ids)
    n_found_lines = sum(1 for r in records for ln in r["lines"]
                        if (r["contract_id"], ln["product_code"]) in line_truth)
    return {
        "contracts": len(records),
        "fields": len(df),
        "accuracy": df["correct"].mean(),
        "by_field": df.groupby("field")["correct"].mean().reindex(HEADER_FIELDS + LINE_FIELDS),
        "lines_found": f"{n_found_lines}/{n_truth_lines}",
        "extra_lines": sum(len(r["lines"]) for r in records) - n_found_lines,
        "review_share": (df["confidence"] < config.CONFIDENCE_CUTOFF).mean(),
        "confident_but_wrong": ((df["confidence"] >= config.CONFIDENCE_CUTOFF) & ~df["correct"]).sum(),
        "wrong_caught_by_queue": ((df["confidence"] < config.CONFIDENCE_CUTOFF) & ~df["correct"]).sum(),
        "wrong": (~df["correct"]).sum(),
    }


# ==========================================================================
# Detection precision / recall
# ==========================================================================

def evaluate_detection(flags: pd.DataFrame, planted: pd.DataFrame, visible_only: bool = False) -> dict:
    """Score flags against planted leaks, per leak type and per direction."""
    planted = planted[planted["impact_usd"] != 0].copy()
    planted["direction"] = planted["impact_usd"].map(lambda x: "Under-billed" if x > 0 else "Over-billed")
    flags = flags.copy()
    if visible_only:
        planted = planted[planted["impact_usd"].abs() >= config.MIN_FLAG_USD]
        flags = flags[~flags["below_threshold"]]

    key_p = planted[["invoice_line_id", "leak_type", "direction", "impact_usd"]].rename(
        columns={"leak_type": "rule_id", "impact_usd": "planted_usd"})
    merged = flags.merge(key_p, on=["invoice_line_id", "rule_id"], how="outer",
                         suffixes=("", "_planted"), indicator=True)
    merged["direction"] = merged["direction"].fillna(merged["direction_planted"])

    results = {}
    for direction in ("Under-billed", "Over-billed"):
        m = merged[merged["direction"] == direction]
        table = []
        for rule in sorted(set(m["rule_id"].dropna())) + ["All"]:
            g = m if rule == "All" else m[m["rule_id"] == rule]
            tp = (g["_merge"] == "both").sum()
            fp = (g["_merge"] == "left_only").sum()
            fn = (g["_merge"] == "right_only").sum()
            planted_usd = g["planted_usd"].abs().sum()
            captured = g.loc[g["_merge"] == "both", "planted_usd"].abs().sum()
            table.append({"rule_id": rule, "planted": tp + fn, "flagged": tp + fp, "TP": tp, "FP": fp, "FN": fn,
                          "precision": tp / (tp + fp) if tp + fp else float("nan"),
                          "recall": tp / (tp + fn) if tp + fn else float("nan"),
                          "planted_usd": planted_usd, "usd_captured": captured,
                          "usd_captured_pct": captured / planted_usd if planted_usd else float("nan"),
                          "flagged_usd": g.loc[g["_merge"] != "right_only", "impact_usd"].abs().sum()})
        results[direction] = pd.DataFrame(table)
    results["merged"] = merged
    results["false_positives"] = merged[merged["_merge"] == "left_only"]
    results["false_negatives"] = merged[merged["_merge"] == "right_only"]
    return results


def detect_with(records: list[dict], invoices, usage) -> pd.DataFrame:
    """Run matching + detection in memory on one extractor's output (no files written)."""
    _, cmap = build_matches(invoices, records)
    return detect(invoices, cmap, prepare_contracts(records), usage)


# ==========================================================================
# Report
# ==========================================================================

def _pct(x) -> str:
    return "n/a" if pd.isna(x) else f"{x:.1%}"


def _table(df: pd.DataFrame) -> str:
    lines = ["| Leak type | Planted | Flagged | TP | FP | FN | Precision | Recall | Planted USD | USD captured |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in df.itertuples():
        name = "**All**" if r.rule_id == "All" else f"{r.rule_id} {config.LEAK_TYPES[r.rule_id][0]}"
        lines.append(f"| {name} | {r.planted:,} | {r.flagged:,} | {r.TP:,} | {r.FP:,} | {r.FN:,} | "
                     f"{_pct(r.precision)} | {_pct(r.recall)} | {r.planted_usd:,.0f} | "
                     f"{r.usd_captured:,.0f} ({_pct(r.usd_captured_pct)}) |")
    return "\n".join(lines)


def run() -> dict:
    header_truth, line_truth = load_truth()
    planted_all = pd.read_csv(config.TRUTH_LEAKS_CSV, dtype={"invoice_line_id": str})
    invoices, usage = load_invoices(), load_usage()
    md = ["# Evaluation report", "",
          "Generated by `python -m src.evaluate`. Ground truth: `data/planted_leaks.csv`, "
          "`data/contracts.csv`, `data/contract_lines.csv` (never read by detection).", "",
          f"Targets (NFR-01): recall ≥ {TARGET:.0%}, precision ≥ {TARGET:.0%}; "
          "extraction accuracy ≥ 95% of fields (BRD success metrics).", ""]

    extractors = [n for n in ("regex", "ollama", "anthropic")
                  if (config.OUTPUT_DIR / f"extracted_contracts_{n}.json").exists()]
    summary_rows, results = [], {}
    for name in extractors:
        records = json.loads((config.OUTPUT_DIR / f"extracted_contracts_{name}.json").read_text(encoding="utf-8"))
        ext = evaluate_extraction(records, header_truth, line_truth)
        ids = {r["contract_id"] for r in records}
        planted = planted_all[planted_all["contract_id"].isin(ids)]
        flags = detect_with(records, invoices, usage)
        det = evaluate_detection(flags, planted)
        det_visible = evaluate_detection(flags, planted, visible_only=True)
        results[name] = {"extraction": ext, "detection": det, "detection_visible": det_visible}
        allrow = det["Under-billed"].iloc[-1]
        summary_rows.append((name, ext, allrow))
        print(f"[{name}] contracts {ext['contracts']} | extraction accuracy {ext['accuracy']:.1%} | "
              f"leakage precision {allrow.precision:.1%} recall {allrow.recall:.1%} "
              f"USD captured {allrow.usd_captured_pct:.1%}")

    # --- Summary
    md += ["## Summary: regex vs LLM", "",
           "| Extractor | Contracts | Field accuracy | Confident but wrong | Leakage precision | Leakage recall | USD captured |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for name, ext, a in summary_rows:
        md.append(f"| {name} | {ext['contracts']} | {_pct(ext['accuracy'])} | {ext['confident_but_wrong']} | "
                  f"{_pct(a.precision)} | {_pct(a.recall)} | {_pct(a.usd_captured_pct)} |")
    md += ["", "*Confident but wrong* = extracted values with confidence ≥ 0.8 that do not match the "
           "truth. These are the dangerous ones, because they skip the review queue.", ""]

    # --- Details per extractor
    for name in extractors:
        ext, det, vis = (results[name][k] for k in ("extraction", "detection", "detection_visible"))
        md += [f"## {name} extractor", "", "### Extraction accuracy (field level)", "",
               f"- Contracts: {ext['contracts']}, fields checked: {ext['fields']:,}, "
               f"overall accuracy: **{_pct(ext['accuracy'])}**",
               f"- Product lines found: {ext['lines_found']}, extra lines invented: {ext['extra_lines']}",
               f"- Fields sent to the review queue (< 0.8): {_pct(ext['review_share'])}",
               f"- Wrong values: {ext['wrong']} — caught by the review queue: {ext['wrong_caught_by_queue']}, "
               f"confident but wrong: {ext['confident_but_wrong']}", "",
               "| Field | Accuracy |", "|---|---:|"]
        md += [f"| {f} | {_pct(v)} |" for f, v in ext["by_field"].items()]
        md += ["", "### Leak detection — leakage (customer under-billed), all logged flags", "",
               _table(det["Under-billed"]), "",
               "### Leak detection — over-billing (reported separately)", "",
               _table(det["Over-billed"]), "",
               f"### Default view only (flags and planted leaks ≥ USD {config.MIN_FLAG_USD:.0f})", "",
               _table(vis["Under-billed"]), ""]

    # --- False positives / negatives for the regex baseline (the production configuration)
    main = "regex" if "regex" in results else extractors[0]
    det = results[main]["detection"]
    fp, fn = det["false_positives"], det["false_negatives"]
    fp.to_csv(config.OUTPUT_DIR / "eval_false_positives.csv", index=False)
    fn.to_csv(config.OUTPUT_DIR / "eval_false_negatives.csv", index=False)

    # Evaluation outcomes for the Power BI Recall / Precision measures
    ev = det["merged"].copy()
    ev["outcome"] = ev["_merge"].map({"both": "TP", "left_only": "FP", "right_only": "FN"})
    config.POWERBI_DIR.mkdir(parents=True, exist_ok=True)
    ev[["invoice_line_id", "rule_id", "direction", "outcome", "planted_usd", "impact_usd"]].rename(
        columns={"impact_usd": "flagged_usd"}).to_csv(config.POWERBI_DIR / "eval_results.csv", index=False)
    md += [f"## False positives and negatives ({main} extractor)", "",
           f"Saved to `output/eval_false_positives.csv` ({len(fp)}) and "
           f"`output/eval_false_negatives.csv` ({len(fn)}).", "",
           "| Rule | False positives | False negatives |", "|---|---:|---:|"]
    for rule in sorted(set(fp["rule_id"].dropna()) | set(fn["rule_id"].dropna())):
        md.append(f"| {rule} | {(fp['rule_id'] == rule).sum()} | {(fn['rule_id'] == rule).sum()} |")
    md += ["", "<!-- ANALYSIS -->", ""]

    report_path = config.OUTPUT_DIR / "evaluation_report.md"
    analysis = ""
    if report_path.exists():                         # keep the hand-written analysis between runs
        old = report_path.read_text(encoding="utf-8")
        if "<!-- ANALYSIS -->" in old:
            analysis = old.split("<!-- ANALYSIS -->", 1)[1]
    report_path.write_text("\n".join(md).replace("<!-- ANALYSIS -->\n", "<!-- ANALYSIS -->" + analysis, 1)
                           if analysis else "\n".join(md), encoding="utf-8")
    print(f"Report written to {report_path.relative_to(config.ROOT)}")
    return results


if __name__ == "__main__":
    run()
