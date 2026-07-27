"""Twilio webhook signature validation (spec.md section 7.1)."""

from __future__ import annotations

from fastapi import HTTPException, Request
from twilio.request_validator import RequestValidator

from app.config import settings

_validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)


async def verify_twilio_signature(request: Request) -> dict[str, str]:
    """FastAPI dependency: validates X-Twilio-Signature, returns the parsed form.

    The signature is computed over the PUBLIC url (behind ngrok/a reverse
    proxy, request.url is the internal one and will never match).
    """
    raw_form = await request.form()
    form = {key: str(value) for key, value in raw_form.items()}
    url = f"{settings.PUBLIC_BASE_URL}{request.url.path}"
    signature = request.headers.get("X-Twilio-Signature", "")
    if not _validator.validate(url, form, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return form
