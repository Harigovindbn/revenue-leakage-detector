"""
Generate the Power BI dashboard as a Power BI Project (PBIP) folder.

    python tools/build_powerbi_project.py

Creates powerbi/RevenueLeakage.pbip, which opens in Power BI Desktop:
  * RevenueLeakage.SemanticModel  tables (loaded from output/powerbi/*.csv by Power Query),
                                  relationships, date table and all DAX measures (TMDL)
  * RevenueLeakage.Report         three pages: Executive summary, Flag review,
                                  Root causes & tax (PBIR JSON)

The CSV folder is a Power Query parameter called DataFolder. It defaults to this
repository's output/powerbi folder; change it in Power BI Desktop under
Transform data > Edit parameters if you move the project.
"""

import csv
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import config  # noqa: E402

OUT = ROOT / "powerbi"
NAME = "RevenueLeakage"
SM = OUT / f"{NAME}.SemanticModel"
RP = OUT / f"{NAME}.Report"
SCHEMA = "https://developer.microsoft.com/json-schemas/fabric"
HOME = "fact_flags"                      # table that holds the measures
BASE_THEME = "CY24SU10"

TABLES = ["fact_flags", "fact_invoice_lines", "dim_customer", "dim_contract",
          "dim_leak_type", "dim_date", "eval_results"]

DATE_COLS = {"date_key", "date", "start_date", "end_date", "escalation_date"}
INT_COLS = {"quantity", "flag_count", "year", "month", "product_count", "contracts", "sort_order"}
BOOL_COLS = {"is_visible", "has_discount", "has_usage_tier"}
DOUBLE_COLS = {"impact_usd", "expected_amount", "billed_amount", "priority_score", "net_amount",
               "unit_price", "discount_amount", "tax_amount", "tax_rate", "confidence", "match_score",
               "standard_tax_rate", "escalation_pct", "planted_usd", "flagged_usd",
               "min_extraction_confidence"}
SUM_COLS = {"net_amount", "tax_amount", "discount_amount", "quantity", "planted_usd", "flagged_usd"}
MONEY_COLS = {"impact_usd", "expected_amount", "billed_amount", "net_amount", "unit_price",
              "discount_amount", "tax_amount", "planted_usd", "flagged_usd"}

# ---------------------------------------------------------------------------
# DAX measures: (name, expression, format, folder)
# ---------------------------------------------------------------------------
# KEEPFILTERS so an outer status filter (e.g. Confirmed Leakage) still applies
LEAK = ('fact_flags[direction] = "Under-billed", fact_flags[is_visible] = TRUE(), '
        'KEEPFILTERS(fact_flags[status] <> "False alarm")')
