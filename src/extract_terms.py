"""
Contract term extraction (FR-01 to FR-03).

Turns each contract .txt into one structured record:

    customer_name, country, start_date, end_date, escalation_pct, escalation_date,
    lines: [product_code, product_name, quantity, unit_price, discount_pct,
            discount_end_date, tier_limit, overage_rate]
    confidence: {field: 0..1}     evidence: {field: contract sentence}

Three extractors share one interface (`extract(text) -> raw dict`):
  * RegexExtractor      free, deterministic baseline
  * AnthropicExtractor  Claude with structured JSON output (needs ANTHROPIC_API_KEY)
  * OllamaExtractor     free local model via Ollama's structured output

Every raw result then goes through the same `finalize()` step, which
*grounds* each value: it looks for the value in the contract text. A value
that cannot be found there gets its confidence capped at 0.5, so a
hallucinated number always lands in the review queue (US-05).

Run on its own:
    python -m src.extract_terms --extractor regex
    python -m src.extract_terms --extractor ollama --limit 50 --no-activate
"""

import argparse
import csv
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from src import config
from src.common import DATE_TEXT_FORMAT, add_months, ensure_output_dirs, parse_date

LINE_FIELDS = ["quantity", "unit_price", "discount_pct", "discount_end_date",
               "tier_limit", "overage_rate"]
HEADER_FIELDS = ["customer_name", "country", "start_date", "end_date",
                 "escalation_pct", "escalation_date"]
PROMPT_VERSION = "v1"          # bump to invalidate the LLM cache after a prompt change
UNGROUNDED_CAP = 0.5           # confidence ceiling for a value not found in the text

MONTHS = ("January|February|March|April|May|June|July|August|September|"
          "October|November|December")
DATE_RE = rf"(\d{{1,2}}\s+(?:{MONTHS})\s+\d{{4}})"
PCT_RE = r"(\d+(?:\.\d+)?)\s*(?:%|percent)"
ESCALATION_WORDS = re.compile(r"increase|rise|uplift|adjust|escalat", re.I)
DISCOUNT_WORDS = re.compile(r"discount|\boff\b|introductory|promotional", re.I)
TIER_WORDS = re.compile(r"allowance|includes up to|beyond|excess|overage", re.I)


# ==========================================================================
# Small text helpers
# ==========================================================================

def num(text: str) -> float:
    """'1,234.50' -> 1234.5"""
    return float(text.replace(",", ""))


def fmt_num(v: float) -> str:
    """4.0 -> '4', 3.5 -> '3.5', 0.006 -> '0.006' (how numbers are written in contracts)."""
    return f"{v:g}"


def number_pattern(v: float, thousands: bool = False) -> str:
    """Regex that matches the number v as written, without matching inside a bigger number."""
    text = f"{int(v):,}" if thousands and float(v).is_integer() else fmt_num(v)
    return rf"(?<![\d.,]){re.escape(text)}(?:\.0+)?(?![\d])"


def clause_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def product_name_map(text: str) -> dict[str, str]:
    """Product name -> code, from the services list ('- Platform licence [PLAT-BASE]: ...')."""
    found = re.findall(r"^-\s*(.+?)\s*\[([A-Z0-9-]+)\]", text, re.M)
    return {name.strip(): code for name, code in found}


def products_in(line: str, names: dict[str, str]) -> list[str]:
    """Codes of the products named in a sentence, longest name first."""
    return [names[n] for n in sorted(names, key=len, reverse=True) if n in line]


# ==========================================================================
# Extractor 1: rule-based (regex) baseline
# ==========================================================================

