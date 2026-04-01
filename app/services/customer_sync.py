import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple


PROTECTED_GHL_TYPES = {"internal", "vendor", "do_not_contact"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"", "none", "nan"} else text


def normalize_phone(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def format_e164_phone(value: Any) -> str:
    digits = normalize_phone(value)
    return f"+1{digits}" if digits else ""


def normalize_email(value: Any) -> str:
    text = clean_text(value)
    return text.lower() if text else ""


def normalize_name(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", text)


def derive_customer_status(is_inactive: Any, is_lead: Any) -> str:
    if bool(is_inactive):
        return "past"
    if bool(is_lead):
        return "lead"
    return "active"


def desired_ghl_type(customer_status: str) -> str:
    value = clean_text(customer_status).lower()
    if value == "past":
        return "past_customer"
    if value == "lead":
        return "lead"
    return "customer"


def customer_display_name(customer: Dict[str, Any]) -> str:
    first_name = clean_text(customer.get("first_name"))
    last_name = clean_text(customer.get("last_name"))
    company_name = clean_text(customer.get("company_name"))
    full_name = " ".join(part for part in [first_name, last_name] if part).strip()
    return full_name or company_name or clean_text(customer.get("source_customer_id")) or "(unnamed)"


def build_customer_sync_record(row: Dict[str, Any]) -> Dict[str, Any]:
    first_name = clean_text(row.get("first_name"))
    last_name = clean_text(row.get("last_name"))
    company_name = clean_text(row.get("company_name"))
    display_name = " ".join(part for part in [first_name, last_name] if part).strip() or company_name
    email = normalize_email(row.get("email"))
    phones = [
        format_e164_phone(row.get("phone")),
        format_e164_phone(row.get("mobile_phone")),
    ]
    phones = [phone for phone in phones if phone]
    names = sorted(
        {
            value
            for value in [
                normalize_name(display_name),
                normalize_name(company_name),
                normalize_name(f"{first_name} {last_name}"),
            ]
            if value
        }
    )
    status = derive_customer_status(row.get("is_inactive"), row.get("is_lead")) or clean_text(row.get("customer_status"))
    return {
        "id": row.get("id"),
        "source_customer_id": clean_text(row.get("source_customer_id")),
        "first_name": first_name,
        "last_name": last_name,
        "company_name": company_name,
        "email": email,
        "phones": phones,
        "primary_phone": phones[0] if phones else "",
        "names": names,
        "customer_status": status,
        "desired_type": desired_ghl_type(status),
        "ghl_contact_id": clean_text(row.get("ghl_contact_id")),
        "display_name": display_name,
    }


def build_ghl_contact_indexes(contacts: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    by_phone: defaultdict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_email: defaultdict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_name: defaultdict[str, List[Dict[str, Any]]] = defaultdict(list)

    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        contact_id = clean_text(contact.get("id"))
        if contact_id:
            by_id[contact_id] = contact

        phones = [format_e164_phone(contact.get("phone"))]
        phones.extend(format_e164_phone(value) for value in (contact.get("additionalPhones") or []))
        emails = [normalize_email(contact.get("email"))]
        emails.extend(normalize_email(value) for value in (contact.get("additionalEmails") or []))
        names = [
            normalize_name(contact.get("contactName")),
            normalize_name(f"{clean_text(contact.get('firstName'))} {clean_text(contact.get('lastName'))}"),
            normalize_name(contact.get("companyName")),
        ]

        for phone in [value for value in phones if value]:
            by_phone[phone].append(contact)
        for email in [value for value in emails if value]:
            by_email[email].append(contact)
        for name in [value for value in names if value]:
            by_name[name].append(contact)

    return {
        "by_id": by_id,
        "by_phone": dict(by_phone),
        "by_email": dict(by_email),
        "by_name": dict(by_name),
    }


def match_ghl_contact(
    customer: Dict[str, Any],
    indexes: Dict[str, Dict[str, List[Dict[str, Any]]]],
) -> Tuple[Optional[Dict[str, Any]], List[str], bool]:
    existing_contact_id = clean_text(customer.get("ghl_contact_id"))
    if existing_contact_id:
        existing = indexes["by_id"].get(existing_contact_id)
        if existing:
            return existing, ["stored_contact_id"], False

    scores: Dict[str, Dict[str, Any]] = {}

    def _score(contact: Dict[str, Any], method: str, weight: int) -> None:
        contact_id = clean_text(contact.get("id"))
        if not contact_id:
            return
        entry = scores.setdefault(contact_id, {"contact": contact, "score": 0, "methods": set()})
        entry["score"] += weight
        entry["methods"].add(method)

    for phone in customer.get("phones", []):
        for contact in indexes["by_phone"].get(phone, []):
            _score(contact, "phone", 100)
    email = normalize_email(customer.get("email"))
    if email:
        for contact in indexes["by_email"].get(email, []):
            _score(contact, "email", 70)
    for name in customer.get("names", []):
        for contact in indexes["by_name"].get(name, []):
            _score(contact, "name", 20)

    if not scores:
        return None, [], False

    ranked = sorted(scores.values(), key=lambda item: (-item["score"], clean_text(item["contact"].get("id"))))
    top = ranked[0]
    ambiguous = len(ranked) > 1 and ranked[1]["score"] == top["score"]
    return top["contact"], sorted(top["methods"]), ambiguous


def build_ghl_contact_payload(customer: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "firstName": clean_text(customer.get("first_name")),
        "lastName": clean_text(customer.get("last_name")),
        "name": clean_text(customer.get("display_name")),
        "companyName": clean_text(customer.get("company_name")),
        "email": normalize_email(customer.get("email")),
        "phone": clean_text(customer.get("primary_phone")),
        "type": clean_text(customer.get("desired_type")),
    }
    return {key: value for key, value in payload.items() if value}


def contact_needs_update(contact: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    checks = {
        "firstName": clean_text(contact.get("firstName")),
        "lastName": clean_text(contact.get("lastName")),
        "email": normalize_email(contact.get("email")),
        "phone": format_e164_phone(contact.get("phone")),
        "type": clean_text(contact.get("type")),
    }
    desired = {
        "firstName": clean_text(payload.get("firstName")),
        "lastName": clean_text(payload.get("lastName")),
        "email": normalize_email(payload.get("email")),
        "phone": format_e164_phone(payload.get("phone")),
        "type": clean_text(payload.get("type")),
    }
    for key, desired_value in desired.items():
        if desired_value and checks.get(key) != desired_value:
            return True
    return False
