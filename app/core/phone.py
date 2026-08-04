"""Phone number normalization and classification (spec.md section 4.1, 7.4).

Every phone number in the system passes through `normalize_e164` before it
reaches the database or a Twilio call — no raw webhook strings go further.
"""

from __future__ import annotations

import phonenumbers

from app.core.countries import DEFAULT_COUNTRY, get_country_rules

_ANONYMOUS_VALUES = {"anonymous", "unknown", "restricted", "+266696687"}


def is_anonymous(raw: str | None) -> bool:
    """True for Twilio's withheld-caller markers (spec.md 7.4)."""
    if raw is None:
        return True
    return raw.strip().lower() in _ANONYMOUS_VALUES


def normalize_e164(raw: str | None, default_region: str = DEFAULT_COUNTRY) -> str | None:
    """Returns `raw` in E.164 form, or None if it's missing/anonymous/invalid.

    Accepts local and international variants (e.g. '501234567',
    '48501234567', '+48 501 234 567', '501-234-567').
    """
    if not raw or is_anonymous(raw):
        return None
    try:
        parsed = phonenumbers.parse(raw.strip(), default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def mask_e164(phone: str | None) -> str:
    """Masks an E.164 number for logging (CLAUDE.md invariant 8): keeps the
    country code + first digit, hides the middle, keeps the last 4 digits —
    e.g. '+48501234567' -> '+4850***4567'. Never log a raw phone number.
    """
    if phone is None:
        return "<none>"
    if len(phone) <= 8:
        return "***"
    return f"{phone[:5]}***{phone[-4:]}"


def is_mobile_number(phone_e164: str, country_code: str = DEFAULT_COUNTRY) -> bool:
    """Cheap prefix-based mobile heuristic (spec.md 7.4) — not a substitute
    for Twilio Lookup, just enough to reject obvious landlines before
    spending an SMS on them.
    """
    rules = get_country_rules(country_code)
    if not phone_e164.startswith(rules.calling_code):
        return False
    national_number = phone_e164[len(rules.calling_code) :]
    return any(national_number.startswith(prefix) for prefix in rules.mobile_prefixes)
