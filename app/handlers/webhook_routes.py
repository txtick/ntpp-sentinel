from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

import datetime as dt
from fastapi import FastAPI, Request

from handlers.sms import (
    extract_contact_id,
    extract_contact_name,
    extract_conversation_id,
    extract_from_phone,
)


@dataclass
class WebhookRouteDeps:
    auth_or_401: Callable[[Request], None]
    parse_request_payload: Callable[[Request], Awaitable[Dict[str, Any]]]
    log_raw_event: Callable[[str, Dict[str, Any]], None]
    flow_who: Callable[[Optional[str], Optional[str], Optional[str]], str]
    has_missed_call_marker: Callable[[Dict[str, Any]], bool]
    ai_inbound_should_suppress: Callable[[Optional[str]], Awaitable[Any]]
    is_spam: Callable[[Optional[str]], bool]

    call_require_missed_marker: bool
    call_missed_marker_keys: List[str]
    call_sla_hours: float
    call_dedupe_window_minutes: int
    now_local: Callable[[], dt.datetime]
    add_business_hours: Callable[[dt.datetime, float], dt.datetime]
    is_recent: Callable[[Optional[str], dt.datetime, int], bool]
    find_recent_call_issue: Callable[[Optional[str], Optional[str], Optional[str]], Optional[Dict[str, Any]]]
    create_call_issue: Callable[[Optional[str], Optional[str], Optional[str], Optional[str], str, str], int]
    ghl_get_contact_name: Callable[[Optional[str]], Awaitable[Optional[str]]]
    ghl_find_conversation_id_for_contact: Callable[[Optional[str], Optional[str]], Awaitable[Optional[str]]]
    flow_log: Callable[..., None]


def register_webhook_routes(app: FastAPI, deps: WebhookRouteDeps) -> None:
    @app.post("/webhook/ghl")
    async def ghl_webhook_raw(request: Request):
        deps.auth_or_401(request)
        payload = await deps.parse_request_payload(request)
        deps.log_raw_event("ghl_raw", payload)
        return {"received": True}

    @app.post("/webhook/ghl/unanswered_call")
    async def unanswered_call(request: Request):
        """
        Deterministic CALL issues only from voicemail_route=tech_sentinel controlled signal.
        """
        deps.auth_or_401(request)
        payload = await deps.parse_request_payload(request)
        deps.log_raw_event("unanswered_call", payload)

        if deps.call_require_missed_marker and not deps.has_missed_call_marker(payload):
            deps.flow_log(
                "call.ignored_missing_missed_marker",
                required_keys=",".join(deps.call_missed_marker_keys),
            )
            return {"received": True, "ignored": "missing_missed_call_marker"}

        vr = payload.get("voicemail_route")
        if isinstance(vr, list):
            routes = [str(x) for x in vr]
        else:
            routes = [str(vr)] if vr is not None else []

        if "tech_sentinel" not in routes:
            return {"received": True, "ignored": "voicemail_route_not_tech_sentinel"}

        contact_id = extract_contact_id(payload)
        from_phone = extract_from_phone(payload)
        conversation_id = extract_conversation_id(payload)

        contact_name = extract_contact_name(payload)
        if not contact_name:
            try:
                contact_name = await deps.ghl_get_contact_name(contact_id)
            except Exception:
                contact_name = None

        if not conversation_id:
            try:
                conversation_id = await deps.ghl_find_conversation_id_for_contact(contact_id, from_phone)
            except Exception:
                conversation_id = None

        who = deps.flow_who(contact_name, from_phone, contact_id)

        if deps.is_spam(from_phone):
            deps.flow_log(
                "call.ignored_spam",
                who=who,
                contact_id=contact_id,
                conversation_id=conversation_id,
            )
            return {"received": True, "ignored": "spam_phone"}

        if conversation_id:
            ai_suppress, ai_gate = await deps.ai_inbound_should_suppress(conversation_id)
            if ai_gate is not None:
                deps.flow_log(
                    "ai_gate.inbound_call",
                    who=who,
                    contact_id=contact_id,
                    conversation_id=conversation_id,
                    needs_follow_up=str(ai_gate.get("needs_follow_up")),
                    confidence=float(ai_gate.get("confidence") or 0.0),
                    decision_mode=str(ai_gate.get("decision_mode") or ""),
                    suppress_threshold=float(ai_gate.get("suppress_threshold") or 0.0),
                    suppressed=bool(ai_suppress),
                )
            if ai_suppress:
                return {"received": True, "ignored": "ai_inbound_suppress"}

        now_local = deps.now_local()
        created_ts = now_local.isoformat()
        due_ts = deps.add_business_hours(now_local, deps.call_sla_hours).isoformat()

        latest_call = None
        if conversation_id:
            latest_call = deps.find_recent_call_issue(conversation_id, None, None)
        if latest_call is None and contact_id:
            latest_call = deps.find_recent_call_issue(None, contact_id, None)
        if latest_call is None and from_phone:
            latest_call = deps.find_recent_call_issue(None, None, from_phone)

        if latest_call is not None and deps.is_recent(latest_call["created_ts"], now_local, deps.call_dedupe_window_minutes):
            deps.flow_log(
                "call.ignored_recent_duplicate",
                who=who,
                contact_id=contact_id,
                conversation_id=conversation_id,
                duplicate_of_issue_id=latest_call["id"],
                dedupe_window_minutes=deps.call_dedupe_window_minutes,
            )
            return {"received": True, "ignored": "recent_duplicate_call_webhook"}

        issue_id = deps.create_call_issue(
            contact_id,
            from_phone,
            contact_name,
            conversation_id,
            created_ts,
            due_ts,
        )

        deps.flow_log(
            "call.issue_created",
            issue_id=issue_id,
            who=who,
            contact_id=contact_id,
            conversation_id=conversation_id,
            status="PENDING",
            due_ts=due_ts,
        )
        return {"received": True, "issue_created": True}