EVAL_UNDER = 'eval_results[direction] = "Under-billed"'
MEASURES = [
    ("Total Leakage", f"CALCULATE(SUM(fact_flags[impact_usd]), {LEAK})", "$#,0", "1 Leakage"),
    ("Confirmed Leakage", 'CALCULATE([Total Leakage], fact_flags[status] = "Confirmed")', "$#,0", "1 Leakage"),
    ("Unreviewed Leakage", 'CALCULATE([Total Leakage], fact_flags[status] = "Unreviewed")', "$#,0", "1 Leakage"),
    ("Needs Info Leakage", 'CALCULATE([Total Leakage], fact_flags[status] = "Needs info")', "$#,0", "1 Leakage"),
    ("Total Revenue", "SUM(fact_invoice_lines[net_amount])", "$#,0", "1 Leakage"),
    ("Leakage % of Revenue", "DIVIDE([Total Leakage], [Total Revenue])", "0.00%", "1 Leakage"),
    ("Leakage Flags", f"CALCULATE(COUNTROWS(fact_flags), {LEAK})", "#,0", "1 Leakage"),
    ("Hidden Flags", "CALCULATE(COUNTROWS(fact_flags), fact_flags[is_visible] = FALSE())", "#,0", "1 Leakage"),
    ("Over-billing", 'CALCULATE(-SUM(fact_flags[impact_usd]), fact_flags[direction] = "Over-billed", '
                     'fact_flags[is_visible] = TRUE())', "$#,0", "1 Leakage"),
    ("L1 Leakage", 'CALCULATE([Total Leakage], fact_flags[rule_id] = "L1")', "$#,0", "2 Root causes"),
    ("L2 Leakage", 'CALCULATE([Total Leakage], fact_flags[rule_id] = "L2")', "$#,0", "2 Root causes"),
    ("Leakage Prev Month", "CALCULATE([Total Leakage], DATEADD(dim_date[date_key], -1, MONTH))", "$#,0", "2 Root causes"),
    ("Leakage MoM Change", "[Total Leakage] - [Leakage Prev Month]", "$#,0", "2 Root causes"),
    ("Leakage MoM %", "DIVIDE([Leakage MoM Change], [Leakage Prev Month])", "0.0%", "2 Root causes"),
    ("Tax Under-charged", 'CALCULATE(SUM(fact_flags[impact_usd]), fact_flags[rule_id] = "L5", '
                          'fact_flags[tax_direction] = "Under-charged", fact_flags[is_visible] = TRUE())', "$#,0", "3 Tax"),
    ("Tax Over-charged", 'CALCULATE(-SUM(fact_flags[impact_usd]), fact_flags[rule_id] = "L5", '
                         'fact_flags[tax_direction] = "Over-charged", fact_flags[is_visible] = TRUE())', "$#,0", "3 Tax"),
    ("L5 Flags", 'CALCULATE(COUNTROWS(fact_flags), fact_flags[rule_id] = "L5", fact_flags[is_visible] = TRUE())', "#,0", "3 Tax"),
    ("Priority Score", "SUM(fact_flags[priority_score])", "#,0", "4 Flag review"),
    ("Avg Confidence", "AVERAGE(fact_flags[confidence])", "0.00", "4 Flag review"),
    ("Expected Amount", "SUM(fact_flags[expected_amount])", "$#,0.00", "4 Flag review"),
    ("Billed Amount", "SUM(fact_flags[billed_amount])", "$#,0.00", "4 Flag review"),
    ("Flag Impact", "SUM(fact_flags[impact_usd])", "$#,0.00", "4 Flag review"),
    ("Evidence", 'IF(HASONEVALUE(fact_flags[flag_id]), SELECTEDVALUE(fact_flags[evidence_clause]), '
                 '"Click one flag in the table to see the contract clause it relied on.")', None, "4 Flag review"),
    ("Rule Detail", 'IF(HASONEVALUE(fact_flags[flag_id]), SELECTEDVALUE(fact_flags[rule_detail]), "")', None, "4 Flag review"),
    ("Invoice Match Rate", 'DIVIDE(CALCULATE(COUNTROWS(fact_invoice_lines), fact_invoice_lines[match_status] = "Matched"), '
                           'COUNTROWS(fact_invoice_lines))', "0.0%", "5 Quality"),
    ("Unmatched Revenue", 'CALCULATE([Total Revenue], fact_invoice_lines[match_status] = "Unmatched")', "$#,0", "5 Quality"),
    ("Recall", f'DIVIDE(CALCULATE(COUNTROWS(eval_results), eval_results[outcome] = "TP", {EVAL_UNDER}), '
               f'CALCULATE(COUNTROWS(eval_results), eval_results[outcome] IN {{"TP", "FN"}}, {EVAL_UNDER}))', "0.0%", "5 Quality"),
    ("Precision", f'DIVIDE(CALCULATE(COUNTROWS(eval_results), eval_results[outcome] = "TP", {EVAL_UNDER}), '
                  f'CALCULATE(COUNTROWS(eval_results), eval_results[outcome] IN {{"TP", "FP"}}, {EVAL_UNDER}))', "0.0%", "5 Quality"),
    ("USD Captured %", f'DIVIDE(CALCULATE(SUM(eval_results[planted_usd]), eval_results[outcome] = "TP", {EVAL_UNDER}), '
                       f'CALCULATE(SUM(eval_results[planted_usd]), eval_results[outcome] IN {{"TP", "FN"}}, {EVAL_UNDER}))', "0.0%", "5 Quality"),
    ("Review Precision", 'DIVIDE(CALCULATE(COUNTROWS(fact_flags), fact_flags[status] = "Confirmed"), '
                         'CALCULATE(COUNTROWS(fact_flags), fact_flags[status] IN {"Confirmed", "False alarm"}))', "0.0%", "5 Quality"),
]

