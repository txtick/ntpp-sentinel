"""
Route Rollover Handler
======================
Multi-step SMS state machine for the rollover feature.
Imported by sms_routes.py and called as an early-return intercept inside
the `if is_internal:` block before existing command parsing.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from services.rollover import (
    OFFICE_PHONE,
    MAX_STOPS_SHOWN,
    CUSTOMER_SEND_DELAY_SECONDS,
    is_rollover_trigger,
    fetch_tech_route,
    fetch_skimmer_customer,
    extract_customer_phone,
    get_active_session,
    abort_incomplete_sessions,
    create_session,
    update_session_state,
    build_route_list_message,
    build_confirmation_message,
    build_customer_message,
    build_manager_message,
    parse_stop_numbers,
    parse_yes_no,
)

if TYPE_CHECKING:
    from handlers.sms_routes import SMSRouteDeps

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def handle_rollover(
    text: str,
    contact_id: str,
    conversation_id: Optional[str],
    deps: "SMSRouteDeps",
) -> Optional[Dict[str, Any]]:
    """
    Returns a result dict if this message was handled by the rollover system,
    or None if the caller should proceed with normal command parsing.
    """
    if not deps.rollover_enabled:
        return None

    conn = deps.db()
    try:
        active_session = get_active_session(conn, contact_id)
    finally:
        conn.close()

    if active_session:
        state = active_session["state"]
        if state == "AWAITING_SELECTION":
            return await _handle_awaiting_selection(
                active_session, text, contact_id, conversation_id, deps
            )
        if state == "AWAITING_CONFIRMATION":
            return await _handle_awaiting_confirmation(
                active_session, text, contact_id, conversation_id, deps
            )

    if is_rollover_trigger(text):
        return await _handle_trigger(contact_id, conversation_id, deps)

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _reply(text: str, contact_id: str, conversation_id: Optional[str], deps) -> None:
    """Send an SMS reply to the tech who triggered rollover."""
    conv_id = conversation_id
    if not conv_id:
        try:
            conv_id = await deps.manager_conversation_for_contact(contact_id)
        except Exception:
            conv_id = None
    if conv_id:
        try:
            await deps.ghl_send_message(conv_id, contact_id, text)
        except Exception as exc:
            log.error("rollover: failed to reply to tech %s: %s", contact_id, exc)


async def _get_tech_first_name(contact_id: str, deps) -> str:
    """Best-effort: return the tech's first name from GHL, fall back to 'your tech'."""
    try:
        full_name = await deps.ghl_get_contact_name(contact_id)
        if full_name:
            return full_name.strip().split()[0]
    except Exception:
        pass
    return "your tech"


async def _send_customer_apology(
    stop: Dict[str, Any],
    tech_first_name: str,
    deps,
) -> bool:
    """
    Resolve the customer's GHL conversation via their Skimmer phone number
    and send the apology message. Returns True on success.
    """
    skimmer_customer_id = stop.get("customerId")
    customer_first_name = (stop.get("customerFirstName") or "there").strip()

    # Step 1: get customer phone from Skimmer
    phone: Optional[str] = None
    if skimmer_customer_id:
        customer_data = await fetch_skimmer_customer(skimmer_customer_id)
        if customer_data:
            phone = extract_customer_phone(customer_data)

    if not phone:
        log.warning("rollover: no phone for skimmer customer=%s", skimmer_customer_id)
        return False

    # Step 2: find GHL conversation + contact ID by phone
    try:
        conv_id, ghl_contact_id = await deps.ghl_search_conversation_by_phone(phone)
    except Exception as exc:
        log.error("rollover: GHL conversation lookup failed phone=%s: %s", phone, exc)
        return False

    if not conv_id or not ghl_contact_id:
        log.warning("rollover: no GHL conversation for phone=%s", phone)
        return False

    # Step 3: send
    msg = build_customer_message(customer_first_name, tech_first_name)
    try:
        await deps.ghl_send_message(conv_id, ghl_contact_id, msg)
        return True
    except Exception as exc:
        log.error("rollover: failed to send apology phone=%s: %s", phone, exc)
        return False


# ---------------------------------------------------------------------------
# State machine steps
# ---------------------------------------------------------------------------

