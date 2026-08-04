"""structlog setup shared by the API and Celery workers (CLAUDE.md invariant
8, code conventions "Logs: structlog JSON"): JSON output, and any field that
carries a phone number is masked before it ever reaches a log sink, so a
call site forgetting to mask manually can't leak one.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.phone import mask_e164

_PHONE_FIELDS = {
    "from_raw",
    "to_raw",
    "from_e164",
    "to_e164",
    "caller_e164",
    "owner_phone_e164",
    "phone_e164",
    "forwarded_from",
}


def _mask_phone_fields(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for field in _PHONE_FIELDS:
        value = event_dict.get(field)
        if isinstance(value, str):
            event_dict[field] = mask_e164(value)
    return event_dict


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _mask_phone_fields,
            structlog.processors.JSONRenderer(),
        ],
        cache_logger_on_first_use=True,
    )