RELATIONSHIPS = [
    ("fact_flags", "date_key", "dim_date", "date_key"),
    ("fact_flags", "customer_key", "dim_customer", "customer_key"),
    ("fact_flags", "contract_id", "dim_contract", "contract_id"),
    ("fact_flags", "rule_id", "dim_leak_type", "rule_id"),
    ("fact_invoice_lines", "date_key", "dim_date", "date_key"),
    ("fact_invoice_lines", "customer_key", "dim_customer", "customer_key"),
    ("fact_invoice_lines", "contract_id", "dim_contract", "contract_id"),
    ("eval_results", "rule_id", "dim_leak_type", "rule_id"),
]


# ===========================================================================
# Semantic model (TMDL)
# ===========================================================================

def headers(table: str) -> list[str]:
    with (config.POWERBI_DIR / f"{table}.csv").open(encoding="utf-8") as f:
        return next(csv.reader(f))


def col_type(c: str) -> tuple[str, str]:
    """(TMDL dataType, Power Query type) for a column name."""
    if c in DATE_COLS:
        return "dateTime", "type date"
    if c in INT_COLS:
        return "int64", "Int64.Type"
    if c in BOOL_COLS:
        return "boolean", "type logical"
    if c in DOUBLE_COLS:
        return "double", "type number"
    return "string", "type text"


def table_tmdl(table: str) -> str:
    cols = headers(table)
    lines = [f"table {table}"]
    if table == "dim_date":
        lines.append("\tdataCategory: Time")
    lines.append("")
    if table == HOME:
        for name, expr, fmt, folder in MEASURES:
            lines.append(f"\tmeasure '{name}' = {expr}")
            if fmt:
                lines.append(f"\t\tformatString: {fmt}")
            lines.append(f"\t\tdisplayFolder: {folder}")
            lines.append("")
    for c in cols:
        dtype, _ = col_type(c)
        lines.append(f"\tcolumn {c}")
        lines.append(f"\t\tdataType: {dtype}")
        if table == "dim_date" and c == "date_key":
            lines.append("\t\tisKey")
        if dtype == "dateTime":
            lines.append("\t\tformatString: yyyy-mm-dd")
        elif c in MONEY_COLS:
            lines.append("\t\tformatString: $#,0.00")
        summarize = "sum" if c in SUM_COLS and table == "fact_invoice_lines" else "none"
        lines.append(f"\t\tsummarizeBy: {summarize}")
        lines.append(f"\t\tsourceColumn: {c}")
        if table == "dim_date" and c == "month_name":
            lines.append("\t\tsortByColumn: month")
        if table == "dim_leak_type" and c == "leak_type":
            lines.append("\t\tsortByColumn: sort_order")
        lines.append("")
    types = ", ".join(f'{{"{c}", {col_type(c)[1]}}}' for c in cols)
    m = (f'let Source = Csv.Document(File.Contents(DataFolder & "{table}.csv"), '
         f'[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]), '
         f'Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]), '
         f'Typed = Table.TransformColumnTypes(Promoted, {{{types}}}, "en-US") in Typed')
    lines += [f"\tpartition {table} = m", "\t\tmode: import", f"\t\tsource = {m}", ""]
    return "\n".join(lines)


def build_semantic_model() -> None:
    d = SM / "definition"
    (d / "tables").mkdir(parents=True, exist_ok=True)
    (SM / "definition.pbism").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {}}, indent=2), encoding="utf-8")
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1600\n", encoding="utf-8")
    (d / "model.tmdl").write_text(
        "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
        "\tsourceQueryCulture: en-US\n\tdiscourageImplicitMeasures\n\n"
        + "".join(f"ref table {t}\n" for t in TABLES), encoding="utf-8")
    folder = str(config.POWERBI_DIR) + "\\"
    (d / "expressions.tmdl").write_text(
        f'expression DataFolder = "{folder}" meta [IsParameterQuery=true, Type="Text", '
        f'IsParameterQueryRequired=true]\n', encoding="utf-8")
    rels = []
    for i, (ft, fc, tt, tc) in enumerate(RELATIONSHIPS, 1):
        rels.append(f"relationship rel_{i:02d}_{ft}_{tt}\n\tfromColumn: {ft}.{fc}\n\ttoColumn: {tt}.{tc}\n")
    (d / "relationships.tmdl").write_text("\n".join(rels), encoding="utf-8")
    for t in TABLES:
        (d / "tables" / f"{t}.tmdl").write_text(table_tmdl(t), encoding="utf-8")