async def _handle_trigger(
    contact_id: str,
    conversation_id: Optional[str],
    deps,
) -> Dict[str, Any]:
    skimmer_tech_id = deps.skimmer_tech_id_map.get(contact_id)
    if not skimmer_tech_id:
        await _reply(
            f"Your Skimmer tech ID isn't configured yet. Contact the office at {OFFICE_PHONE}.",
            contact_id, conversation_id, deps,
        )
        return {"received": True, "rollover": "aborted", "reason": "no_tech_id_mapping"}

    # Discard any prior incomplete session so today's trigger starts fresh
    conn = deps.db()
    try:
        abort_incomplete_sessions(conn, contact_id)
    finally:
        conn.close()

    today = deps.now_local().date().isoformat()
    try:
        route_data = await fetch_tech_route(skimmer_tech_id, today)
    except Exception as exc:
        log.error("rollover: skimmer route fetch failed tech=%s: %s", skimmer_tech_id, exc)
        await _reply(
            f"Sorry, I wasn't able to pull your route right now. Please try again in a few "
            f"minutes or contact the office at {OFFICE_PHONE}.",
            contact_id, conversation_id, deps,
        )
        return {"received": True, "rollover": "aborted", "reason": "skimmer_api_error"}

    stops = route_data.get("stops") or []
    if not stops:
        await _reply(
            f"I don't see any route stops for you today. If this seems wrong, "
            f"contact the office at {OFFICE_PHONE}.",
            contact_id, conversation_id, deps,
        )
        return {"received": True, "rollover": "aborted", "reason": "no_stops"}

    conn = deps.db()
    try:
        session_id = create_session(conn, contact_id, conversation_id, stops)
    finally:
        conn.close()

    await _reply(build_route_list_message(stops), contact_id, conversation_id, deps)
    log.info("rollover.trigger contact=%s session=%d stops=%d", contact_id, session_id, len(stops))
    return {"received": True, "rollover": "started", "session_id": session_id}


async def _handle_awaiting_selection(
    session,
    text: str,
    contact_id: str,
    conversation_id: Optional[str],
    deps,
) -> Dict[str, Any]:
    stops = json.loads(session["route_stops_json"])
    display_count = min(len(stops), MAX_STOPS_SHOWN)

    numbers, err = parse_stop_numbers(text, display_count)
    if err:
        await _reply(err, contact_id, conversation_id, deps)
        return {"received": True, "rollover": "awaiting_selection", "parse_error": err}

    selected_stops = [stops[n - 1] for n in numbers]
    tech_first_name = await _get_tech_first_name(contact_id, deps)

    conn = deps.db()
    try:
        update_session_state(
            conn,
            session["id"],
            "AWAITING_CONFIRMATION",
            selected_indices=",".join(str(n) for n in numbers),
        )
    finally:
        conn.close()

    await _reply(
        build_confirmation_message(selected_stops, tech_first_name),
        contact_id, conversation_id, deps,
    )
    return {"received": True, "rollover": "awaiting_confirmation"}


async def _handle_awaiting_confirmation(
    session,
    text: str,
    contact_id: str,
    conversation_id: Optional[str],
    deps,
) -> Dict[str, Any]:
    answer = parse_yes_no(text)

    if answer is None:
        await _reply(
            "Please reply YES to send the messages or NO to cancel.",
            contact_id, conversation_id, deps,
        )
        return {"received": True, "rollover": "awaiting_confirmation", "unrecognized": True}

    if answer == "NO":
        conn = deps.db()
        try:
            update_session_state(conn, session["id"], "ABORTED")
        finally:
            conn.close()
        await _reply("Rollover cancelled. No messages were sent.", contact_id, conversation_id, deps)
        return {"received": True, "rollover": "cancelled"}

    # YES — reconstruct selected stops from session
    stops = json.loads(session["route_stops_json"])
    raw_indices = (session["selected_indices"] or "").split(",")
    selected_stops: List[Dict[str, Any]] = []
    for idx_str in raw_indices:
        idx_str = idx_str.strip()
        if idx_str.isdigit():
            idx = int(idx_str) - 1
            if 0 <= idx < len(stops):
                selected_stops.append(stops[idx])

    tech_first_name = await _get_tech_first_name(contact_id, deps)
    sent_time = deps.now_local().strftime("%-I:%M %p")

    notified: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for stop in selected_stops:
        success = await _send_customer_apology(stop, tech_first_name, deps)
        (notified if success else failed).append(stop)
        await asyncio.sleep(CUSTOMER_SEND_DELAY_SECONDS)

    conn = deps.db()
    try:
        update_session_state(conn, session["id"], "COMPLETE", mark_completed=True)
    finally:
        conn.close()

    # Notify managers
    manager_msg = build_manager_message(tech_first_name, contact_id, notified, failed, sent_time)
    for mgr_id in deps.manager_contact_ids:
        try:
            mgr_conv = await deps.manager_conversation_for_contact(mgr_id)
            if mgr_conv:
                await deps.ghl_send_message(mgr_conv, mgr_id, manager_msg)
        except Exception as exc:
            log.error("rollover: failed to notify manager %s: %s", mgr_id, exc)

    await _reply(
        f"Done. {len(notified)} customer(s) have been notified. Have a good rest of your day.",
        contact_id, conversation_id, deps,
    )
    log.info(
        "rollover.complete contact=%s session=%d notified=%d failed=%d",
        contact_id, session["id"], len(notified), len(failed),
    )
    return {"received": True, "rollover": "complete", "notified": len(notified), "failed": len(failed)}