class RegexExtractor:
    """Reads labelled header fields and known clause shapes. Free and instant."""

    name = "regex"

    def extract(self, text: str) -> dict:
        conf = {}
        header = {
            "customer_name": self._label(text, "Customer"),
            "country": self._label(text, "Registered country"),
            "start_date": self._label(text, "Effective Date"),
            "end_date": self._label(text, r"Term:\s*until", raw=True),
        }
        for k, v in header.items():
            conf[k] = 1.0 if v else 0.0

        names = product_name_map(text)
        lines = {}
        for m in re.finditer(r"^-\s*(?P<name>.+?)\s*\[(?P<code>[A-Z0-9-]+)\]:\s*"
                             r"(?:(?P<qty>[\d,]+)\s+units?\s+at\s+)?USD\s*(?P<price>[\d,]+(?:\.\d+)?)",
                             text, re.M):
            code = m["code"]
            lines[code] = {"product_code": code, "product_name": m["name"].strip(),
                           "quantity": int(num(m["qty"])) if m["qty"] else 1,
                           "unit_price": num(m["price"]),
                           "discount_pct": None, "discount_end_date": None,
                           "tier_limit": None, "overage_rate": None}
            conf[f"{code}.quantity"] = conf[f"{code}.unit_price"] = 1.0

        # Escalation clause: a sentence with an increase word and a percentage
        esc_pct, esc_date = None, None
        conf["escalation_pct"] = conf["escalation_date"] = 0.0
        for ln in clause_lines(text):
            pct = re.search(PCT_RE, ln)
            if pct and ESCALATION_WORDS.search(ln) and not DISCOUNT_WORDS.search(ln):
                esc_pct, conf["escalation_pct"] = float(pct[1]), 1.0
                d = re.search(DATE_RE, ln)
                if d:
                    esc_date, conf["escalation_date"] = d[1], 1.0
                elif "anniversary" in ln.lower() and header["start_date"]:
                    # "on each anniversary of the Effective Date" -> start + 12 months
                    start = parse_date(header["start_date"])
                    esc_date, conf["escalation_date"] = add_months(start, 12).isoformat(), 0.9
                break

        # Discount and usage-tier clauses, linked to products by name
        for ln in clause_lines(text):
            codes = products_in(ln, names)
            if not codes or codes[0] not in lines:
                continue
            code, line = codes[0], lines[codes[0]]
            if DISCOUNT_WORDS.search(ln) and re.search(PCT_RE, ln):
                pct, end = re.search(PCT_RE, ln), re.search(DATE_RE, ln)
                line["discount_pct"] = float(pct[1])
                line["discount_end_date"] = end[1] if end else None
                conf[f"{code}.discount_pct"] = 1.0
                conf[f"{code}.discount_end_date"] = 1.0 if end else 0.0
            elif TIER_WORDS.search(ln):
                tier = re.search(r"([\d,]{4,})\s*units", ln)
                rate = re.search(r"USD\s*(\d*\.\d+)|(\d*\.\d+)\s*USD", ln)
                line["tier_limit"] = int(num(tier[1])) if tier else None
                line["overage_rate"] = float(rate[1] or rate[2]) if rate else None
                conf[f"{code}.tier_limit"] = 1.0 if tier else 0.0
                conf[f"{code}.overage_rate"] = 1.0 if rate else 0.0

        return {**header, "escalation_pct": esc_pct, "escalation_date": esc_date,
                "lines": list(lines.values()), "confidence": conf}

    @staticmethod
    def _label(text: str, label: str, raw: bool = False) -> str | None:
        pattern = rf"^{label}\s*(.+)$" if raw else rf"^{label}\s*:\s*(.+)$"
        m = re.search(pattern, text, re.M)
        return m[1].strip() if m else None


# ==========================================================================
# Extractors 2 and 3: LLM with structured JSON output
# ==========================================================================

def _nullable(t: str) -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


LLM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": HEADER_FIELDS + ["lines", "confidence"],
    "properties": {
        "customer_name": {"type": "string"},
        "country": {"type": "string", "description": "full country name"},
        "start_date": {"type": "string", "description": "YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        "escalation_pct": _nullable("number"),
        "escalation_date": {**_nullable("string"), "description": "YYYY-MM-DD"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["product_code"] + LINE_FIELDS + ["confidence"],
                "properties": {
                    "product_code": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "unit_price": {"type": "number"},
                    "discount_pct": _nullable("number"),
                    "discount_end_date": _nullable("string"),
                    "tier_limit": _nullable("integer"),
                    "overage_rate": _nullable("number"),
                    "confidence": {"type": "number"},
                },
            },
        },
        "confidence": {
            "type": "object",
            "additionalProperties": False,
            "required": HEADER_FIELDS,
            "properties": {f: {"type": "number"} for f in HEADER_FIELDS},
        },
    },
}