# ===========================================================================
# Report (PBIR JSON)
# ===========================================================================

def lit(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


def field(kind: str, table: str, prop: str) -> dict:
    return {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": prop}}


def C(table, prop):  # column
    return ("Column", table, prop)


def M(prop):         # measure
    return ("Measure", HOME, prop)


def visual(name, vtype, x, y, w, h, roles=None, title=None, sort=None, objects=None, z=0):
    v = {"visualType": vtype}
    if roles:
        qs = {}
        for role, items in roles.items():
            qs[role] = {"projections": [
                {"field": field(k, t, p), "queryRef": f"{t}.{p}", "nativeQueryRef": p}
                for k, t, p in items]}
        v["query"] = {"queryState": qs}
        if sort:
            k, t, p, direction = sort
            v["query"]["sortDefinition"] = {"sort": [{"field": field(k, t, p), "direction": direction}],
                                            "isDefaultSort": False}
    if objects:
        v["objects"] = objects
    if title:
        v["visualContainerObjects"] = {"title": [{"properties": {
            "show": lit("true"), "text": lit(f"'{title}'")}}]}
    v["drillFilterOtherVisuals"] = True
    return {"$schema": f"{SCHEMA}/item/report/definition/visualContainer/2.0.0/schema.json",
            "name": name, "position": {"x": x, "y": y, "z": z, "width": w, "height": h, "tabOrder": z},
            "visual": v}


def textbox(name, text, x=16, y=6, w=900, h=44):
    v = visual(name, "textbox", x, y, w, h)
    v["visual"]["objects"] = {"general": [{"properties": {"paragraphs": [{"textRuns": [{
        "value": text, "textStyle": {"fontSize": "18pt", "fontWeight": "bold"}}]}]}}]}
    del v["visual"]["drillFilterOtherVisuals"]
    return v


def slicer(name, table, prop, x, title, y=56, w=300, h=58):
    return visual(name, "slicer", x, y, w, h, {"Values": [C(table, prop)]}, title,
                  objects={"data": [{"properties": {"mode": lit("'Dropdown'")}}]})


def card(name, measure, x, y, w=240, h=100):
    return visual(name, "card", x, y, w, h, {"Values": [M(measure)]}, measure)


def bar(name, cat, meas, x, y, w, h, title):
    return visual(name, "clusteredBarChart", x, y, w, h, {"Category": [cat], "Y": [M(meas)]}, title,
                  sort=("Measure", HOME, meas, "Descending"))


def categorical_filter(name, table, prop, values):
    return {"name": name, "field": field("Column", table, prop), "type": "Categorical",
            "filter": {"Version": 2, "From": [{"Name": "f", "Entity": table, "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "f"}},
                                                       "Property": prop}}],
                           "Values": [[{"Literal": {"Value": v}}] for v in values]}}}]}}


