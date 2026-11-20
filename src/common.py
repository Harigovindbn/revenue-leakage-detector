"""Small helpers shared by every pipeline step (dates, money, file loading)."""

import json
from datetime import date, datetime

import pandas as pd

from src import config

DATE_TEXT_FORMAT = "%d %B %Y"          # how dates are written in contracts: 01 April 2025


def parse_date(value) -> date | None:
    """Parse an ISO date (2025-04-01) or a contract-style date (01 April 2025)."""
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", DATE_TEXT_FORMAT, "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def add_months(d: date, n: int) -> date:
    """Same calendar day n months later (day clamped to 28 to stay valid)."""
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, min(d.day, 28))


def money(x: float) -> float:
    """Round to cents the same way a billing system would."""
    return round(x + 1e-9, 2)


def escalations_applied(escalation_date: date | None, on: date) -> int:
    """How many annual increases apply on date `on` (first on escalation_date, then yearly)."""
    if escalation_date is None or on < escalation_date:
        return 0
    n, d = 0, escalation_date
    while d <= on:
        n += 1
        d = add_months(d, 12)
    return n


def price_after(base_price: float, escalation_pct: float | None, years: int) -> float:
    """Contract price after `years` compounding increases: base x (1 + pct)^years."""
    return money(base_price * (1 + (escalation_pct or 0) / 100) ** years)


def load_invoices() -> pd.DataFrame:
    df = pd.read_csv(config.INVOICES_CSV, dtype={"invoice_line_id": str, "invoice_id": str})
    df["invoice_date"] = pd.to_datetime(df["invoice_date"]).dt.date
    return df


def load_usage() -> pd.DataFrame:
    return pd.read_csv(config.USAGE_CSV)


def load_customers() -> pd.DataFrame:
    return pd.read_csv(config.CUSTOMERS_CSV)


def load_extracted_contracts(path=None) -> list[dict]:
    path = path or config.OUTPUT_DIR / "extracted_contracts.json"
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_output_dirs() -> None:
    for d in (config.OUTPUT_DIR, config.CACHE_DIR, config.POWERBI_DIR):
        d.mkdir(parents=True, exist_ok=True)