LLM_INSTRUCTIONS = """You extract billing terms from a B2B subscription contract.
Return JSON that matches the schema. Rules:
- Dates as YYYY-MM-DD. country = the customer's registered country, full name.
- One entry in "lines" per product in the services list; product_code is the code in [brackets].
- quantity = number of units (1 if the line has no unit count). unit_price = monthly USD price per unit, before any discount.
- escalation_pct = the annual price increase in percent (4 for 4%). escalation_date = the date the first increase applies;
  if the contract says "each anniversary of the Effective Date", use the start date plus one year.
- discount_pct / discount_end_date: only if a discount clause names that product, else null.
- tier_limit / overage_rate: only for products with a monthly usage allowance, else null. overage_rate is USD per unit.
- confidence (0 to 1): how sure you are each value is stated in the text. Use a low score if you inferred or guessed it.
- Never invent values. Use null when the contract does not say."""


def _cached_call(extractor_name: str, model: str, contract_id: str, text: str, call) -> dict:
    """Return a cached LLM result if this exact contract text was sent before; else call and cache."""
    key = hashlib.sha256(f"{extractor_name}|{model}|{PROMPT_VERSION}|{text}".encode()).hexdigest()[:16]
    path = config.CACHE_DIR / extractor_name / f"{contract_id}_{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    result = call(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


def _from_llm_json(data: dict) -> dict:
    """Convert the LLM's JSON into the common raw-record shape."""
    conf = {k: float(v) for k, v in (data.get("confidence") or {}).items()}
    lines = []
    for ln in data.get("lines") or []:
        code = str(ln.get("product_code", "")).strip().strip("[]")
        line_conf = float(ln.get("confidence", 0.5))
        lines.append({"product_code": code, "product_name": None,
                      **{f: ln.get(f) for f in LINE_FIELDS}})
        for f in LINE_FIELDS:
            conf[f"{code}.{f}"] = line_conf if ln.get(f) is not None else 1.0
    return {**{f: data.get(f) for f in HEADER_FIELDS}, "lines": lines, "confidence": conf}


class AnthropicExtractor:
    """Claude via the official SDK, with JSON output constrained to LLM_SCHEMA."""

    name = "anthropic"

    def __init__(self):
        from dotenv import load_dotenv
        load_dotenv(config.ROOT / ".env")
        import anthropic
        self.client = anthropic.Anthropic()        # reads ANTHROPIC_API_KEY; never hard-coded
        self.model = os.getenv("ANTHROPIC_MODEL", config.ANTHROPIC_MODEL)
        self.tokens = {"input": 0, "output": 0}

    def extract(self, text: str, contract_id: str = "") -> dict:
        return _from_llm_json(_cached_call(self.name, self.model, contract_id, text, self._call))

    def _call(self, text: str) -> dict:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=8000,
            system=LLM_INSTRUCTIONS,
            messages=[{"role": "user", "content": f"<contract>\n{text}\n</contract>"}],
            output_config={"effort": "low",
                           "format": {"type": "json_schema", "schema": LLM_SCHEMA}},
            # If Claude declines a request, the API retries it on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        self.tokens["input"] += response.usage.input_tokens
        self.tokens["output"] += response.usage.output_tokens
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined to extract this contract")
        body = next(b.text for b in response.content if b.type == "text")
        return json.loads(body)


