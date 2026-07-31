"""Golden tests for GSM-7 transliteration and segment counting (spec.md 4.2;
CLAUDE.md Testing section). Pure functions — no mocking.
"""

from __future__ import annotations

import pytest

from app.core.sms_encoding import GSM7_SAFE, compute_encoding_and_segments, prepare_sms_body

POLISH_SENTENCES = [
    "Dziękujemy za telefon, oddzwonimy jak najszybciej.",
    "Przepraszamy, nie mogliśmy odebrać połączenia.",
    "Nasza firma jest teraz zamknięta, prosimy o kontakt później.",
    "Zostaw wiadomość, a nasz zespół skontaktuje się z Tobą wkrótce.",
    "Cieszymy się z Twojego telefonu, wkrótce się odezwiemy.",
    "Skontaktujemy się z Państwem najszybciej jak to możliwe.",
    "Życzymy miłego dnia i dziękujemy za cierpliwość.",
]


@pytest.mark.parametrize("sentence", POLISH_SENTENCES)
def test_prepare_sms_body_transliterates_to_gsm7_safe_chars(sentence: str) -> None:
    body = prepare_sms_body(sentence, allow_diacritics=False, max_segments=1)
    assert all(ch in GSM7_SAFE for ch in body)
    # transliteration must not have introduced a lossy "?" for a real Polish sentence
    assert "?" not in body


@pytest.mark.parametrize("sentence", POLISH_SENTENCES)
def test_prepare_sms_body_default_produces_single_gsm7_segment(sentence: str) -> None:
    body = prepare_sms_body(sentence, allow_diacritics=False, max_segments=1)
    encoding, segments = compute_encoding_and_segments(body)
    assert encoding == "gsm7"
    assert segments == 1


def test_prepare_sms_body_translit_map_matches_spec() -> None:
    body = prepare_sms_body("ĄĆĘŁŃÓŚŹŻ ąćęłńóśźż", allow_diacritics=False, max_segments=1)
    assert body == "ACELNOSZZ acelnoszz"


def test_prepare_sms_body_hard_gsm7_closure_replaces_unmappable_chars() -> None:
    body = prepare_sms_body("Cena: 10€ 🎉", allow_diacritics=False, max_segments=1)
    assert all(ch in GSM7_SAFE for ch in body)
    assert "?" in body


def test_prepare_sms_body_normalizes_whitespace() -> None:
    body = prepare_sms_body(
        "  Dzień   dobry  \n\n  Państwu  ", allow_diacritics=False, max_segments=1
    )
    assert body == "Dzien dobry Panstwu"


def test_prepare_sms_body_truncates_gsm7_single_segment_at_160() -> None:
    body = prepare_sms_body("a" * 200, allow_diacritics=False, max_segments=1)
    assert len(body) == 160
    assert body.endswith("...")


def test_prepare_sms_body_truncates_gsm7_concat_segments() -> None:
    body = prepare_sms_body("a" * 500, allow_diacritics=False, max_segments=3)
    assert len(body) == 3 * 153
    assert body.endswith("...")


def test_prepare_sms_body_allow_diacritics_keeps_polish_chars() -> None:
    body = prepare_sms_body("Dziękujemy za telefon", allow_diacritics=True, max_segments=1)
    assert body == "Dziękujemy za telefon"


def test_prepare_sms_body_allow_diacritics_truncates_at_ucs2_limit() -> None:
    body = prepare_sms_body("ą" * 100, allow_diacritics=True, max_segments=1)
    assert len(body) == 70
    assert body.endswith("...")


def test_prepare_sms_body_fits_under_limit_untruncated() -> None:
    short = "Dziękujemy za telefon."
    body = prepare_sms_body(short, allow_diacritics=False, max_segments=1)
    assert not body.endswith("...")


def test_compute_encoding_and_segments_ucs2_for_diacritics() -> None:
    encoding, segments = compute_encoding_and_segments("Dziękujemy")
    assert encoding == "ucs2"
    assert segments == 1


def test_compute_encoding_and_segments_empty_string() -> None:
    assert compute_encoding_and_segments("") == ("gsm7", 1)


@pytest.mark.parametrize(
    ("length", "expected_segments"),
    [(160, 1), (161, 2), (153 * 2, 2), (153 * 2 + 1, 3)],
)
def test_compute_encoding_and_segments_gsm7_segment_boundaries(
    length: int, expected_segments: int
) -> None:
    encoding, segments = compute_encoding_and_segments("a" * length)
    assert encoding == "gsm7"
    assert segments == expected_segments


@pytest.mark.parametrize(
    ("length", "expected_segments"),
    [(70, 1), (71, 2), (67 * 2, 2), (67 * 2 + 1, 3)],
)
def test_compute_encoding_and_segments_ucs2_segment_boundaries(
    length: int, expected_segments: int
) -> None:
    encoding, segments = compute_encoding_and_segments("ą" * length)
    assert encoding == "ucs2"
    assert segments == expected_segments
