import json
import datetime as dt
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request

from handlers.sms import (
    extract_contact_id,
    extract_contact_name,
    extract_contact_type,
    extract_conversation_id,
    extract_direction,
    extract_from_phone,
    extract_text,
    is_internal_sender,
    is_ack_closeout,
    normalize_phone,
)


@dataclass
class SMSRouteDeps:
    tz_name: str
    sms_sla_hours: float
    ack_close_enabled: bool
    ack_close_window_mode: str
    ack_close_window_hours: float
    ack_close_max_len: int
    internal_contact_ids: Set[str]
    auth_or_401: Callable[[Request], None]
    parse_request_payload: Callable[[Request], Awaitable[Dict[str, Any]]]
    log_raw_event: Callable[[str, Dict[str, Any]], None]
    flow_who: Callable[[Optional[str], Optional[str], Optional[str]], str]
    now_local: Callable[[], dt.datetime]
    add_business_hours: Callable[[dt.datetime, float], dt.datetime]
    ghl_find_conversation_id_for_contact: Callable[[Optional[str], Optional[str]], Awaitable[Optional[str]]]
    set_last_internal_outbound: Callable[[str, str, Optional[str]], None]
    manager_conversation_for_contact: Callable[[str], Awaitable[Optional[str]]]
    ghl_send_message: Callable[[str, str, str], Awaitable[Dict[str, Any]]]
    recent_staff_outbound_ts: Callable[[str], Awaitable[Optional[dt.datetime]]]
    flow_log: Callable[..., None]
    get_last_internal_outbound: Callable[[str], Optional[str]]
    parse_iso_dt: Callable[[Any], Optional[dt.datetime]]
    business_day_end_for: Callable[[dt.datetime], dt.datetime]
    ai_inbound_should_suppress: Callable[[Optional[str]], Awaitable[Tuple[bool, Optional[Dict[str, Any]]]]]
    db: Callable[[], Any]
    ghl_get_contact_name: Callable[[Optional[str]], Awaitable[Optional[str]]]
    list_open_issues: Callable[[int, int], Tuple[List[dict], int]]
    set_issue_contact_name: Callable[[int, str], None]
    render_list_like_summary: Callable[[List[dict], int, int, int], str]
    get_issue_by_id: Callable[[int], Any]
    add_note: Callable[[int, str], bool]
    resolve_by_id: Callable[[int, str], int]
    resolve_target: Callable[[str], int]
    mark_spam: Callable[[str], None]
    resolve_by_phone: Callable[[str, str], int]
    ghl_conversation_link: Callable[[Optional[str]], Optional[str]]


