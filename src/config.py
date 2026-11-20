"""
Central configuration for the Revenue Leakage Detector.

Every business threshold and reference table lives here, so a Finance or Tax
user can change a rate without touching the detection logic (BRD risk register:
"Rates held in one config table").
"""

from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONTRACTS_DIR = DATA_DIR / "contracts"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = OUTPUT_DIR / "cache"
POWERBI_DIR = OUTPUT_DIR / "powerbi"

# Inputs the detector is ALLOWED to read (what a real company would have)
INVOICES_CSV = DATA_DIR / "invoices.csv"
USAGE_CSV = DATA_DIR / "usage.csv"
CUSTOMERS_CSV = DATA_DIR / "customers.csv"
MANUAL_MAPPINGS_CSV = DATA_DIR / "manual_mappings.csv"

# Ground truth: used ONLY by evaluate.py, never by detection
TRUTH_CONTRACTS_CSV = DATA_DIR / "contracts.csv"
TRUTH_CONTRACT_LINES_CSV = DATA_DIR / "contract_lines.csv"
TRUTH_LEAKS_CSV = DATA_DIR / "planted_leaks.csv"

# --------------------------------------------------------------------------
# Tax rates (BRD "Tax rates used"; re-check with each tax authority)
# --------------------------------------------------------------------------
TAX_RATES = {
    # ISO code: (country name, tax label, standard rate)
    "AE": ("United Arab Emirates", "VAT", 0.05),
    "AU": ("Australia", "GST", 0.10),
    "NZ": ("New Zealand", "GST", 0.15),
    "DE": ("Germany", "VAT", 0.19),
    "NL": ("Netherlands", "VAT", 0.21),
    "IE": ("Ireland", "VAT", 0.23),
}
COUNTRY_NAME_TO_CODE = {name.lower(): code for code, (name, _, _) in TAX_RATES.items()}

# --------------------------------------------------------------------------
# Business thresholds (BRD "General rules", FR-03, L4)
# --------------------------------------------------------------------------
MIN_FLAG_USD = 10.0            # flags below this are logged but hidden
CONFIDENCE_CUTOFF = 0.8        # extracted fields below this go to the review queue
PRICE_TOLERANCE = 0.005        # L4: price differs from contract by more than 0.5%
TAX_RATE_TOLERANCE = 0.0001    # L5: rates are compared as decimals

# --------------------------------------------------------------------------
# Customer matching (FR-05)
# --------------------------------------------------------------------------
MATCH_AUTO_ACCEPT = 90         # fuzzy score (0-100) to accept a match automatically
MATCH_REVIEW_FLOOR = 75        # between floor and auto-accept: matched, but flagged for review
LEGAL_SUFFIXES = [
    # longest first so "pty ltd" is removed before "ltd"
    "fz llc", "pty ltd", "pty limited", "limited", "corporation", "company",
    "gmbh", "llc", "fze", "ltd", "inc", "corp", "bv", "dac", "ag", "co", "plc", "sdn bhd",
]

# --------------------------------------------------------------------------
# Leak types (BRD "Leak types and business rules")
# --------------------------------------------------------------------------
LEAK_TYPES = {
    "L1": ("Missed price escalation", "High"),
    "L2": ("Expired discount still applied", "High"),
    "L3": ("Unbilled overage", "Medium"),
    "L4": ("Wrong unit price", "Medium"),
    "L5": ("Wrong tax rate", "Medium (High if under-charged)"),
}

# --------------------------------------------------------------------------
# LLM settings (secrets come from .env, never from code - NFR-04)
# --------------------------------------------------------------------------
ANTHROPIC_MODEL = "claude-opus-5-5"
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "llama3.2:3b"