def pages() -> list[dict]:
    """Three report pages: (name, display name, visuals, page filters)."""
    exec_v = [
        textbox("title", "Revenue leakage: executive summary"),
        slicer("slicer_quarter", "dim_date", "year_quarter", 16, "Quarter"),
        slicer("slicer_country", "dim_customer", "country_name", 332, "Country"),
        slicer("slicer_type", "dim_leak_type", "leak_type", 648, "Leak type"),
        card("card_total", "Total Leakage", 16, 124),
        card("card_confirmed", "Confirmed Leakage", 265, 124),
        card("card_unreviewed", "Unreviewed Leakage", 514, 124),
        card("card_pct", "Leakage % of Revenue", 763, 124),
        card("card_over", "Over-billing", 1012, 124, w=252),
        visual("line_month", "lineChart", 16, 236, 740, 232,
               {"Category": [C("dim_date", "year_month")], "Y": [M("Total Leakage")]},
               "Leakage by month (growing as errors compound)"),
        bar("bar_type", C("dim_leak_type", "leak_type"), "Total Leakage", 768, 236, 496, 232, "Leakage by type"),
        bar("bar_customer", C("dim_customer", "customer_name"), "Total Leakage", 16, 480, 740, 232,
            "Customers with the most leakage"),
        visual("donut_status", "donutChart", 768, 480, 496, 232,
               {"Category": [C("fact_flags", "status")], "Y": [M("Total Leakage")]}, "Leakage by review status"),
    ]
    review_v = [
        textbox("title", "Flag review: work the list from the top"),
        slicer("slicer_status", "fact_flags", "status", 16, "Review status"),
        slicer("slicer_type", "dim_leak_type", "leak_type", 332, "Leak type"),
        slicer("slicer_country", "dim_customer", "country_name", 648, "Country"),
        visual("table_flags", "tableEx", 16, 124, 820, 588,
               {"Values": [C("fact_flags", "flag_id"), C("fact_flags", "invoice_id"),
                           C("dim_customer", "customer_name"), C("dim_leak_type", "leak_type"),
                           C("fact_flags", "impact_usd"), C("fact_flags", "confidence"),
                           C("fact_flags", "priority_score"), C("fact_flags", "status")]},
               "Flags ranked by priority (impact x confidence). Click a row to see its evidence.",
               sort=("Column", "fact_flags", "priority_score", "Descending")),
        visual("card_amounts", "multiRowCard", 848, 124, 416, 150,
               {"Values": [M("Expected Amount"), M("Billed Amount"), M("Flag Impact")]},
               "Selected flag: expected vs billed"),
        visual("table_evidence", "tableEx", 848, 286, 416, 426,
               {"Values": [M("Evidence"), M("Rule Detail")]},
               "Evidence: contract clause and the rule that fired",
               objects={"values": [{"properties": {"wordWrap": lit("true")}}],
                        "columnHeaders": [{"properties": {"wordWrap": lit("true")}}]}),
    ]
    review_filters = [categorical_filter("filter_direction", "fact_flags", "direction", ["'Under-billed'"]),
                      categorical_filter("filter_visible", "fact_flags", "is_visible", ["true"])]
    root_v = [
        textbox("title", "Root causes and tax"),
        visual("line_type_month", "lineChart", 16, 56, 740, 260,
               {"Category": [C("dim_date", "year_month")], "Y": [M("Total Leakage")],
                "Series": [C("dim_leak_type", "leak_type")]}, "Month-on-month leakage by type"),
        visual("matrix_type_year", "pivotTable", 768, 56, 496, 260,
               {"Rows": [C("dim_leak_type", "leak_type")], "Columns": [C("dim_date", "year")],
                "Values": [M("Total Leakage")]}, "Leakage by type and year"),
        bar("bar_owner", C("dim_leak_type", "process_owner"), "Total Leakage", 16, 328, 364, 190,
            "Leakage by process owner"),
        bar("bar_esc_wording", C("dim_contract", "escalation_wording"), "L1 Leakage", 392, 328, 364, 190,
            "Missed escalations (L1) by clause wording"),
        bar("bar_disc_wording", C("dim_contract", "discount_wording"), "L2 Leakage", 768, 328, 496, 190,
            "Expired discounts (L2) by clause wording"),
        visual("matrix_tax", "pivotTable", 16, 530, 740, 182,
               {"Rows": [C("dim_customer", "country_name")],
                "Values": [M("Tax Under-charged"), M("Tax Over-charged"), M("L5 Flags")]},
               "Wrong tax rate (L5) by country: under- and over-charged"),
        card("card_match", "Invoice Match Rate", 768, 530, 244, 88),
        card("card_recall", "Recall", 1020, 530, 244, 88),
        card("card_precision", "Precision", 768, 624, 244, 88),
        card("card_usd", "USD Captured %", 1020, 624, 244, 88),
    ]
    return [("executive_summary", "Executive summary", exec_v, []),
            ("flag_review", "Flag review", review_v, review_filters),
            ("root_causes_tax", "Root causes & tax", root_v, [])]


