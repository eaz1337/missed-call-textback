from __future__ import annotations

import pytest

from app.core.phone import is_anonymous, is_mobile_number, normalize_e164


@pytest.mark.parametrize("raw", ["anonymous", "unknown", "restricted", "+266696687", None])
def test_is_anonymous_true_for_withheld_markers(raw: str | None) -> None:
    assert is_anonymous(raw) is True


@pytest.mark.parametrize("raw", ["+48501234567", "501234567"])
def test_is_anonymous_false_for_real_numbers(raw: str) -> None:
    assert is_anonymous(raw) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+48501234567", "+48501234567"),
        ("501234567", "+48501234567"),
        ("48501234567", "+48501234567"),
        ("0048501234567", "+48501234567"),
        ("+48 501 234 567", "+48501234567"),
        ("501-234-567", "+48501234567"),
    ],
)
def test_normalize_e164_accepts_pl_variants(raw: str, expected: str) -> None:
    assert normalize_e164(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "anonymous", "not-a-number", "123"])
def test_normalize_e164_returns_none_for_invalid_input(raw: str | None) -> None:
    assert normalize_e164(raw) is None


@pytest.mark.parametrize(
    "prefix", ["45", "50", "51", "53", "57", "60", "66", "69", "72", "73", "78", "79", "88"]
)
def test_is_mobile_number_true_for_pl_mobile_prefixes(prefix: str) -> None:
    assert is_mobile_number(f"+48{prefix}0000000", "PL") is True


def test_is_mobile_number_false_for_pl_landline() -> None:
    assert is_mobile_number("+48221234567", "PL") is False


def test_is_mobile_number_false_for_other_calling_code() -> None:
    assert is_mobile_number("+491701234567", "PL") is False


def test_is_mobile_number_unknown_country_raises() -> None:
    with pytest.raises(ValueError, match="No CountryRules registered"):
        is_mobile_number("+48501234567", "ZZ")