class OllamaExtractor:
    """A local model through Ollama's HTTP API (free, data never leaves the laptop)."""

    name = "ollama"

    def __init__(self):
        from dotenv import load_dotenv
        load_dotenv(config.ROOT / ".env")
        self.url = os.getenv("OLLAMA_URL", config.OLLAMA_URL)
        self.model = os.getenv("OLLAMA_MODEL", config.OLLAMA_MODEL)

    def extract(self, text: str, contract_id: str = "") -> dict:
        return _from_llm_json(_cached_call(self.name, self.model, contract_id, text, self._call))

    def _call(self, text: str) -> dict:
        import urllib.request
        payload = {
            "model": self.model, "stream": False, "format": LLM_SCHEMA,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": LLM_INSTRUCTIONS},
                         {"role": "user", "content": f"<contract>\n{text}\n</contract>"}],
        }
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            content = json.loads(resp.read())["message"]["content"]
        return json.loads(content)


EXTRACTORS = {"regex": RegexExtractor, "anthropic": AnthropicExtractor, "ollama": OllamaExtractor}


# ==========================================================================
# Shared post-processing: normalise, ground in the text, collect evidence
# ==========================================================================

def _country_code(value) -> str | None:
    if not value:
        return None
    v = str(value).strip()
    if v.upper() in config.TAX_RATES:
        return v.upper()
    return config.COUNTRY_NAME_TO_CODE.get(v.lower())


def _date_pattern(d: date) -> str:
    return rf"0?{d.day}\s+{d.strftime('%B')}\s+{d.year}"


def find_evidence(lines: list[str], field: str, value, record: dict, names: dict) -> str | None:
    """Return the contract sentence that states `value` for `field`, or None if not found."""
    if value is None:
        return None
    code, _, sub = field.partition(".") if "." in field else (None, "", field)
    pname = next((n for n, c in names.items() if c == code), None)

    def first(pred):
        return next((ln for ln in lines if pred(ln)), None)

    if field == "customer_name":
        return first(lambda ln: str(value) in ln)
    if field == "country":
        name = config.TAX_RATES.get(value, ("",))[0]
        return first(lambda ln: name and name in ln)
    if field in ("start_date", "end_date"):
        label = "Effective Date" if field == "start_date" else "Term"
        return first(lambda ln: ln.startswith(label) and re.search(_date_pattern(value), ln))
    if field == "escalation_pct":
        pat = number_pattern(value) + r"\s*(?:%|percent)"
        return first(lambda ln: ESCALATION_WORDS.search(ln) and re.search(pat, ln))
    if field == "escalation_date":
        esc_line = first(lambda ln: ESCALATION_WORDS.search(ln) and re.search(PCT_RE, ln)
                         and not DISCOUNT_WORDS.search(ln))
        if esc_line and re.search(_date_pattern(value), esc_line):
            return esc_line
        start = record.get("start_date")
        if esc_line and "anniversary" in esc_line.lower() and start and value == add_months(start, 12):
            return esc_line
        return None
    # line-level fields
    if sub == "unit_price":
        return first(lambda ln: f"[{code}]" in ln and f"USD {value:,.2f}" in ln)
    if sub == "quantity":
        if value == 1:
            return first(lambda ln: f"[{code}]" in ln and "units at" not in ln)
        return first(lambda ln: f"[{code}]" in ln and f"{value} units at" in ln)
    if not pname:
        return None
    if sub == "discount_pct":
        pat = number_pattern(value) + r"\s*(?:%|percent)"
        return first(lambda ln: pname in ln and DISCOUNT_WORDS.search(ln) and re.search(pat, ln))
    if sub == "discount_end_date":
        return first(lambda ln: pname in ln and DISCOUNT_WORDS.search(ln)
                     and re.search(_date_pattern(value), ln))
    if sub == "tier_limit":
        pat = number_pattern(value, thousands=True)
        return first(lambda ln: pname in ln and TIER_WORDS.search(ln) and re.search(pat, ln))
    if sub == "overage_rate":
        pat = number_pattern(value)
        return first(lambda ln: pname in ln and TIER_WORDS.search(ln) and re.search(pat, ln))
    return None