def _desktop_theme_file() -> Path | None:
    """Find the base theme JSON shipped with Power BI Desktop (Store or installer version)."""
    import subprocess
    roots = [Path("C:/Program Files/Microsoft Power BI Desktop")]
    try:   # the Store app folder cannot be listed, so ask Windows where it is installed
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-AppxPackage -Name *MicrosoftPowerBIDesktop*).InstallLocation"],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        if out:
            roots.insert(0, Path(out.splitlines()[0]))
    except (OSError, subprocess.SubprocessError):
        pass
    for root in roots:
        f = root / "bin" / "WebView2Resources" / "minerva" / "sharedresources" / "BaseThemes" / f"{BASE_THEME}.json"
        if f.exists():
            return f
    return None


def build_report() -> None:
    d = RP / "definition"
    if d.exists():
        shutil.rmtree(d)
    (d / "pages").mkdir(parents=True)
    (RP / "definition.pbir").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}},
        indent=2), encoding="utf-8")
    (d / "version.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0"}, indent=2), encoding="utf-8")
    (d / "report.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/report/3.0.0/schema.json",
        "themeCollection": {"baseTheme": {"name": BASE_THEME,
                                          "reportVersionAtImport": {"visual": "1.8.50", "report": "2.0.50", "page": "1.3.50"},
                                          "type": "SharedResources"}},
        "resourcePackages": [{"name": "SharedResources", "type": "SharedResources",
                              "items": [{"name": BASE_THEME, "path": f"BaseThemes/{BASE_THEME}.json",
                                         "type": "BaseTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True}},
        indent=2), encoding="utf-8")

    # Base theme shipped with Power BI Desktop (copied if Desktop is installed)
    theme_dst = RP / "StaticResources" / "SharedResources" / "BaseThemes" / f"{BASE_THEME}.json"
    if not theme_dst.exists():
        src = _desktop_theme_file()
        if src:
            theme_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, theme_dst)
        else:
            print("  note: Power BI Desktop not found; base theme file not copied")

    order = []
    for page_name, display, visuals, filters in pages():
        pd_ = d / "pages" / page_name
        (pd_ / "visuals").mkdir(parents=True)
        page = {"$schema": f"{SCHEMA}/item/report/definition/page/2.0.0/schema.json",
                "name": page_name, "displayName": display, "displayOption": "FitToPage",
                "height": 720, "width": 1280}
        if filters:
            page["filterConfig"] = {"filters": filters}
        (pd_ / "page.json").write_text(json.dumps(page, indent=2), encoding="utf-8")
        for z, v in enumerate(visuals):
            v["position"]["z"] = v["position"]["tabOrder"] = z * 1000
            (pd_ / "visuals" / v["name"]).mkdir()
            (pd_ / "visuals" / v["name"] / "visual.json").write_text(json.dumps(v, indent=2), encoding="utf-8")
        order.append(page_name)
    (d / "pages" / "pages.json").write_text(json.dumps({
        "$schema": f"{SCHEMA}/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": order, "activePageName": order[0]}, indent=2), encoding="utf-8")


def main() -> None:
    missing = [t for t in TABLES if not (config.POWERBI_DIR / f"{t}.csv").exists()]
    if missing:
        sys.exit(f"Run the pipeline first; missing {missing} in output/powerbi")
    OUT.mkdir(exist_ok=True)
    build_semantic_model()
    build_report()
    (OUT / f"{NAME}.pbip").write_text(json.dumps({
        "$schema": f"{SCHEMA}/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}],
        "settings": {"enableAutoRecovery": True}}, indent=2), encoding="utf-8")
    (OUT / ".gitignore").write_text("**/.pbi/localSettings.json\n**/.pbi/cache.abf\n", encoding="utf-8")
    print(f"Power BI project written to {OUT.relative_to(ROOT)}\\{NAME}.pbip "
          f"({len(MEASURES)} measures, {len(RELATIONSHIPS)} relationships, 3 pages)")


if __name__ == "__main__":
    main()
