"""_mask_phone_fields: pure function, no structlog pipeline needed to test it
(CLAUDE.md invariant 8 — never log a raw phone number)."""

from __future__ import annotations

from app.core.logging import _mask_phone_fields


def test_mask_phone_fields_masks_known_fields() -> None:
    event_dict = {
        "event": "opt_out_processed",
        "from_e164": "+48501234567",
        "to_e164": "+48221234567",
        "client_id": "not-a-phone-leave-alone",
    }
    result = _mask_phone_fields(None, "info", event_dict)
    assert result["from_e164"] == "+4850***4567"
    assert result["to_e164"] == "+4822***4567"
    assert result["client_id"] == "not-a-phone-leave-alone"
    assert result["event"] == "opt_out_processed"


def test_mask_phone_fields_ignores_non_string_values() -> None:
    event_dict = {"from_e164": None}
    result = _mask_phone_fields(None, "info", event_dict)
    assert result["from_e164"] is None