def finalize(contract_id: str, raw: dict, text: str, extractor: str) -> dict:
    """Normalise types, ground every value in the text, attach evidence and final confidence."""
    names = product_name_map(text)
    code_to_name = {c: n for n, c in names.items()}
    record = {
        "contract_id": contract_id,
        "source_file": f"contracts/{contract_id}.txt",
        "extractor": extractor,
        "customer_name": (raw.get("customer_name") or "").strip() or None,
        "country": _country_code(raw.get("country")),
        "start_date": parse_date(raw.get("start_date")),
        "end_date": parse_date(raw.get("end_date")),
        "escalation_pct": float(raw["escalation_pct"]) if raw.get("escalation_pct") is not None else None,
        "escalation_date": parse_date(raw.get("escalation_date")),
        "lines": [],
    }
    for ln in raw.get("lines", []):
        code = ln["product_code"]
        record["lines"].append({
            "product_code": code,
            "product_name": ln.get("product_name") or code_to_name.get(code),
            "quantity": int(ln["quantity"]) if ln.get("quantity") is not None else None,
            "unit_price": float(ln["unit_price"]) if ln.get("unit_price") is not None else None,
            "discount_pct": float(ln["discount_pct"]) if ln.get("discount_pct") is not None else None,
            "discount_end_date": parse_date(ln.get("discount_end_date")),
            "tier_limit": int(ln["tier_limit"]) if ln.get("tier_limit") is not None else None,
            "overage_rate": float(ln["overage_rate"]) if ln.get("overage_rate") is not None else None,
        })

    text_lines = clause_lines(text)
    raw_conf = raw.get("confidence", {})
    confidence, evidence = {}, {}

    def grade(field, value):
        if value is None:
            # A missing header value is a problem; a missing optional line term
            # (no discount, no usage tier) is normal and needs no review.
            optional = "." in field and field.split(".")[1] not in ("quantity", "unit_price")
            confidence[field] = round(float(raw_conf.get(field, 1.0)), 2) if optional else 0.0
            return
        c = float(raw_conf.get(field, 0.5))
        ev = find_evidence(text_lines, field, value, record, names)
        if ev is None:
            c = min(c, UNGROUNDED_CAP)
        confidence[field] = round(min(c, 1.0), 2)
        evidence[field] = ev

    for f in HEADER_FIELDS:
        grade(f, record[f])
    for ln in record["lines"]:
        for f in LINE_FIELDS:
            grade(f"{ln['product_code']}.{f}", ln[f])

    record["confidence"] = confidence
    record["evidence"] = evidence
    return record


# ==========================================================================
# Manual corrections (US-05: a corrected value replaces the extracted one)
# ==========================================================================

OVERRIDES_CSV = config.DATA_DIR / "extraction_overrides.csv"


def apply_overrides(records: list[dict]) -> int:
    """Apply data/extraction_overrides.csv (contract_id, field, corrected_value)."""
    if not OVERRIDES_CSV.exists():
        OVERRIDES_CSV.write_text("contract_id,field,corrected_value,note\n", encoding="utf-8")
        return 0
    by_id = {r["contract_id"]: r for r in records}
    applied = 0
    with OVERRIDES_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rec = by_id.get(row["contract_id"])
            if not rec:
                continue
            field, value = row["field"], row["corrected_value"]
            if "." in field:
                code, sub = field.split(".", 1)
                target = next((ln for ln in rec["lines"] if ln["product_code"] == code), None)
                if target is None:
                    target = {"product_code": code, "product_name": None,
                              **{f: None for f in LINE_FIELDS}}
                    rec["lines"].append(target)
            else:
                target, sub = rec, field
            target[sub] = (parse_date(value) if "date" in sub else
                           int(float(value)) if sub in ("quantity", "tier_limit") else
                           float(value) if sub in ("unit_price", "discount_pct", "overage_rate",
                                                   "escalation_pct") else value)
            rec["confidence"][field] = 1.0
            rec["evidence"][field] = f"Manual correction: {row.get('note') or 'reviewed'}"
            applied += 1
    return applied


# ==========================================================================
# Outputs
# ==========================================================================

