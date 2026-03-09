import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import httpx
import sqlite3

from db import db

AI_GATE_SCHEMA = {
    "name": "follow_up_gate",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "needs_follow_up": {"type": "string", "enum": ["YES", "NO"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
            },
        },
        "required": ["needs_follow_up", "confidence", "evidence"],
    },
}


@dataclass
class AIGateConfig:
    enabled: bool
    openai_api_key: str
    openai_base_url: str
    model: str
    gap_hours: float
    max_messages: int
    timeout_seconds: float
    max_output_tokens: int
    on_every_inbound: bool
    decision_mode: str
    on_every_inbound_no_confidence: float
    primary_suppress_no_confidence: float
    now_local: Callable[[], dt.datetime]
    msg_is_staff_outbound: Callable[[Dict[str, Any]], bool]
    msg_ts: Callable[[Dict[str, Any]], Optional[dt.datetime]]
    msg_text: Callable[[Dict[str, Any]], str]
    suppress_no_confidence: float = 0.90
    redact_pii: bool = True


def _ai_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _select_context_window(
    msgs: List[Dict[str, Any]],
    *,
    gap_hours: float,
    max_messages: int,
    is_staff_outbound: Callable[[Dict[str, Any]], bool],
    msg_ts: Callable[[Dict[str, Any]], Optional[dt.datetime]],
) -> List[Dict[str, Any]]:
    """
    Choose a small, recent slice to avoid mixing multiple mini-conversations.

    Strategy (deterministic):
      - Walk backwards from newest message
      - Stop if we hit a long silence gap (AI_GATE_GAP_HOURS)
      - Also stop once we've included the most recent staff outbound and there is
        at least one newer message after it
      - Always cap at AI_GATE_MAX_MESSAGES
    """
    items: List[Tuple[dt.datetime, Dict[str, Any]]] = []
    for m in msgs or []:
        ts = msg_ts(m)
        if ts is None:
            continue
        items.append((ts, m))
    items.sort(key=lambda x: x[0])
    if not items:
        return []

    selected: List[Tuple[dt.datetime, Dict[str, Any]]] = []
    last_ts: Optional[dt.datetime] = None
    saw_newer_than_staff = False

    for ts, m in reversed(items):
        if last_ts is not None:
            gap = (last_ts - ts).total_seconds()
            if gap >= (gap_hours * 3600.0):
                break

        selected.append((ts, m))
        last_ts = ts

        if is_staff_outbound(m):
            if saw_newer_than_staff:
                break
        else:
            saw_newer_than_staff = True

        if len(selected) >= max_messages:
            break

    selected.reverse()
    return [m for _, m in selected]


def _build_ai_transcript(
    window: List[Dict[str, Any]],
    *,
    msg_is_staff_outbound: Callable[[Dict[str, Any]], bool],
    msg_text: Callable[[Dict[str, Any]], str],
    redact_pii: bool,
) -> str:
    def _redact_pii_text(s: str) -> str:
        t = s or ""
        if not redact_pii:
            return t
        t = re.sub(r"\b[\w.\-+%]+@[\w.\-]+\.[A-Za-z]{2,}\b", "[EMAIL]", t)
        t = re.sub(r"https?://\S+", "[URL]", t)
        t = re.sub(r"\+?\d[\d\-\(\) ]{7,}\d", "[PHONE]", t)
        return t

    lines: List[str] = []
    for m in window or []:
        role = "INTERNAL" if msg_is_staff_outbound(m) else "CUSTOMER"
        txt = (msg_text(m) or "").replace("\n", " ").strip()
        if not txt:
            continue
        txt = _redact_pii_text(txt)
        if len(txt) > 500:
            txt = txt[:500] + "…"
        lines.append(f"[{role}] {txt}")
    return "\n".join(lines)


