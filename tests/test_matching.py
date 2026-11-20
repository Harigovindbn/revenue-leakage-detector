"""Unit tests for customer name normalisation and matching (FR-05)."""

import pytest

from src.match_customers import match_name, name_key, normalize_name, suffixes_conflict


@pytest.mark.parametrize("raw, expected", [
    ("Blue Construction GmbH", "blue construction"),
    ("BLUE CONSTRUCTION GMBH", "blue construction"),
    ("Blue Construction GmbH.", "blue construction"),
    ("Blue  Construction GmbH", "blue construction"),
    ("Blue Construction Corp.", "blue construction"),
    ("Granite Education Pty Ltd", "granite education"),
    ("Summit Retail FZ-LLC", "summit retail"),
    ("Titan Retail B.V.", "titan retail"),
    ("Coral Properties Limited", "coral properties"),
    ("Quantum Energy Pty", "quantum energy"),
    ("Atlas & Co", "atlas and"),
])
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_name_key_ignores_spaces_and_glued_suffix():
    assert name_key("BlueConstruction GmbH") == name_key("Blue Construction GmbH")
    assert name_key("HarbourAnalyticsPty Ltd") == name_key("Harbour Analytics Pty Ltd")


def test_suffix_conflict_between_countries():
    assert suffixes_conflict("Union Foods DAC", "Union Foods LLC")        # Ireland vs UAE
    assert not suffixes_conflict("Union Foods Ltd", "Union Foods Pty Ltd")  # both can be Ltd
    assert not suffixes_conflict("Union Foods", "Union Foods LLC")          # suffix dropped


CONTRACT_NAMES = ["Blue Construction GmbH", "Delta Travel Limited", "Union Foods LLC"]
KEYS = {name_key(n): n for n in CONTRACT_NAMES}


def test_exact_match_after_normalisation():
    m = match_name("BLUE CONSTRUCTION GMBH", CONTRACT_NAMES, KEYS, {})
    assert m["matched_customer"] == "Blue Construction GmbH" and m["score"] == 100


def test_typo_is_matched_by_fuzzy_score():
    m = match_name("Blue Constructon GmbH", CONTRACT_NAMES, KEYS, {})
    assert m["matched_customer"] == "Blue Construction GmbH" and m["method"] == "fuzzy"


def test_different_company_is_not_matched():
    m = match_name("Atlas Travel Limited", CONTRACT_NAMES, KEYS, {})
    assert m["matched_customer"] is None


def test_conflicting_suffix_goes_to_review():
    m = match_name("Union Foods DAC", CONTRACT_NAMES, KEYS, {})
    assert m["matched_customer"] is None and m["status"] == "needs_review"


def test_manual_mapping_wins():
    m = match_name("UF Group", CONTRACT_NAMES, KEYS, {"UF Group": "Union Foods LLC"})
    assert m["matched_customer"] == "Union Foods LLC" and m["method"] == "manual"