def _to_json(records: list[dict]) -> str:
    return json.dumps(records, indent=1, default=lambda o: o.isoformat() if isinstance(o, date) else str(o))


def write_outputs(records: list[dict], extractor: str, activate: bool) -> None:
    """Save per-extractor JSON; if activate, also the files the rest of the pipeline reads."""
    (config.OUTPUT_DIR / f"extracted_contracts_{extractor}.json").write_text(_to_json(records), encoding="utf-8")
    if not activate:
        return
    (config.OUTPUT_DIR / "extracted_contracts.json").write_text(_to_json(records), encoding="utf-8")

    with (config.OUTPUT_DIR / "extracted_contract_lines.csv").open("w", newline="", encoding="utf-8") as f:
        cols = ["contract_id", "customer_name", "country", "start_date", "end_date",
                "escalation_pct", "escalation_date", "product_code", "product_name"] + LINE_FIELDS + ["min_confidence"]
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in records:
            for ln in r["lines"]:
                keys = HEADER_FIELDS + [f"{ln['product_code']}.{x}" for x in LINE_FIELDS]
                w.writerow({**{k: r[k] for k in cols[:7]}, **ln,
                            "min_confidence": min(r["confidence"].get(k, 1.0) for k in keys)})

    with (config.OUTPUT_DIR / "review_queue.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["contract_id", "field", "extracted_value", "confidence", "reason", "evidence", "extractor"])
        for r in records:
            for field, c in r["confidence"].items():
                if c >= config.CONFIDENCE_CUTOFF:
                    continue
                if "." in field:
                    code, sub = field.split(".", 1)
                    ln = next((x for x in r["lines"] if x["product_code"] == code), {})
                    value = ln.get(sub)
                else:
                    value = r.get(field)
                reason = ("missing" if value is None else
                          "not found in contract text" if r["evidence"].get(field) is None else
                          "low model confidence")
                w.writerow([r["contract_id"], field, value, c, reason,
                            r["evidence"].get(field) or "", extractor])


def run(extractor: str = "regex", limit: int | None = None, activate: bool = True) -> list[dict]:
    """Extract every contract in data/contracts with the chosen extractor."""
    ensure_output_dirs()
    ex = EXTRACTORS[extractor]()
    files = sorted(config.CONTRACTS_DIR.glob("*.txt"))[:limit]
    t0 = time.time()

    def one(path):
        text = path.read_text(encoding="utf-8")
        cid = path.stem
        try:
            raw = ex.extract(text) if extractor == "regex" else ex.extract(text, cid)
        except Exception as e:                      # one bad contract must not stop the run
            print(f"  ! {cid}: extraction failed ({e})")
            raw = {"lines": [], "confidence": {}}
        return finalize(cid, raw, text, extractor)

    if extractor == "anthropic":
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(one, files))
    else:
        records = []
        for i, p in enumerate(files, 1):
            records.append(one(p))
            if extractor != "regex" and i % 25 == 0:
                print(f"  {i}/{len(files)} contracts ({time.time() - t0:.0f}s)")

    n_overrides = apply_overrides(records)
    write_outputs(records, extractor, activate)

    n_fields = sum(len(r["confidence"]) for r in records)
    n_low = sum(c < config.CONFIDENCE_CUTOFF for r in records for c in r["confidence"].values())
    n_lines = sum(len(r["lines"]) for r in records)
    print(f"Extractor: {extractor} | contracts: {len(records)} | product lines: {n_lines} | "
          f"fields: {n_fields} | sent to review queue: {n_low} ({n_low / max(n_fields, 1):.1%}) | "
          f"manual corrections applied: {n_overrides} | {time.time() - t0:.1f}s")
    if extractor == "anthropic" and ex.tokens["input"]:
        print(f"  tokens used this run (uncached contracts only): {ex.tokens}")
    return records


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--extractor", choices=list(EXTRACTORS), default="regex")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-activate", action="store_true",
                    help="only write extracted_contracts_<extractor>.json (for comparison)")
    a = ap.parse_args()
    run(a.extractor, a.limit, activate=not a.no_activate)