def register_sms_routes(app: FastAPI, deps: SMSRouteDeps) -> None:
    manager_list_offsets: Dict[str, int] = {}

    def _parse_issue_id(token: str) -> Optional[int]:
        t = (token or "").strip()
        if t.startswith("#"):
            t = t[1:]
        return int(t) if t.isdigit() else None

    async def handle_command(
        text: str, command_contact_id: Optional[str], command_from_phone: Optional[str]
    ) -> Dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            return {"ok": False, "ignored": "empty"}

        raw = raw.replace(",", " ")
        parts = raw.split()
        if not parts:
            return {"ok": False, "ignored": "empty"}

        cmd = parts[0].strip().lower()
        args = parts[1:]
        known = {"list", "more", "open", "resolve", "spam", "note"}
        if cmd not in known:
            return {"ok": False, "ignored": "not_a_command"}

        def _parse_ids(tokens: List[str]) -> List[int]:
            ids: List[int] = []
            for t in tokens:
                tt = t.strip()
                if tt.startswith("#"):
                    tt = tt[1:]
                if tt.isdigit():
                    ids.append(int(tt))
            seen = set()
            out: List[int] = []
            for i in ids:
                if i not in seen:
                    out.append(i)
                    seen.add(i)
            return out

        if cmd == "list":
            if not command_contact_id:
                return {"ok": False, "error": "Missing manager contact id"}
            limit = 5
            offset = 0
            manager_list_offsets[command_contact_id] = offset
            rows, total = deps.list_open_issues(limit=limit, offset=offset)
            if total == 0:
                return {"ok": True, "cmd": "LIST", "text": "No OPEN issues."}
            for r in rows:
                if not (r.get("contact_name") or "").strip() and r.get("contact_id"):
                    fetched = await deps.ghl_get_contact_name(r["contact_id"])
                    if fetched:
                        deps.set_issue_contact_name(r["id"], fetched)
                        r["contact_name"] = fetched
            body = deps.render_list_like_summary(rows, total_open=total, offset=offset, limit=limit)
            return {"ok": True, "cmd": "LIST", "text": body}

        if cmd == "more":
            if not command_contact_id:
                return {"ok": False, "error": "Missing manager contact id"}
            limit = 5
            offset = manager_list_offsets.get(command_contact_id, 0) + limit
            manager_list_offsets[command_contact_id] = offset
            rows, total = deps.list_open_issues(limit=limit, offset=offset)
            if not rows:
                manager_list_offsets[command_contact_id] = 0
                return {"ok": True, "cmd": "MORE", "text": "No more OPEN issues. Reply: List"}
            for r in rows:
                if not (r.get("contact_name") or "").strip() and r.get("contact_id"):
                    fetched = await deps.ghl_get_contact_name(r["contact_id"])
                    if fetched:
                        deps.set_issue_contact_name(r["id"], fetched)
                        r["contact_name"] = fetched
            body = deps.render_list_like_summary(rows, total_open=total, offset=offset, limit=limit)
            return {"ok": True, "cmd": "MORE", "text": body}

        if cmd == "open":
            if not args:
                return {"ok": False, "error": "Usage: Open <id>"}
            iid = _parse_issue_id(args[0])
            if not iid:
                return {"ok": False, "error": "Invalid issue id"}
            r = deps.get_issue_by_id(iid)
            if not r:
                return {"ok": False, "error": "Issue not found"}
            name = r["contact_name"] or r["phone"] or "unknown"
            link = deps.ghl_conversation_link(r["conversation_id"])
            txt = f"{name}: {link}" if link else f"{name}: conversation_id={r['conversation_id'] or '-'}"
            return {"ok": True, "cmd": "OPEN", "id": iid, "text": txt}

        if cmd == "note":
            if len(args) < 2:
                return {"ok": False, "error": "Usage: Note <id> <text>"}
            iid = _parse_issue_id(args[0])
            if not iid:
                return {"ok": False, "error": "Invalid issue id"}
            note_text = " ".join(args[1:]).strip()
            ok = deps.add_note(iid, note_text)
            return {"ok": ok, "cmd": "NOTE", "id": iid, "text": ("Noted." if ok else "Issue not found.")}

        if cmd == "resolve":
            if not args:
                return {"ok": False, "error": "Usage: Resolve <id...>  OR  Resolve <phone/contactId/name>"}
            ids = _parse_ids(args)
            if ids:
                changed: List[int] = []
                for iid in ids:
                    if deps.resolve_by_id(iid, status="RESOLVED") > 0:
                        changed.append(iid)
                if changed:
                    return {
                        "ok": True,
                        "cmd": "RESOLVE",
                        "ids": changed,
                        "text": f"Sentinel: Resolved {', '.join(str(x) for x in changed)}.",
                    }
                return {"ok": True, "cmd": "RESOLVE", "ids": ids, "text": "Sentinel: No matching OPEN issues for those IDs."}
            target = " ".join(args).strip()
            resolved = deps.resolve_target(target)
            return {
                "ok": True,
                "cmd": "RESOLVE",
                "resolved": resolved,
                "target": target,
                "text": f"Sentinel: Resolved {resolved} issue(s) for '{target}'.",
            }

        if cmd == "spam":
            if not args:
                return {"ok": False, "error": "Usage: Spam <id...>  OR  Spam <phone>"}
            ids = _parse_ids(args)
            if ids:
                marked: List[int] = []
                for iid in ids:
                    r = deps.get_issue_by_id(iid)
                    if r and r["phone"]:
                        try:
                            deps.mark_spam(r["phone"])
                        except Exception:
                            pass
                    if deps.resolve_by_id(iid, status="SPAM") > 0:
                        marked.append(iid)
                if marked:
                    return {
                        "ok": True,
                        "cmd": "SPAM",
                        "ids": marked,
                        "text": f"Sentinel: Marked SPAM {', '.join(str(x) for x in marked)}.",
                    }
                return {"ok": True, "cmd": "SPAM", "ids": ids, "text": "Sentinel: No matching OPEN issues for those IDs."}
            phone = normalize_phone(args[0])
            if not phone:
                return {"ok": False, "error": "Invalid phone or IDs"}
            deps.mark_spam(phone)
            deps.resolve_by_phone(phone, status="SPAM")
            return {"ok": True, "cmd": "SPAM", "phone": phone, "text": f"Sentinel: Marked SPAM {phone}."}

        return {"ok": False, "error": "Unknown command"}

    @app.post("/webhook/ghl/inbound_sms")
    async def inbound_sms(request: Request):
        deps.auth_or_401(request)
        payload = await deps.parse_request_payload(request)
        deps.log_raw_event("inbound_sms", payload)

        text = extract_text(payload)
        contact_id = extract_contact_id(payload)
        from_phone = extract_from_phone(payload)
        conversation_id = extract_conversation_id(payload)

        contact_name = extract_contact_name(payload)
        if not contact_name and contact_id:
            try:
                contact_name = await deps.ghl_get_contact_name(contact_id)
            except Exception:
                contact_name = None
        direction = extract_direction(payload)
        contact_type = extract_contact_type(payload)
        is_internal = is_internal_sender(contact_type, contact_id, deps.internal_contact_ids)
        who = deps.flow_who(contact_name, from_phone, contact_id)

        now_local = deps.now_local()
        created_ts = now_local.isoformat()
        due_ts = deps.add_business_hours(now_local, deps.sms_sla_hours).isoformat()

        if not conversation_id:
            try:
                conversation_id = await deps.ghl_find_conversation_id_for_contact(contact_id, from_phone)
            except Exception:
                conversation_id = None

        if conversation_id and is_internal:
            deps.set_last_internal_outbound(conversation_id, created_ts, contact_id)

        if direction in ("outbound", "outgoing"):
            deps.flow_log("sms.ignored_outbound", who=who, contact_id=contact_id, conversation_id=conversation_id)
            return {"received": True, "ignored": "outbound"}

        if is_internal:
            result = await handle_command(text=text, command_contact_id=contact_id, command_from_phone=from_phone)
            if result.get("ok") and result.get("text") and contact_id:
                try:
                    conv_id = await deps.manager_conversation_for_contact(contact_id)
                    if conv_id:
                        await deps.ghl_send_message(conv_id, contact_id, result["text"])
                except Exception:
                    pass
                deps.flow_log("sms.internal_command", who=who, contact_id=contact_id, command=result.get("cmd"))
                return {"received": True, "command": True, "result": result}

            deps.flow_log("sms.ignored_internal_non_command", who=who, contact_id=contact_id)
            return {"received": True, "ignored": "internal_non_command"}

        if conversation_id and deps.ack_close_enabled:
            last_internal_ts = deps.get_last_internal_outbound(conversation_id)
            last_dt = deps.parse_iso_dt(last_internal_ts)
            # Fallback: reaction/ack can arrive before periodic jobs update cached internal outbound ts.
            # If payload looks like close-out and cache is empty, probe recent conversation messages.
            if last_dt is None and is_ack_closeout(text, max_len=deps.ack_close_max_len):
                try:
                    last_dt = await deps.recent_staff_outbound_ts(conversation_id)
                except Exception:
                    last_dt = None
            if last_dt:
                last_dt_local = (
                    last_dt.astimezone(ZoneInfo(deps.tz_name))
                    if last_dt.tzinfo
                    else last_dt.replace(tzinfo=ZoneInfo(deps.tz_name))
                )
                if deps.ack_close_window_mode == "eod":
                    window_end = deps.business_day_end_for(last_dt_local)
                    within_window = (now_local >= last_dt_local) and (now_local <= window_end)
                else:
                    delta = now_local - last_dt_local
                    within_window = 0 <= delta.total_seconds() <= (deps.ack_close_window_hours * 3600.0)

                if within_window and is_ack_closeout(text, max_len=deps.ack_close_max_len):
                    deps.flow_log(
                        "sms.ignored_ack_closeout",
                        who=who,
                        contact_id=contact_id,
                        conversation_id=conversation_id,
                    )
                    return {"received": True, "ignored": "ack_closeout_after_staff_reply"}

        ai_suppress, ai_gate = await deps.ai_inbound_should_suppress(conversation_id)
        if ai_gate is not None:
            deps.flow_log(
                "ai_gate.inbound_sms",
                who=who,
                contact_id=contact_id,
                conversation_id=conversation_id,
                needs_follow_up=str(ai_gate.get("needs_follow_up")),
                confidence=float(ai_gate.get("confidence") or 0.0),
                suppressed=bool(ai_suppress),
            )
        if ai_suppress:
            return {"received": True, "ignored": "ai_inbound_suppress"}

        conn = deps.db()
        row = None
        if conversation_id:
            row = conn.execute(
                "SELECT * FROM issues WHERE status IN ('PENDING','OPEN') AND issue_type='SMS' AND conversation_id=? ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        if row is None and from_phone:
            row = conn.execute(
                "SELECT * FROM issues WHERE status IN ('PENDING','OPEN') AND issue_type='SMS' AND phone=? ORDER BY id DESC LIMIT 1",
                (from_phone,),
            ).fetchone()

        if row is None:
            meta: Dict[str, Any] = {"last_text": text[:500], "source": "inbound_sms_webhook"}
            if contact_name:
                meta["contact_name"] = contact_name
            cur = conn.execute(
                """
                INSERT INTO issues
                  (issue_type, contact_id, phone, contact_name, created_ts, due_ts, status, meta,
                   first_inbound_ts, last_inbound_ts, inbound_count, outbound_count, conversation_id)
                VALUES
                  ('SMS', ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, 1, 0, ?)
            """,
                (
                    contact_id,
                    from_phone,
                    contact_name or None,
                    created_ts,
                    due_ts,
                    json.dumps(meta),
                    created_ts,
                    created_ts,
                    conversation_id,
                ),
            )
            deps.flow_log(
                "sms.issue_created",
                issue_id=cur.lastrowid,
                who=who,
                contact_id=contact_id,
                conversation_id=conversation_id,
                status="PENDING",
                due_ts=due_ts,
            )
        else:
            meta = {}
            try:
                if row["meta"]:
                    meta = json.loads(row["meta"])
            except Exception:
                meta = {}
            meta["last_text"] = text[:500]
            meta["updated_by"] = "inbound_sms_webhook"
            if contact_name and not meta.get("contact_name"):
                meta["contact_name"] = contact_name
            conn.execute(
                """
                UPDATE issues
                SET last_inbound_ts=?,
                    inbound_count=COALESCE(inbound_count,0)+1,
                    contact_id=COALESCE(contact_id, ?),
                    phone=COALESCE(phone, ?),
                    conversation_id=COALESCE(conversation_id, ?),
                    contact_name=CASE WHEN (contact_name IS NULL OR contact_name='') THEN ? ELSE contact_name END,
                    meta=?
                WHERE id=?
            """,
                (created_ts, contact_id, from_phone, conversation_id, contact_name or None, json.dumps(meta), row["id"]),
            )
            deps.flow_log(
                "sms.issue_updated",
                issue_id=row["id"],
                who=who,
                contact_id=contact_id,
                conversation_id=conversation_id,
                status=row["status"],
            )

        conn.commit()
        conn.close()
        return {"received": True, "issue_created_or_updated": True}
