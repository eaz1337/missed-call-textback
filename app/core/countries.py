"""Per-country rules registry.

Every place in the codebase that needs a country-specific fact (mobile
number prefixes, phonenumbers region, SMS opt-out keywords, diacritic
transliteration) looks it up here by ISO 3166-1 alpha-2 code instead of
hardcoding it inline. Adding a new country later is adding one entry to
COUNTRY_REGISTRY, not touching guards/phone/sms_encoding logic.

Only "PL" is populated — per spec.md, Poland is the only supported market
for now.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CountryRules:
    code: str  # ISO 3166-1 alpha-2, e.g. "PL"
    phone_region: str  # region hint for phonenumbers.parse, usually == code
    calling_code: str  # E.164 calling code prefix, e.g. "+48"
    mobile_prefixes: tuple[str, ...]  # national-significant-number prefixes that are mobile
    translit_map: dict[str, str] = field(default_factory=dict)  # diacritic -> ASCII
    opt_out_keywords: tuple[str, ...] = ()  # inbound SMS bodies (upper, trimmed) that mean "stop"


PL = CountryRules(
    code="PL",
    phone_region="PL",
    calling_code="+48",
    # spec.md 7.4: Polish mobile numbers start with one of these prefixes.
    mobile_prefixes=(
        "45",
        "50",
        "51",
        "53",
        "57",
        "60",
        "66",
        "69",
        "72",
        "73",
        "78",
        "79",
        "88",
    ),
    translit_map={
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ź": "z",
        "ż": "z",
        "Ą": "A",
        "Ć": "C",
        "Ę": "E",
        "Ł": "L",
        "Ń": "N",
        "Ó": "O",
        "Ś": "S",
        "Ź": "Z",
        "Ż": "Z",
    },
    opt_out_keywords=("STOP", "KONIEC", "NIE", "REZYGNUJE", "REZYGNUJĘ"),
)

COUNTRY_REGISTRY: dict[str, CountryRules] = {"PL": PL}

DEFAULT_COUNTRY = "PL"


def get_country_rules(code: str) -> CountryRules:
    try:
        return COUNTRY_REGISTRY[code.upper()]
    except KeyError:
        raise ValueError(f"No CountryRules registered for country code {code!r}") from None
