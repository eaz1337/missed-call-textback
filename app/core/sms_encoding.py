"""SMS body normalization for GSM-7 safety and segment limits (spec.md 4.2).

Transliteration by default (`allow_diacritics=false`): no Polish diacritic
belongs to the GSM-7 alphabet, and a single one in the body switches the
whole message to UCS-2, cutting the per-segment limit from 160 to 70 chars
(CLAUDE.md invariant 5). The diacritic -> ASCII map comes from
`CountryRules.translit_map` (app/core/countries.py) keyed by `country_code`,
not a hardcoded PL-only constant, so a future country supplies its own map
without touching this function (spec.md 4.2 Week 2 implementation note).
"""

from __future__ import annotations

from app.core.countries import DEFAULT_COUNTRY, get_country_rules

# Subset of GSM-7 sufficient for validation after transliteration (spec.md 4.2).
GSM7_SAFE = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

_GSM7_SINGLE_SEGMENT = 160
_GSM7_CONCAT_SEGMENT = 153
_UCS2_SINGLE_SEGMENT = 70
_UCS2_CONCAT_SEGMENT = 67


def prepare_sms_body(
    text: str,
    *,
    allow_diacritics: bool,
    max_segments: int,
    country_code: str = DEFAULT_COUNTRY,
) -> str:
    """Normalizes whitespace, transliterates (unless allowed), and truncates
    to the segment budget implied by `max_segments` and the resulting
    encoding (spec.md 4.2).
    """
    text = " ".join(text.split())
    if not allow_diacritics:
        rules = get_country_rules(country_code)
        translit_table = {
            ord(diacritic): ascii_ for diacritic, ascii_ in rules.translit_map.items()
        }
        text = text.translate(translit_table)
        text = "".join(ch if ch in GSM7_SAFE else "?" for ch in text)  # hard GSM-7 closure
        limit = _GSM7_SINGLE_SEGMENT if max_segments == 1 else max_segments * _GSM7_CONCAT_SEGMENT
    else:
        limit = _UCS2_SINGLE_SEGMENT if max_segments == 1 else max_segments * _UCS2_CONCAT_SEGMENT
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def compute_encoding_and_segments(text: str) -> tuple[str, int]:
    """Returns ('gsm7'|'ucs2', segment_count) for `sms_messages.encoding` /
    `.segments` (spec.md 4.2 rule 4: logged before sending, to monitor cost
    per client). Matches the CHECK constraint values in
    app.models.sms_message.SMS_ENCODINGS.
    """
    if not text:
        return "gsm7", 1
    if all(ch in GSM7_SAFE for ch in text):
        single, concat = _GSM7_SINGLE_SEGMENT, _GSM7_CONCAT_SEGMENT
        encoding = "gsm7"
    else:
        single, concat = _UCS2_SINGLE_SEGMENT, _UCS2_CONCAT_SEGMENT
        encoding = "ucs2"
    if len(text) <= single:
        return encoding, 1
    segments = -(-len(text) // concat)  # ceil division
    return encoding, segments
