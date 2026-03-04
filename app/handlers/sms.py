import re
from typing import Any, Dict, Optional, Set

_ACK_EMOJI_RE = re.compile(
    r"^[\s\W_]*(?:👍|👌|✅|🙏|🙂|😀|😄|😊|🙌|🤝|🎉|🥰|😅|😂|😉)+[\s\W_]*$",
    re.UNICODE,
)

_ACK_PHRASES = {
    "thanks",
    "thank you",
    "thx",
    "ty",
    "ok",
    "okay",
    "k",
    "kk",
    "cool",
    "cool thanks",
    "sounds good",
    "sg",
    "got it",
    "perfect",
    "great",
    "awesome",
    "cool beans",
    "nice",
    "all good",
    "all good now",
    "we're good",
    "we are good",
    "no worries",
    "no problem",
    "done",
    "resolved",
    "handled",
    "taken care of",
    "took care of it",
    "fixed",
    "fixed it",
    "i fixed it",
    "we fixed it",
    "got it fixed",
    "cancel",
    "cancelled",
    "nevermind",
    "never mind",
}

_ACK_REACTION_PREFIXES = (
    "liked",
    "loved",
    "disliked",
    "laughed at",
    "emphasized",
    "questioned",
)


def normalize_phone(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    s = str(p).strip()
    s = re.sub(r"[^\d\+]", "", s)
    if s.startswith("00"):
        s = "+" + s[2:]
    if s and s[0] != "+" and len(re.sub(r"\D", "", s)) == 10:
        s = "+1" + re.sub(r"\D", "", s)
    return s


def extract_text(payload: Dict[str, Any]) -> str:
    for k in ("body", "message", "text", "content", "Message"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    for k in ("data", "sms", "message", "Message"):
        v = payload.get(k)
        if isinstance(v, dict):
            t = extract_text(v)
            if t:
                return t
    return ""


def extract_conversation_id(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("conversationId", "conversation_id", "conversation", "conversationID"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("id", "conversationId"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    d = payload.get("data")
    if isinstance(d, dict):
        return extract_conversation_id(d)
    return None


def extract_contact_id(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("contactId", "contact_id", "contact", "contactID"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            vv = v.get("id")
            if isinstance(vv, str) and vv.strip():
                return vv.strip()
    d = payload.get("data")
    if isinstance(d, dict):
        return extract_contact_id(d)
    return None


def extract_from_phone(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("from", "fromNumber", "phone", "customerPhone"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return normalize_phone(v)
    d = payload.get("data")
    if isinstance(d, dict):
        return extract_from_phone(d)
    return None


def extract_direction(payload: Dict[str, Any]) -> str:
    for k in ("direction", "type"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    d = payload.get("data")
    if isinstance(d, dict):
        return extract_direction(d)
    return ""


def extract_contact_type(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("contactType", "contact_type", "type"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()

    d = payload.get("contact") or payload.get("data")
    if isinstance(d, dict):
        for k in ("contactType", "contact_type", "type"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    return None


def extract_contact_name(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("contactName", "fullName", "full_name", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    for container_key in ("contact", "data"):
        d = payload.get(container_key)
        if isinstance(d, dict):
            for k in ("contactName", "fullName", "full_name", "name"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None


def is_internal_sender(contact_type: Optional[str], contact_id: Optional[str], internal_contact_ids: Set[str]) -> bool:
    if contact_type and contact_type.lower() == "internal":
        return True
    if contact_id and contact_id in internal_contact_ids:
        return True
    return False


def _normalize_text_for_match(s: str) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t2 = re.sub(r"[^\w\s]", "", t).strip()
    return t2


def is_ack_closeout(text: Optional[str], max_len: int = 80) -> bool:
    if not text:
        return False
    raw = text.strip()
    if not raw:
        return False
    if len(raw) > max_len:
        return False
    if _ACK_EMOJI_RE.match(raw):
        return True
    t = _normalize_text_for_match(raw)
    if not t:
        return False
    if t in _ACK_PHRASES:
        return True
    has_gratitude = any(x in t for x in ("thanks", "thank you", "thx", "ty"))
    has_ack_intent = any(
        x in t
        for x in (
            "ok",
            "okay",
            "sounds good",
            "got it",
            "perfect",
            "great",
            "awesome",
            "all good",
            "no worries",
            "no problem",
        )
    )
    if has_gratitude and has_ack_intent:
        return True
    if any(t == p or t.startswith(p + " ") for p in _ACK_REACTION_PREFIXES):
        return True
    if t.startswith("fixed it") or t.endswith("fixed it"):
        return True
    if "got it fixed" in t or "took care of" in t or "taken care of" in t:
        return True
    return False
