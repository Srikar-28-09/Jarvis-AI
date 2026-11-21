"""Provider-neutral outbound telephony for remote attention escalation.

The real provider adapter uses Twilio Programmable Voice:
- https://www.twilio.com/docs/voice/api/call-resource
- https://www.twilio.com/docs/usage/webhooks/webhooks-security
- https://www.twilio.com/docs/phone-numbers/regulatory/getting-started

Calling is never enabled by the adapter alone. Provider configuration,
recipient verification, callback URLs, and the Jarvis policy gate must all pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallInitiation:
    provider: str
    call_id: str
    status: str = "initiated"


class TelephonyError(RuntimeError):
    pass


class TelephonyProvider:
    def configured(self) -> bool:
        return False

    async def initiate_call(
        self,
        *,
        event_id: str,
        to_number: str,
        from_number: str,
        voice_url: str,
        status_callback_url: str,
    ) -> CallInitiation:
        raise TelephonyError("Telephony provider is not configured")

    def validate_status_callback(
        self,
        *,
        callback_url: str,
        params: Mapping[str, str],
        signature: str,
    ) -> bool:
        return False


class DisabledTelephonyProvider(TelephonyProvider):
    pass


class MockTelephonyProvider(TelephonyProvider):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def configured(self) -> bool:
        return True

    async def initiate_call(
        self,
        *,
        event_id: str,
        to_number: str,
        from_number: str,
        voice_url: str,
        status_callback_url: str,
    ) -> CallInitiation:
        call_id = f"mock-{event_id}"
        self.calls.append({
            "event_id": event_id,
            "to_number": to_number,
            "from_number": from_number,
            "voice_url": voice_url,
            "status_callback_url": status_callback_url,
        })
        return CallInitiation("mock", call_id)

    def validate_status_callback(
        self,
        *,
        callback_url: str,
        params: Mapping[str, str],
        signature: str,
    ) -> bool:
        return bool(signature == "mock-valid")


class TwilioTelephonyProvider(TelephonyProvider):
    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        webhook_secret: Optional[str] = None,
    ) -> None:
        self.account_sid = (account_sid or "").strip()
        self.auth_token = (auth_token or "").strip()
        self.webhook_secret = (webhook_secret or "").strip() or self.auth_token

    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token)

    async def initiate_call(
        self,
        *,
        event_id: str,
        to_number: str,
        from_number: str,
        voice_url: str,
        status_callback_url: str,
    ) -> CallInitiation:
        if not self.configured():
            raise TelephonyError("Twilio account credentials are not configured")
        if not to_number or not from_number:
            raise TelephonyError("Twilio recipient and caller numbers are required")
        if not voice_url or not voice_url.lower().startswith("https://"):
            raise TelephonyError("Twilio voice URL must be HTTPS")
        if not status_callback_url or not status_callback_url.lower().startswith("https://"):
            raise TelephonyError("Twilio status callback URL must be HTTPS")
        try:
            from twilio.rest import Client
        except ImportError as exc:
            raise TelephonyError("Twilio SDK is not installed") from exc

        import asyncio

        def _create() -> object:
            client = Client(self.account_sid, self.auth_token)
            return client.calls.create(
                to=to_number,
                from_=from_number,
                url=voice_url,
                status_callback=status_callback_url,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                status_callback_method="POST",
            )

        try:
            call = await asyncio.to_thread(_create)
        except Exception as exc:
            logger.warning("Twilio call initiation failed: %s", type(exc).__name__)
            raise TelephonyError("Twilio call initiation failed") from exc
        return CallInitiation("twilio", str(getattr(call, "sid", "")))

    def validate_status_callback(
        self,
        *,
        callback_url: str,
        params: Mapping[str, str],
        signature: str,
    ) -> bool:
        if not callback_url or not signature or not self.webhook_secret:
            return False
        try:
            from twilio.request_validator import RequestValidator
        except ImportError:
            return False
        validator = RequestValidator(self.webhook_secret)
        try:
            return bool(validator.validate(callback_url, dict(params), signature))
        except Exception:
            return False

    def validate_callback(
        self,
        *,
        callback_url: str,
        params: Mapping[str, str],
        signature: str,
    ) -> bool:
        """Validate a Twilio-signed callback with the SDK RequestValidator."""
        return self.validate_status_callback(
            callback_url=callback_url,
            params=params,
            signature=signature,
        )


def resolve_voice_callback_url(template: str, event_id: str) -> str:
    """Resolve a configured voice URL, optionally substituting {event_id}."""
    resolved = str(template or "").strip()
    if not resolved:
        return ""
    return resolved.replace("{event_id}", str(event_id or "").strip()).replace(
        "{{event_id}}",
        str(event_id or "").strip(),
    )


def validate_https_callback_url(callback_url: str) -> bool:
    """Fail closed on callback URLs that cannot be a public HTTPS Twilio target."""
    try:
        parsed = urlsplit(callback_url)
    except Exception:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.netloc
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def callback_url_with_query(callback_url: str, query_params: Mapping[str, str]) -> str:
    """Add/replace actual query parameters while preserving exact configured URL."""
    try:
        parsed = urlsplit(callback_url)
    except Exception:
        return callback_url
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in query_params.items():
        existing[str(key)] = str(value)
    query = urlencode(existing, doseq=True)
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        query,
        parsed.fragment,
    ))
