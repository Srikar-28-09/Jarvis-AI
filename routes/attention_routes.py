"""Authenticated attention event and safe phone-escalation routes."""

from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from core.database import get_db_session
from src.auth_helpers import (
    effective_user,
    is_delegated_credential,
    require_user,
)
from src.settings import get_setting, save_settings, load_settings


class AttentionConfig(BaseModel):
    enabled: bool = False
    provider: str = "disabled"
    decision_required_call: bool = True
    critical_call: bool = True
    critical_quiet_hours_override: bool = False
    quiet_hours_start: str = ""
    quiet_hours_end: str = ""
    event_ttl_minutes: int = 60
    challenge_ttl_minutes: int = 10
    max_calls_per_hour: int = 2
    call_cooldown_minutes: int = 30
    max_call_attempts: int = 1
    account_sid: str = ""
    auth_token: str = ""
    webhook_secret: str = ""
    from_number: str = ""
    to_number: str = ""
    voice_url: str = ""
    status_callback_url: str = ""


def setup_attention_routes(api_key_manager=None) -> APIRouter:
    router = APIRouter(prefix="/api/attention", tags=["attention"])

    def _owner(request: Request) -> str:
        if is_delegated_credential(request):
            raise HTTPException(
                403,
                "Attention and approval responses require an interactive session.",
            )
        owner = require_user(request)
        return str(owner or "").strip().casefold()

    @router.get("/pending")
    async def pending(request: Request, include_resolved: bool = False):
        from src.attention import list_attention_events
        owner = _owner(request)
        return {
            "events": list_attention_events(owner, include_resolved=include_resolved)
        }

    @router.get("/next-steps")
    async def next_steps(request: Request):
        from src.proactive import list_next_steps
        owner = _owner(request)
        return {"next_steps": list_next_steps(owner)}

    @router.get("/{event_id}")
    async def get_event(event_id: str, request: Request):
        from src.attention import get_attention_event
        owner = _owner(request)
        event = get_attention_event(event_id)
        if not event or str(event.get("owner") or "").strip().casefold() != owner:
            raise HTTPException(404, "Attention event not found")
        return event

    @router.post("/{event_id}/acknowledge")
    async def acknowledge(event_id: str, request: Request):
        from src.attention import acknowledge_attention_event
        owner = _owner(request)
        if not acknowledge_attention_event(event_id, owner):
            raise HTTPException(404, "Attention event not found or already resolved")
        return {"ok": True}

    @router.post("/{event_id}/resolve")
    async def resolve(event_id: str, request: Request):
        from src.attention import resolve_attention_event
        owner = _owner(request)
        if not resolve_attention_event(event_id, owner, action="resolved_by_owner"):
            raise HTTPException(404, "Attention event not found")
        return {"ok": True}

    @router.post("/{event_id}/challenge/start")
    async def challenge_start(event_id: str, request: Request):
        from src.attention import start_attention_challenge
        owner = _owner(request)
        event, code = start_attention_challenge(event_id, owner)
        if not event:
            raise HTTPException(409, "No pending owner-bound approval for this event")
        # Return the code only to the authenticated owner. It is never placed in
        # call audio, ntfy payload, logs, or challenge audit history.
        return {"event": event, "challenge_code": code}

    @router.post("/{event_id}/challenge/verify")
    async def challenge_verify(event_id: str, request: Request, body: dict):
        from src.attention import verify_attention_challenge
        owner = _owner(request)
        code = str(body.get("code") or "").strip()
        if not code or len(code) > 16:
            raise HTTPException(400, "Invalid challenge code")
        ok, reason = verify_attention_challenge(event_id, owner, code)
        if not ok:
            raise HTTPException(409, reason)
        return {"ok": True, "status": "approved"}

    @router.get("/config/status")
    async def config_status(request: Request):
        require_user(request)
        require_admin = __import__("core.middleware", fromlist=["require_admin"]).require_admin
        require_admin(request)
        secrets_map = {}
        if api_key_manager is not None:
            try:
                secrets_map = api_key_manager.load()
            except Exception:
                secrets_map = {}
        provider = str(get_setting("attention_telephony_provider", "disabled") or "disabled")
        configured = bool(
            provider == "mock"
            or (
                provider == "twilio"
                and secrets_map.get("twilio_account_sid")
                and secrets_map.get("twilio_auth_token")
                and secrets_map.get("twilio_from_number")
                and secrets_map.get("twilio_to_number")
                and secrets_map.get("twilio_voice_url")
                and secrets_map.get("twilio_status_callback_url")
            )
        )
        return {
            "enabled": bool(get_setting("attention_phone_escalation_enabled", False)),
            "provider": provider,
            "configured": configured,
            "decision_required_call": bool(get_setting("attention_decision_required_call", True)),
            "critical_call": bool(get_setting("attention_critical_call", True)),
            "critical_quiet_hours_override": bool(get_setting("attention_critical_quiet_hours_override", False)),
            "quiet_hours_start": str(get_setting("attention_quiet_hours_start", "") or ""),
            "quiet_hours_end": str(get_setting("attention_quiet_hours_end", "") or ""),
            "event_ttl_minutes": int(get_setting("attention_event_ttl_minutes", 60) or 60),
            "challenge_ttl_minutes": int(get_setting("attention_challenge_ttl_minutes", 10) or 10),
            "max_calls_per_hour": int(get_setting("attention_max_calls_per_hour", 2) or 2),
            "call_cooldown_minutes": int(get_setting("attention_call_cooldown_minutes", 30) or 30),
            "max_call_attempts": int(get_setting("attention_max_call_attempts", 1) or 1),
            "secrets_configured": {
                key: bool(secrets_map.get(key))
                for key in (
                    "twilio_account_sid",
                    "twilio_auth_token",
                    "twilio_webhook_secret",
                    "twilio_from_number",
                    "twilio_to_number",
                    "twilio_voice_url",
                    "twilio_status_callback_url",
                )
            },
        }

    @router.put("/config")
    async def update_config(config: AttentionConfig, request: Request):
        require_user(request)
        require_admin = __import__("core.middleware", fromlist=["require_admin"]).require_admin
        require_admin(request)
        provider = str(config.provider or "disabled").strip().lower()
        if provider not in {"disabled", "mock", "twilio"}:
            raise HTTPException(400, "Unsupported telephony provider")
        if provider != "mock" and any(
            ord(char) < 32 for value in (
                config.quiet_hours_start,
                config.quiet_hours_end,
            ) for char in value
        ):
            raise HTTPException(400, "Invalid quiet-hours configuration")
        if api_key_manager is not None:
            secret_values = {
                "twilio_account_sid": config.account_sid,
                "twilio_auth_token": config.auth_token,
                "twilio_webhook_secret": config.webhook_secret,
                "twilio_from_number": config.from_number,
                "twilio_to_number": config.to_number,
                "twilio_voice_url": config.voice_url,
                "twilio_status_callback_url": config.status_callback_url,
            }
            for key, value in secret_values.items():
                if str(value).strip():
                    api_key_manager.save(key, str(value).strip())
        settings = dict(load_settings())
        settings.update({
            "attention_phone_escalation_enabled": bool(config.enabled),
            "attention_telephony_provider": provider,
            "attention_decision_required_call": bool(config.decision_required_call),
            "attention_critical_call": bool(config.critical_call),
            "attention_critical_quiet_hours_override": bool(config.critical_quiet_hours_override),
            "attention_quiet_hours_start": config.quiet_hours_start.strip(),
            "attention_quiet_hours_end": config.quiet_hours_end.strip(),
            "attention_event_ttl_minutes": max(1, min(config.event_ttl_minutes, 1440)),
            "attention_challenge_ttl_minutes": max(1, min(config.challenge_ttl_minutes, 60)),
            "attention_max_calls_per_hour": max(1, min(config.max_calls_per_hour, 20)),
            "attention_call_cooldown_minutes": max(1, min(config.call_cooldown_minutes, 1440)),
            "attention_max_call_attempts": max(1, min(config.max_call_attempts, 3)),
        })
        save_settings(settings)
        return {"ok": True}

    @router.post("/twilio/status")
    async def twilio_status(request: Request):
        if api_key_manager is None:
            raise HTTPException(503, "Telephony provider is not configured")
        try:
            secrets_map = api_key_manager.load()
        except Exception:
            raise HTTPException(503, "Telephony credentials unavailable")
        from src.telephony import TwilioTelephonyProvider
        provider = TwilioTelephonyProvider(
            account_sid=str(secrets_map.get("twilio_account_sid") or ""),
            auth_token=str(secrets_map.get("twilio_auth_token") or ""),
            webhook_secret=str(secrets_map.get("twilio_webhook_secret") or ""),
        )
        callback_url = str(secrets_map.get("twilio_status_callback_url") or "")
        signature = request.headers.get("x-twilio-signature", "")
        form = await request.form()
        params = {str(key): str(value) for key, value in form.multi_items()}
        if not provider.validate_status_callback(
            callback_url=callback_url,
            params=params,
            signature=signature,
        ):
            raise HTTPException(403, "Invalid Twilio callback signature")
        call_sid = str(params.get("CallSid") or "")
        status = str(params.get("CallStatus") or "")
        if not call_sid or not status:
            raise HTTPException(400, "Invalid Twilio callback")
        from src.attention import get_attention_manager
        manager = get_attention_manager(api_key_manager)
        if not await manager.process_twilio_callback(call_sid, status):
            raise HTTPException(404, "Unknown call")
        return PlainTextResponse("ok")

    @router.api_route("/twilio/voice/{event_id}", methods=["GET", "POST"])
    async def twilio_voice(event_id: str, request: Request):
        if api_key_manager is None:
            raise HTTPException(503, "Telephony provider is not configured")
        event_id = str(event_id or "").strip()[:128]
        if not event_id:
            raise HTTPException(400, "Invalid voice callback event")
        try:
            secrets_map = api_key_manager.load()
        except Exception:
            raise HTTPException(503, "Telephony credentials unavailable")

        account_sid = str(secrets_map.get("twilio_account_sid") or "").strip()
        auth_token = str(secrets_map.get("twilio_auth_token") or "").strip()
        webhook_secret = str(secrets_map.get("twilio_webhook_secret") or "").strip()
        voice_template = str(secrets_map.get("twilio_voice_url") or "").strip()
        if not account_sid or not auth_token or not webhook_secret or not voice_template:
            raise HTTPException(503, "Telephony voice callback is not configured")

        from src.telephony import (
            TwilioTelephonyProvider,
            callback_url_with_query,
            resolve_voice_callback_url,
            validate_https_callback_url,
        )
        callback_url = resolve_voice_callback_url(voice_template, event_id)
        if (
            not validate_https_callback_url(callback_url)
            or not callback_url.lower().startswith("https://")
        ):
            raise HTTPException(503, "Telephony voice callback is not configured safely")

        signature = str(request.headers.get("x-twilio-signature") or "").strip()
        if not signature:
            raise HTTPException(403, "Missing Twilio callback signature")

        query_params = {
            str(key): str(value)
            for key, value in request.query_params.multi_items()
        }
        callback_url = callback_url_with_query(callback_url, query_params)

        if request.method == "GET":
            params = {}
        elif request.method == "POST":
            form = await request.form()
            params = {
                str(key): str(value)
                for key, value in form.multi_items()
            }
        else:
            raise HTTPException(405, "Unsupported voice callback method")

        provider = TwilioTelephonyProvider(
            account_sid=account_sid,
            auth_token=auth_token,
            webhook_secret=webhook_secret,
        )
        if not provider.validate_callback(
            callback_url=callback_url,
            params=params,
            signature=signature,
        ):
            raise HTTPException(403, "Invalid Twilio callback signature")

        # Deliberately no private event summary or approval details in call audio.
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response>"
            "<Say>Jarvis needs your attention for a decision. "
            "Open the authenticated Jarvis companion or dashboard to review it. "
            "This call does not approve or reject anything.</Say>"
            "<Hangup/>"
            "</Response>"
        )
        return PlainTextResponse(xml, media_type="application/xml")

    return router