def _ai_gate_db_get(conversation_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute(
        "SELECT * FROM conversation_ai_gate WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    conn.close()
    return row


def _ai_gate_db_put(
    conversation_id: str,
    last_msg_ts: str,
    result: Dict[str, Any],
    *,
    model: str,
    now_local: Callable[[], dt.datetime],
) -> None:
    conn = db()
    conn.execute(
        """
        INSERT INTO conversation_ai_gate
          (conversation_id, last_msg_ts, needs_follow_up, confidence, evidence_json, model, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
          last_msg_ts=excluded.last_msg_ts,
          needs_follow_up=excluded.needs_follow_up,
          confidence=excluded.confidence,
          evidence_json=excluded.evidence_json,
          model=excluded.model,
          created_ts=excluded.created_ts
        """,
        (
            conversation_id,
            last_msg_ts,
            str(result.get("needs_follow_up") or "YES"),
            float(result.get("confidence") or 0.0),
            json.dumps(result.get("evidence") or []),
            model,
            now_local().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


async def ai_gate_classify(
    conversation_id: str,
    msgs: List[Dict[str, Any]],
    cfg: AIGateConfig,
) -> Dict[str, Any]:
    """
    Returns {"needs_follow_up":"YES|NO","confidence":float,"evidence":[...]}.

    Fail-open behavior:
      - If anything goes wrong (missing key, API failure, JSON parse), return YES with low confidence.
      - We only suppress escalation when we get a confident NO.
    """
    if not cfg.enabled:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["ai gate disabled"]}

    if not cfg.openai_api_key:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["missing OPENAI_API_KEY"]}

    window = _select_context_window(
        msgs,
        gap_hours=cfg.gap_hours,
        max_messages=cfg.max_messages,
        is_staff_outbound=cfg.msg_is_staff_outbound,
        msg_ts=cfg.msg_ts,
    )
    if not window:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["no messages available"]}

    last_dt = cfg.msg_ts(window[-1])
    last_msg_ts = (
        last_dt.astimezone(dt.timezone.utc).isoformat()
        if last_dt
        else cfg.now_local().astimezone(dt.timezone.utc).isoformat()
    )

    cached = _ai_gate_db_get(conversation_id)
    if cached and str(cached["last_msg_ts"]) == last_msg_ts:
        try:
            return {
                "needs_follow_up": str(cached["needs_follow_up"]),
                "confidence": float(cached["confidence"]),
                "evidence": json.loads(cached["evidence_json"] or "[]"),
                "cached": True,
            }
        except Exception:
            pass

    transcript = _build_ai_transcript(
        window,
        msg_is_staff_outbound=cfg.msg_is_staff_outbound,
        msg_text=cfg.msg_text,
        redact_pii=cfg.redact_pii,
    )
    if not (transcript or "").strip():
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["empty transcript"]}

    sys = (
        "You are a classifier for a pool service business SMS thread. "
        "Decide if the business owes a follow-up to the customer. "
        "Bias toward YES if uncertain (missing a waiting customer is worse than a false alarm). "
        "Return only valid JSON matching the schema."
    )
    user = (
        "Definitions:\n"
        "- FOLLOW-UP NEEDED means the customer is waiting on us for an answer, action, or scheduling.\n"
        "- FOLLOW-UP NOT NEEDED means the thread is resolved or the customer is only acknowledging (thanks/ok/fixed it).\n\n"
        "Messages (most recent last):\n"
        f"{transcript}"
    )

    payload = {
        "model": cfg.model,
        "input": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": AI_GATE_SCHEMA["name"],
                "schema": AI_GATE_SCHEMA["schema"],
            }
        },
        "max_output_tokens": max(16, cfg.max_output_tokens),
        "store": False,
    }

    try:
        async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
            r = await client.post(f"{cfg.openai_base_url}/responses", headers=_ai_headers(cfg.openai_api_key), json=payload)
            if r.status_code >= 400:
                return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": [f"ai error {r.status_code}"]}
            data = r.json()

        if str(data.get("status") or "").lower() == "incomplete":
            reason = (
                (data.get("incomplete_details") or {}).get("reason")
                if isinstance(data.get("incomplete_details"), dict)
                else None
            )
            if reason:
                return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": [f"ai incomplete {reason}"]}
            return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["ai incomplete"]}

        txt = (data.get("output_text") or "").strip()
        if not txt:
            for item in (data.get("output") or []):
                if item.get("type") == "message":
                    for part in (item.get("content") or []):
                        if part.get("type") == "output_text" and part.get("text"):
                            txt = str(part.get("text") or "").strip()
                            break
                if txt:
                    break
        if not txt:
            return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["ai empty response"]}

        result = json.loads(txt)
        nf = str(result.get("needs_follow_up") or "YES").upper()
        if nf not in ("YES", "NO"):
            nf = "YES"
        conf = float(result.get("confidence") or 0.0)
        conf = max(0.0, min(1.0, conf))
        evidence = result.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        evidence = [str(x)[:200] for x in evidence if str(x).strip()][:3]
        if not evidence:
            evidence = ["no evidence"]

        out = {"needs_follow_up": nf, "confidence": conf, "evidence": evidence}
        try:
            _ai_gate_db_put(
                conversation_id,
                last_msg_ts,
                out,
                model=cfg.model,
                now_local=cfg.now_local,
            )
        except Exception:
            pass
        return out
    except Exception:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["ai exception"]}


async def ai_inbound_should_suppress(
    conversation_id: Optional[str],
    cfg: AIGateConfig,
    list_messages: Callable[[str, int], Awaitable[List[Dict[str, Any]]]],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Ingest-time AI gate.

    - ai_primary mode: always runs when conversation_id exists
    - ai_gate_on_every_inbound mode: runs when enabled
    suppresses issue creation/update on NO above configured confidence threshold
    """
    ai_primary = cfg.decision_mode == "ai_primary"
    if not (cfg.on_every_inbound or ai_primary):
        return False, None
    if not conversation_id:
        return False, None

    try:
        msgs = await list_messages(conversation_id, 50)
        gate = await ai_gate_classify(conversation_id, msgs, cfg)
        threshold = (
            cfg.primary_suppress_no_confidence
            if ai_primary
            else cfg.on_every_inbound_no_confidence
        )
        suppress = (
            str(gate.get("needs_follow_up") or "").upper() == "NO"
            and float(gate.get("confidence") or 0.0) >= threshold
        )
        if isinstance(gate, dict):
            gate["decision_mode"] = cfg.decision_mode
            gate["suppress_threshold"] = threshold
        return suppress, gate
    except Exception:
        return False, None
