from fastapi import FastAPI, Request, HTTPException # type: ignore
from fastapi.openapi.utils import get_openapi
import asyncio
import gzip
import os
import json
import sqlite3
import subprocess
import sys
import shutil
import tempfile
import datetime as dt
import time
from typing import Any, Dict, Optional, List, Tuple
import re
import httpx # type: ignore
from zoneinfo import ZoneInfo
from db import db, init_db, ensure_schema, purge_raw_events
from pg import DATABASE_URL, ensure_pg_schema, pg_healthcheck, pg
from handlers.sms import (
    normalize_phone as _normalize_phone,
    extract_text as _extract_text,
    extract_conversation_id as _extract_conversation_id,
    extract_contact_id as _extract_contact_id,
    extract_from_phone as _extract_from_phone,
    extract_direction as _extract_direction,
    extract_contact_type as _extract_contact_type,
    extract_contact_name as _extract_contact_name,
    is_internal_sender as _sms_is_internal_sender,
    is_ack_closeout as _sms_is_ack_closeout,
)
from handlers.sms_routes import SMSRouteDeps, register_sms_routes
from handlers.webhook_routes import WebhookRouteDeps, register_webhook_routes
from services.ai_gate import (
    AIGateConfig,
    ai_gate_classify as _ai_gate_classify,
    ai_inbound_should_suppress as _ai_inbound_should_suppress_impl,
)
from services.business_time import (
    BusinessTimeConfig,
    add_business_hours as add_business_hours_for_config,
    business_day_end_for,
    fmt_as_of_local,
    fmt_date_local,
    fmt_dt_local,
    is_escalated,
    is_recent,
    now_local as services_now_local,
    parse_ghl_date,
    parse_hhmm,
    parse_iso,
    parse_iso_dt,
)
from services.issues import (
    add_note as issue_add_note,
    get_issue_by_id as issue_get_issue_by_id,
    kv_get,
    kv_set,
    list_open_issues as issue_list_open_issues,
    looks_like_contact_id,
    render_list_like_summary,
    resolve_by_contact_id as issue_resolve_by_contact_id,
    resolve_by_id as issue_resolve_by_id,
    resolve_by_name as issue_resolve_by_name,
    resolve_by_phone as issue_resolve_by_phone,
    set_issue_contact_name,
    set_resolved_metadata as issue_set_resolved_metadata,
)
from services.summary import (
    build_section_lines,
    display_name,
    enrich_issues_with_contact_names,
    fetch_overdue_issues,
    fetch_resolved_since,
    fetch_unnotified_breaches,
    summary_title,
)
from services.customer_sync import (
    PROTECTED_GHL_TYPES,
    build_customer_sync_record,
    build_ghl_contact_indexes,
    build_ghl_contact_payload,
    clean_text as sync_clean_text,
    contact_needs_update,
    customer_display_name,
    match_ghl_contact,
)

# ==========================
# Config
# ==========================
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TZ_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Chicago"))
SKIMMER_DB_PATH = os.getenv("SKIMMER_DB_PATH", "/data/skimmer/skimmer.db")
SKIMMER_DOWNLOAD_DIR = os.getenv("SKIMMER_DOWNLOAD_DIR", os.path.dirname(SKIMMER_DB_PATH) or "/data/skimmer")
SKIMMER_LINK_FILE = os.getenv("SKIMMER_LINK_FILE", os.path.join(SKIMMER_DOWNLOAD_DIR, "skimmer_link.txt"))

GHL_APP_BASE = os.getenv("GHL_APP_BASE", "https://app.gohighlevel.com")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")
GOOGLE_OAUTH_TOKEN_URL = os.getenv("GOOGLE_OAUTH_TOKEN_URL", "https://oauth2.googleapis.com/token")
GOOGLE_DRIVE_API_BASE = os.getenv("GOOGLE_DRIVE_API_BASE", "https://www.googleapis.com/drive/v3")
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN", "")
SKIMMER_GDRIVE_FOLDER_ID = os.getenv("SKIMMER_GDRIVE_FOLDER_ID", "")
SKIMMER_GDRIVE_FILE_NAME_REGEX = os.getenv(
    "SKIMMER_GDRIVE_FILE_NAME_REGEX",
    r"(?i)skimmer.*\.(db|sqlite|sqlite3|gz)$",
)
SKIMMER_GDRIVE_SEARCH_DEPTH = max(0, int(os.getenv("SKIMMER_GDRIVE_SEARCH_DEPTH", "3")))

_bh_start_h, _bh_start_m = parse_hhmm(os.getenv("BUSINESS_HOURS_START", "08:00"), 8, 0)
_bh_end_h, _bh_end_m = parse_hhmm(os.getenv("BUSINESS_HOURS_END", "17:00"), 17, 0)
_bh_start_total = (_bh_start_h * 60) + _bh_start_m
_bh_end_total = (_bh_end_h * 60) + _bh_end_m
if _bh_end_total <= _bh_start_total:
    _bh_start_h, _bh_start_m = 8, 0
    _bh_end_h, _bh_end_m = 17, 0
BUSINESS_TIME = BusinessTimeConfig(
    tz_name=TZ_NAME,
    start_hour=_bh_start_h,
    start_minute=_bh_start_m,
    end_hour=_bh_end_h,
    end_minute=_bh_end_m,
)

# GoHighLevel / LeadConnector API
GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com")
GHL_TOKEN = os.getenv("GHL_TOKEN", "")  # Private Integration token (Bearer)
GHL_VERSION = os.getenv("GHL_VERSION", "2021-07-28")
CUSTOMER_SYNC_PAGE_LIMIT = max(1, int(os.getenv("CUSTOMER_SYNC_PAGE_LIMIT", "100")))
CUSTOMER_SYNC_DELAY_SECONDS = float(os.getenv("CUSTOMER_SYNC_DELAY_SECONDS", "0.10"))
# OpenAI (AI follow-up gate; optional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_GATE_ENABLED = os.getenv("AI_GATE_ENABLED", "0").lower() in ("1","true","yes","on")
AI_GATE_MODEL = os.getenv("AI_GATE_MODEL", "gpt-5-mini")
AI_GATE_SUPPRESS_NO_CONFIDENCE = float(os.getenv("AI_GATE_SUPPRESS_NO_CONFIDENCE", "0.90"))
AI_GATE_MAX_MESSAGES = int(os.getenv("AI_GATE_MAX_MESSAGES", "10"))
AI_GATE_GAP_HOURS = float(os.getenv("AI_GATE_GAP_HOURS", "4"))
AI_GATE_MAX_ISSUES_PER_RUN = int(os.getenv("AI_GATE_MAX_ISSUES_PER_RUN", "20"))
AI_GATE_RUN_BUDGET_SECONDS = float(os.getenv("AI_GATE_RUN_BUDGET_SECONDS", "20"))
AI_GATE_TIMEOUT_SECONDS = float(os.getenv("AI_GATE_TIMEOUT_SECONDS", "4"))
AI_GATE_REDACT_PII = os.getenv("AI_GATE_REDACT_PII", "1").lower() in ("1","true","yes","on")
AI_GATE_ON_EVERY_INBOUND = os.getenv("AI_GATE_ON_EVERY_INBOUND", "0").lower() in ("1","true","yes","on")
AI_GATE_ON_EVERY_INBOUND_NO_CONFIDENCE = float(os.getenv("AI_GATE_ON_EVERY_INBOUND_NO_CONFIDENCE", "0.80"))
AI_GATE_MAX_OUTPUT_TOKENS = int(os.getenv("AI_GATE_MAX_OUTPUT_TOKENS", "220"))
DECISION_MODE = os.getenv("DECISION_MODE", "deterministic").strip().lower()
AI_PRIMARY_SUPPRESS_NO_CONFIDENCE = float(os.getenv("AI_PRIMARY_SUPPRESS_NO_CONFIDENCE", "0.65"))

# Summary recipients (managers only, v1)
MANAGER_CONTACT_IDS = [
    s.strip() for s in (os.getenv("MANAGER_CONTACT_IDS", "")).split(",") if s.strip()
]

# Internal manager contact whitelist and reply grace window
INTERNAL_CONTACT_IDS = set(
    x.strip()
    for x in (os.getenv("INTERNAL_CONTACT_IDS", "")).split(",")
    if x.strip()
)
INTERNAL_REPLY_GRACE_HOURS = int(os.getenv("INTERNAL_REPLY_GRACE_HOURS", "12"))

# Ack close-out suppression (customer 'thanks/👍/fixed it' after staff reply)
ACK_CLOSE_ENABLED = os.getenv("ACK_CLOSE_ENABLED", "1").lower() in ("1","true","yes","on")
ACK_CLOSE_WINDOW_MODE = os.getenv("ACK_CLOSE_WINDOW_MODE", "eod").lower()  # 'eod' | 'hours'
ACK_CLOSE_WINDOW_HOURS = float(os.getenv("ACK_CLOSE_WINDOW_HOURS", str(INTERNAL_REPLY_GRACE_HOURS)))
ACK_CLOSE_MAX_LEN = int(os.getenv("ACK_CLOSE_MAX_LEN", "80"))
ACK_CLOSE_IGNORE_WINDOW_FOR_PURE_ACK = os.getenv("ACK_CLOSE_IGNORE_WINDOW_FOR_PURE_ACK", "1").lower() in ("1","true","yes","on")

# Limits to keep SMS short and low-noise
SUMMARY_MAX_ITEMS_PER_SECTION = int(os.getenv("SUMMARY_MAX_ITEMS_PER_SECTION", "8"))
RESOLVED_SINCE_MAX_ITEMS = 5
FLOW_LOG_ENABLED = os.getenv("FLOW_LOG_ENABLED", "1").lower() in ("1", "true", "yes", "on")
RAW_EVENTS_RETENTION_DAYS = int(os.getenv("RAW_EVENTS_RETENTION_DAYS", "30"))

# SLA for customer SMS and CALL response before it is considered an issue (hours)
SMS_SLA_HOURS = float(os.getenv("SMS_SLA_HOURS", "2"))
CALL_SLA_HOURS = float(os.getenv("CALL_SLA_HOURS", "2"))
CALL_DEDUPE_WINDOW_MINUTES = int(os.getenv("CALL_DEDUPE_WINDOW_MINUTES", "240"))
CALL_RESOLVE_LOOKBACK_MINUTES = float(os.getenv("CALL_RESOLVE_LOOKBACK_MINUTES", "15"))
CALL_RESOLVE_POSITIVE_STATUSES = [
    re.sub(r"[^a-z0-9\-]", "", re.sub(r"[\s_]+", "-", s.strip().lower()))
    for s in os.getenv(
        "CALL_RESOLVE_POSITIVE_STATUSES",
        "answered,connected,completed,successful,handled",
    ).split(",")
    if s.strip()
]
CALL_RESOLVE_NEGATIVE_STATUSES = [
    re.sub(r"[^a-z0-9\-]", "", re.sub(r"[\s_]+", "-", s.strip().lower()))
    for s in os.getenv(
        "CALL_RESOLVE_NEGATIVE_STATUSES",
        "missed,no-answer,unanswered,busy,failed,canceled,cancelled,voicemail,ringing",
    ).split(",")
    if s.strip()
]
CALL_REQUIRE_MISSED_MARKER = os.getenv("CALL_REQUIRE_MISSED_MARKER", "1").lower() in ("1", "true", "yes", "on")
CALL_MISSED_MARKER_KEYS = [
    s.strip() for s in os.getenv("CALL_MISSED_MARKER_KEYS", "sentinel_missed_call,missed_call,is_missed_call").split(",") if s.strip()
]
POLL_RESOLVER_CONCURRENCY = max(1, int(os.getenv("POLL_RESOLVER_CONCURRENCY", "8")))

app = FastAPI(
    swagger_ui_parameters={"persistAuthorization": True},
    redoc_url=None,  # ReDoc loads from CDN and shows blank page; use /docs instead
)


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title="NTPP Sentinel", version="1.0.0", routes=app.routes)
    schema.setdefault("components", {})["securitySchemes"] = {
        "ApiKeyHeader": {"type": "apiKey", "in": "header", "name": "X-NTPP-Secret"}
    }
    schema["security"] = [{"ApiKeyHeader": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi

# Shared GHL HTTP client — reused across requests to avoid per-call TLS handshake overhead.
_ghl_client = httpx.AsyncClient(timeout=20.0)

@app.on_event("shutdown")
async def _shutdown_ghl_client():
    await _ghl_client.aclose()


def set_last_internal_outbound(
    conversation_id: str, ts_iso: str, internal_contact_id: Optional[str]
) -> None:
    conn = db()
    _set_last_internal_outbound_conn(conn, conversation_id, ts_iso, internal_contact_id)
    conn.commit()
    conn.close()


def _set_last_internal_outbound_conn(
    conn: sqlite3.Connection,
    conversation_id: str,
    ts_iso: str,
    internal_contact_id: Optional[str],
) -> None:
    conn.execute(
        """
      INSERT INTO conversation_state (conversation_id, last_internal_outbound_ts, last_internal_outbound_contact_id)
      VALUES (?, ?, ?)
      ON CONFLICT(conversation_id) DO UPDATE SET
        last_internal_outbound_ts=excluded.last_internal_outbound_ts,
        last_internal_outbound_contact_id=excluded.last_internal_outbound_contact_id
    """,
        (conversation_id, ts_iso, internal_contact_id),
    )


def get_last_internal_outbound(conversation_id: str) -> Optional[str]:
    conn = db()
    row = conn.execute(
        """
      SELECT last_internal_outbound_ts FROM conversation_state WHERE conversation_id=?
    """,
        (conversation_id,),
    ).fetchone()
    conn.close()
    return row["last_internal_outbound_ts"] if row else None
# ==========================
# Ack close-out helpers
# ==========================
def _is_ack_closeout(text: Optional[str]) -> bool:
    return _sms_is_ack_closeout(text, max_len=ACK_CLOSE_MAX_LEN)

def _business_day_end_for(ts_local: dt.datetime) -> dt.datetime:
    return business_day_end_for(BUSINESS_TIME, ts_local)


@app.on_event("startup")
def _startup():
    os.makedirs("/data", exist_ok=True)
    init_db()
    ensure_schema()
    if DATABASE_URL:
        try:
            ensure_pg_schema()
        except Exception as exc:
            print(f"WARNING: Postgres schema bootstrap failed: {exc}")

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/health/postgres")
def health_postgres():
    if not DATABASE_URL:
        raise HTTPException(status_code=404, detail="DATABASE_URL is not configured")
    try:
        row = pg_healthcheck()
        return {"ok": True, "now": row["now"] if row else None}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ==========================
# Auth helper (shared)
# ==========================
def _auth_or_401(request: Request) -> None:
    secret = request.headers.get("X-NTPP-Secret") or request.query_params.get("secret")
    if not secret or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _skimmer_link_file_path() -> str:
    return SKIMMER_LINK_FILE


def _write_skimmer_link(link: str) -> str:
    path = _skimmer_link_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(link.strip())
    return path


def _ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def _download_url_to_path(url: str, dest_path: str) -> None:
    _ensure_dir(os.path.dirname(dest_path))
    with httpx.stream("GET", url, timeout=120.0, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in response.iter_bytes(chunk_size=8192):
                if chunk:
                    fh.write(chunk)


def _looks_like_gzip(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"\x1f\x8b"
    except Exception:
        return False


def _gunzip_file(src_path: str, dest_path: str) -> None:
    _ensure_dir(os.path.dirname(dest_path))
    with gzip.open(src_path, "rb") as sf, open(dest_path, "wb") as df:
        shutil.copyfileobj(sf, df)


def _download_and_save_skimmer(url: str, dest_path: str) -> None:
    temp_dir = os.path.dirname(dest_path) or SKIMMER_DOWNLOAD_DIR
    _ensure_dir(temp_dir)
    with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False, suffix=".tmp") as tmp:
        temp_path = tmp.name
    try:
        _download_url_to_path(url, temp_path)
        if _looks_like_gzip(temp_path) or url.lower().endswith(".gz"):
            _gunzip_file(temp_path, dest_path)
        else:
            os.replace(temp_path, dest_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


async def _google_access_token() -> str:
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET or not GOOGLE_OAUTH_REFRESH_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth is not fully configured",
        )

    r = await _ghl_client.post(
        GOOGLE_OAUTH_TOKEN_URL,
        data={
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "refresh_token": GOOGLE_OAUTH_REFRESH_TOKEN,
            "grant_type": "refresh_token",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Google token refresh failed: {r.status_code} {r.text[:300]}",
        )

    payload = r.json()
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(status_code=502, detail="Google token refresh returned no access_token")
    return access_token


async def _google_drive_list_folder(folder_id: str, access_token: str) -> List[Dict[str, Any]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "q": f"'{folder_id}' in parents and trashed=false",
        "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink,shortcutDetails),nextPageToken",
        "orderBy": "modifiedTime desc",
        "pageSize": "100",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        "corpora": "allDrives",
    }
    r = await _ghl_client.get(f"{GOOGLE_DRIVE_API_BASE}/files", headers=headers, params=params)
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Google Drive file listing failed: {r.status_code} {r.text[:300]}",
        )

    payload = r.json()
    files = payload.get("files")
    return files if isinstance(files, list) else []


async def _google_drive_list_skimmer_candidates(folder_id: str, access_token: str) -> List[Dict[str, Any]]:
    visited = set()

    async def _walk(current_folder_id: str, depth: int) -> List[Dict[str, Any]]:
        if current_folder_id in visited:
            return []
        visited.add(current_folder_id)

        files = await _google_drive_list_folder(current_folder_id, access_token)
        out: List[Dict[str, Any]] = []
        child_folders: List[str] = []

        for item in files:
            if not isinstance(item, dict):
                continue
            mime_type = str(item.get("mimeType") or "")
            shortcut = item.get("shortcutDetails") if isinstance(item.get("shortcutDetails"), dict) else None
            shortcut_target_id = str((shortcut or {}).get("targetId") or "").strip()
            shortcut_target_mime = str((shortcut or {}).get("targetMimeType") or "").strip()

            if mime_type == "application/vnd.google-apps.folder":
                child_id = str(item.get("id") or "").strip()
                if child_id:
                    child_folders.append(child_id)
                continue

            if (
                mime_type == "application/vnd.google-apps.shortcut"
                and shortcut_target_id
                and shortcut_target_mime == "application/vnd.google-apps.folder"
            ):
                child_folders.append(shortcut_target_id)
                continue

            out.append(item)

        if depth <= 0:
            return out

        for child_folder_id in child_folders:
            out.extend(await _walk(child_folder_id, depth - 1))
        return out

    return await _walk(folder_id, SKIMMER_GDRIVE_SEARCH_DEPTH)


def _pick_latest_skimmer_file(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not files:
        return None

    pattern = None
    try:
        pattern = re.compile(SKIMMER_GDRIVE_FILE_NAME_REGEX)
    except re.error:
        pattern = None

    candidates = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        mime_type = str(item.get("mimeType") or "")
        if mime_type == "application/vnd.google-apps.folder":
            continue
        if pattern is not None and not pattern.search(name):
            continue
        candidates.append(item)

    if candidates:
        return candidates[0]

    for item in files:
        if isinstance(item, dict) and str(item.get("mimeType") or "") != "application/vnd.google-apps.folder":
            return item
    return None


def _download_google_drive_file(file_id: str, access_token: str, dest_path: str) -> None:
    _ensure_dir(os.path.dirname(dest_path))
    download_url = f"{GOOGLE_DRIVE_API_BASE}/files/{file_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    temp_dir = os.path.dirname(dest_path) or SKIMMER_DOWNLOAD_DIR
    with tempfile.NamedTemporaryFile(dir=temp_dir, delete=False, suffix=".tmp") as tmp:
        temp_path = tmp.name
    try:
        with httpx.stream(
            "GET",
            download_url,
            headers=headers,
            params={
                "alt": "media",
                "supportsAllDrives": "true",
            },
            timeout=120.0,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            with open(temp_path, "wb") as fh:
                for chunk in response.iter_bytes(chunk_size=8192):
                    if chunk:
                        fh.write(chunk)

        if _looks_like_gzip(temp_path):
            _gunzip_file(temp_path, dest_path)
        else:
            os.replace(temp_path, dest_path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


async def _sync_skimmer_from_google_drive(import_after: bool = True) -> Dict[str, Any]:
    if not SKIMMER_GDRIVE_FOLDER_ID:
        raise HTTPException(status_code=500, detail="SKIMMER_GDRIVE_FOLDER_ID is not configured")

    access_token = await _google_access_token()
    files = await _google_drive_list_skimmer_candidates(SKIMMER_GDRIVE_FOLDER_ID, access_token)
    chosen = _pick_latest_skimmer_file(files)
    if not chosen:
        raise HTTPException(status_code=404, detail="No matching Skimmer file found in Google Drive folder")

    file_id = str(chosen.get("id") or "").strip()
    file_name = str(chosen.get("name") or "").strip()
    modified_time = str(chosen.get("modifiedTime") or "").strip()
    if not file_id:
        raise HTTPException(status_code=502, detail="Google Drive listing returned a file without an id")

    await asyncio.to_thread(_download_google_drive_file, file_id, access_token, SKIMMER_DB_PATH)

    result = {
        "status": "downloaded",
        "source": "google_drive",
        "skimmer_db_path": SKIMMER_DB_PATH,
        "google_drive_file_id": file_id,
        "google_drive_file_name": file_name,
        "google_drive_modified_time": modified_time or None,
    }
    if import_after:
        result["import"] = _run_skimmer_import()
    return result


# ==========================
# Time / SLA helpers
# ==========================
def _now_local() -> dt.datetime:
    return services_now_local(TZ_NAME)


def _parse_iso_dt(value) -> Optional[dt.datetime]:
    return parse_iso_dt(value)

def _parse_ghl_date(value) -> Optional[dt.datetime]:
    return parse_ghl_date(value)

def add_business_hours(start_local: dt.datetime, hours: float) -> dt.datetime:
    return add_business_hours_for_config(BUSINESS_TIME, start_local, hours)

def _fmt_date_local(d: dt.datetime) -> str:
    return fmt_date_local(d)

def _fmt_as_of_local(d: dt.datetime) -> str:
    return fmt_as_of_local(d)

def ghl_conversation_link(conversation_id: Optional[str]) -> Optional[str]:
    if not conversation_id or not GHL_LOCATION_ID:
        return None
    return f"{GHL_APP_BASE}/v2/location/{GHL_LOCATION_ID}/conversations/conversations/{conversation_id}"


# ==========================
# GHL API helpers
# ==========================
def _ghl_headers() -> Dict[str, str]:
    if not GHL_TOKEN:
        raise HTTPException(status_code=500, detail="Server missing GHL_TOKEN")
    if not GHL_LOCATION_ID:
        raise HTTPException(status_code=500, detail="Server missing GHL_LOCATION_ID")
    return {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Version": GHL_VERSION,
        "LocationId": GHL_LOCATION_ID,   # <-- THIS is the fix
    }

async def ghl_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = GHL_BASE_URL.rstrip("/") + path
    r = await _ghl_client.get(url, headers=_ghl_headers(), params=params or {})
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GHL GET {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()

async def ghl_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = GHL_BASE_URL.rstrip("/") + path
    r = await _ghl_client.post(url, headers=_ghl_headers(), json=payload)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GHL POST {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()


async def ghl_put(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = GHL_BASE_URL.rstrip("/") + path
    r = await _ghl_client.put(url, headers=_ghl_headers(), json=payload)
    if r.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"GHL PUT {path} failed: {r.status_code} {r.text[:300]}")
    return r.json() if (r.text or "").strip() else {}

async def ghl_list_messages(conversation_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Verified response shape: {'messages': [...], 'traceId': ...}
    (Your container probe showed top keys: ['messages','traceId'])
    """
    data = await ghl_get(f"/conversations/{conversation_id}/messages", params={"limit": limit})

    if isinstance(data, dict):
        msgs = data.get("messages")
        if isinstance(msgs, list):
            return msgs
        # fallback older shapes
        if isinstance(msgs, dict) and isinstance(msgs.get("messages"), list):
            return msgs["messages"]
        if isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data.get("data"), dict) and isinstance(data["data"].get("messages"), list):
            return data["data"]["messages"]
    if isinstance(data, list):
        return data
    return []

async def ghl_send_message(conversation_id: str, contact_id: str, message_text: str) -> Dict[str, Any]:
    """
    LOCKED (verified):
      POST /conversations/messages
      payload:
        type: "SMS"
        message: "<text>"
        conversationId: "<id>"
        contactId: "<id>"
    """
    payload = {
        "type": "SMS",
        "message": message_text,
        "conversationId": conversation_id,
        "contactId": contact_id,
    }
    return await ghl_post("/conversations/messages", payload)


async def ghl_get_contact_name(contact_id: Optional[str]) -> Optional[str]:
    """Best-effort contact name lookup via GHL Contacts API."""
    if not contact_id:
        return None
    try:
        data = await ghl_get(f"/contacts/{contact_id}")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    c = data.get("contact") if isinstance(data.get("contact"), dict) else data
    if isinstance(c, dict):
        # Try standard fields first
        for k in ("name", "fullName", "contactName"):
            v = c.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        
        # Fallback to firstName + lastName
        first = (c.get("firstName") or "").strip()
        last = (c.get("lastName") or "").strip()
        if first or last:
            name = f"{first} {last}".strip()
            return name if name else None
    
    return None

async def ghl_find_conversation_id_for_contact(contact_id: Optional[str], phone: Optional[str]) -> Optional[str]:
    """
    Deterministic: call conversations/search and return the newest conversation id.
    Prefers contact_id; falls back to phone if contact_id missing.

    NOTE: response shape can vary; we normalize common shapes.
    """
    params: Dict[str, Any] = {}
    if contact_id:
        params["contactId"] = contact_id
    elif phone:
        params["phone"] = phone
    else:
        return None

    data = await ghl_get("/conversations/search", params=params)

    if isinstance(data, dict):
        for key in ("conversations", "data", "items"):
            if key in data and isinstance(data[key], list) and data[key]:
                c = data[key][0]
                if isinstance(c, dict):
                    for k in ("id", "conversationId"):
                        v = c.get(k)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
    return None


async def ghl_fetch_all_contacts(page_limit: int = CUSTOMER_SYNC_PAGE_LIMIT) -> List[Dict[str, Any]]:
    contacts: List[Dict[str, Any]] = []
    search_after: Optional[List[Any]] = None

    while True:
        payload: Dict[str, Any] = {
            "locationId": GHL_LOCATION_ID,
            "pageLimit": page_limit,
        }
        if search_after:
            payload["searchAfter"] = search_after

        data = await ghl_post("/contacts/search", payload)
        page = data.get("contacts") if isinstance(data, dict) else None
        if not isinstance(page, list) or not page:
            break

        contacts.extend([contact for contact in page if isinstance(contact, dict)])
        last = page[-1] if isinstance(page[-1], dict) else {}
        search_after = last.get("searchAfter")
        if len(page) < page_limit or not search_after:
            break

    return contacts


async def ghl_update_contact(contact_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return await ghl_put(f"/contacts/{contact_id}", payload)


async def ghl_create_contact(payload: Dict[str, Any]) -> Dict[str, Any]:
    create_payload = dict(payload)
    create_payload["locationId"] = GHL_LOCATION_ID
    return await ghl_post("/contacts/", create_payload)


def _ghl_contact_id_from_response(data: Dict[str, Any]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    for key in ("id", "contactId"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    contact = data.get("contact")
    if isinstance(contact, dict):
        for key in ("id", "contactId"):
            value = contact.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# ==========================
# Payload extraction helpers
# ==========================
def _is_internal_sender(contact_type: Optional[str], contact_id: Optional[str]) -> bool:
    return _sms_is_internal_sender(contact_type, contact_id, INTERNAL_CONTACT_IDS)

def _truthy_value(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v) != 0.0
    if isinstance(v, str):
        t = v.strip().lower()
        return t in ("1", "true", "yes", "on", "missed", "no-answer", "no answer", "busy", "canceled", "cancelled")
    return False

def _find_key_values(payload: Any, key: str) -> List[Any]:
    out: List[Any] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            if str(k) == key:
                out.append(v)
            out.extend(_find_key_values(v, key))
    elif isinstance(payload, list):
        for item in payload:
            out.extend(_find_key_values(item, key))
    return out

def _has_missed_call_marker(payload: Dict[str, Any]) -> bool:
    if not CALL_MISSED_MARKER_KEYS:
        return False
    for key in CALL_MISSED_MARKER_KEYS:
        vals = _find_key_values(payload, key)
        for v in vals:
            if _truthy_value(v):
                return True
    return False


# ==========================
# Spam helper
# ==========================
def _is_spam(conn: sqlite3.Connection, phone: Optional[str]) -> bool:
    if not phone:
        return False
    row = conn.execute("SELECT 1 FROM spam_phones WHERE phone = ?", (phone,)).fetchone()
    return row is not None

def mark_spam(phone: str) -> None:
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO spam_phones (phone, created_ts) VALUES (?, ?)",
        (phone, _now_local().isoformat())
    )
    conn.commit()
    conn.close()


def _set_resolved_metadata(issue_id: int, resolved_by: str, extra: Optional[Dict[str, Any]] = None) -> None:
    issue_set_resolved_metadata(issue_id, resolved_by, _now_local, extra)


def _set_resolved_metadata_conn(
    conn: sqlite3.Connection,
    issue_id: int,
    resolved_by: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    row = conn.execute("SELECT meta FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        return
    try:
        meta = json.loads(row["meta"] or "{}")
    except Exception:
        meta = {}
    meta.update(
        {
            "resolved_by": resolved_by,
            "resolved_meta_ts": _now_local().isoformat(),
        }
    )
    if extra:
        meta.update(extra)
    conn.execute("UPDATE issues SET meta=? WHERE id=?", (json.dumps(meta), issue_id))


# ==========================
# Raw event ingestion
# ==========================
async def _parse_request_payload(request: Request) -> Dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    raw_body = await request.body()
    payload: Dict[str, Any] = {
        "_meta": {
            "content_type": content_type,
            "content_length": len(raw_body),
        }
    }

    if "application/json" in content_type and raw_body.strip():
        try:
            payload.update(json.loads(raw_body.decode("utf-8")))
        except Exception as e:
            payload["_meta"]["json_error"] = str(e)
            payload["_raw"] = raw_body.decode("utf-8", errors="replace")
    else:
        try:
            form = await request.form()
            payload.update(dict(form))
        except Exception as e:
            payload["_meta"]["form_error"] = str(e)
            payload["_raw"] = raw_body.decode("utf-8", errors="replace")
    return payload

def _log_raw_event(source: str, payload: Dict[str, Any]) -> None:
    """Schedule the SQLite write as a background task so it doesn't block the event loop."""
    payload_json = json.dumps(payload)
    ts = dt.datetime.now(dt.timezone.utc).isoformat()

    async def _task():
        try:
            await asyncio.to_thread(_log_raw_event_sync, source, ts, payload_json)
        except Exception:
            pass

    asyncio.create_task(_task())


def _log_raw_event_sync(source: str, ts: str, payload_json: str) -> None:
    conn = db()
    conn.execute(
        "INSERT INTO raw_events (received_ts, source, payload) VALUES (?, ?, ?)",
        (ts, source, payload_json),
    )
    conn.commit()
    conn.close()

def _flow_who(contact_name: Optional[str], phone: Optional[str], contact_id: Optional[str]) -> str:
    if isinstance(contact_name, str) and contact_name.strip():
        return contact_name.strip()
    if isinstance(phone, str) and phone.strip():
        p = phone.strip()
        if p.startswith("+1") and len(p) >= 12:
            return "+1***" + p[-4:]
        if len(p) >= 4:
            return "***" + p[-4:]
        return p
    if isinstance(contact_id, str) and contact_id.strip():
        return f"contact:{contact_id.strip()}"
    return "unknown"

def _flow_log(event: str, **fields: Any) -> None:
    if not FLOW_LOG_ENABLED:
        return
    payload = {
        "ts": dt.datetime.now(tz=ZoneInfo(TZ_NAME)).isoformat(),
        "event": event,
    }
    for k, v in fields.items():
        if v is not None:
            payload[k] = v
    print("FLOW " + json.dumps(payload, separators=(",", ":"), ensure_ascii=True))


def get_issue_by_id(issue_id: int) -> Optional[sqlite3.Row]:
    return issue_get_issue_by_id(issue_id)

def resolve_by_id(issue_id: int, status: str = "RESOLVED") -> int:
    changed = issue_resolve_by_id(issue_id, status, _now_local)
    if changed > 0 and status == "RESOLVED":
        _set_resolved_metadata(issue_id, "MANUAL_COMMAND_ID")
    return changed

def add_note(issue_id: int, note: str) -> bool:
    return issue_add_note(issue_id, note, _now_local)


# ---- Manager LIST paging state (in-memory) ----
_MANAGER_LIST_OFFSETS: dict[str, int] = {}

def list_open_issues(limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    return issue_list_open_issues(limit, offset)

def _render_list_like_summary(rows: list[dict], total_open: int, offset: int, limit: int) -> str:
    return render_list_like_summary(rows, total_open, offset, limit)

def _set_issue_contact_name(issue_id: int, name: str) -> None:
    set_issue_contact_name(issue_id, name)

def resolve_by_phone(phone: str, status: str = "RESOLVED") -> int:
    changed, ids = issue_resolve_by_phone(phone, status, _now_local)
    if status == "RESOLVED":
        for iid in ids:
            _set_resolved_metadata(iid, "MANUAL_COMMAND_PHONE", {"resolve_target": phone})
    return changed

def resolve_by_contact_id(contact_id: str, status: str = "RESOLVED") -> int:
    changed, ids = issue_resolve_by_contact_id(contact_id, status, _now_local)
    if status == "RESOLVED":
        for iid in ids:
            _set_resolved_metadata(iid, "MANUAL_COMMAND_CONTACT_ID", {"resolve_target": contact_id})
    return changed

def resolve_by_name(name: str, status: str = "RESOLVED") -> int:
    matched_ids = issue_resolve_by_name(name, status, _now_local)
    if status == "RESOLVED":
        for iid in matched_ids:
            _set_resolved_metadata(iid, "MANUAL_COMMAND_NAME", {"resolve_target": name})
    return len(matched_ids)

def _looks_like_contact_id(s: str) -> bool:
    return looks_like_contact_id(s)

def resolve_target(target: str) -> int:
    token = target.strip()
    phone = _normalize_phone(token)
    if phone:
        return resolve_by_phone(phone)
    if _looks_like_contact_id(token):
        return resolve_by_contact_id(token)
    return resolve_by_name(token)


# ==========================
# Webhook helpers
def _webhook_find_recent_call_issue(
    conversation_id: Optional[str],
    contact_id: Optional[str],
    phone: Optional[str],
) -> Optional[sqlite3.Row]:
    conn = db()
    latest_call = None
    if conversation_id:
        latest_call = conn.execute(
            """
            SELECT id, created_ts, status
            FROM issues
            WHERE issue_type='CALL'
              AND conversation_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
    elif contact_id:
        latest_call = conn.execute(
            """
            SELECT id, created_ts, status
            FROM issues
            WHERE issue_type='CALL'
              AND contact_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (contact_id,),
        ).fetchone()
    elif phone:
        latest_call = conn.execute(
            """
            SELECT id, created_ts, status
            FROM issues
            WHERE issue_type='CALL'
              AND phone=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (phone,),
        ).fetchone()
    conn.close()
    return latest_call


def _webhook_is_spam(phone: Optional[str]) -> bool:
    if not phone:
        return False
    conn = db()
    out = _is_spam(conn, phone)
    conn.close()
    return out


def _webhook_create_call_issue(
    contact_id: Optional[str],
    from_phone: Optional[str],
    contact_name: Optional[str],
    conversation_id: Optional[str],
    created_ts: str,
    due_ts: str,
) -> int:
    meta = {"source": "voicemail_route=tech_sentinel"}
    if contact_name:
        meta["contact_name"] = contact_name

    conn = db()
    cur = conn.execute(
        """
        INSERT INTO issues (
            issue_type, contact_id, phone, contact_name, created_ts, due_ts, status, meta,
            conversation_id, first_inbound_ts, last_inbound_ts, inbound_count, outbound_count
        )
        VALUES ('CALL', ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, 1, 0)
        """,
        (
            contact_id,
            from_phone,
            contact_name or None,
            created_ts,
            due_ts,
            json.dumps(meta),
            conversation_id,
            created_ts,
            created_ts,
        ),
    )
    conn.commit()
    issue_id = cur.lastrowid
    conn.close()
    return issue_id


# ==========================
# Polling resolver (SMS)
# ==========================
def _msg_ts(m: Dict[str, Any]) -> Optional[dt.datetime]:
    v = m.get("dateAdded")
    if isinstance(v, str) and v:
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return None

def _msg_direction(m: Dict[str, Any]) -> str:
    v = m.get("direction")
    if isinstance(v, str) and v:
        return v.lower()
    return ""

def _msg_text(m: Dict[str, Any]) -> str:
    if not isinstance(m, dict):
        return ""
    for k in ("body", "message", "text", "content"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def _internal_user_ids() -> set:
    raw = os.getenv("INTERNAL_USER_IDS", "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}

def _msg_is_staff_outbound(m: Dict[str, Any]) -> bool:
    """
    Returns True only for a real staff reply:
      - direction == outbound
      - userId is present (excludes workflow automation which has no userId)
      - userId is in INTERNAL_USER_IDS allowlist
    Strict mode only: INTERNAL_USER_IDS must be configured for any auto-resolve.
    """
    if not isinstance(m, dict):
        return False
    if _msg_direction(m) != "outbound":
        return False
    uid = m.get("userId")
    if not uid:
        return False
    allow = _internal_user_ids()
    if not allow:
        return False
    return uid in allow

def _msg_is_call_resolution_outbound(m: Dict[str, Any]) -> bool:
    """
    CALL issue resolution signal:
      - any outbound activity in the conversation counts as a follow-up
    This intentionally includes outbound call log entries that may not carry userId.
    """
    if not isinstance(m, dict):
        return False
    return _msg_direction(m) == "outbound"


def _normalize_status_token(value: Any) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "", s)
    return s


def _msg_call_status_tokens(m: Dict[str, Any]) -> List[str]:
    if not isinstance(m, dict):
        return []

    keys = ("callStatus", "call_status", "status", "callDisposition", "disposition")
    containers = ("meta", "metadata", "call", "customData", "extra")

    out: List[str] = []

    def _collect(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        for k in keys:
            tok = _normalize_status_token(obj.get(k))
            if tok:
                out.append(tok)

    _collect(m)
    for c in containers:
        _collect(m.get(c))

    deduped: List[str] = []
    seen = set()
    for tok in out:
        if tok not in seen:
            deduped.append(tok)
            seen.add(tok)
    return deduped


def _status_matches(token: str, candidates: List[str]) -> bool:
    if token in candidates:
        return True
    return any(token.startswith(f"{c}-") for c in candidates)


def _msg_call_resolution_signal(m: Dict[str, Any]) -> Optional[str]:
    """
    Returns the resolution signal for CALL issues, if present.

    Signals:
      - "outbound": legacy behavior, any outbound activity
      - "call_status_positive": explicit answered/completed call status
    """
    if not isinstance(m, dict):
        return None

    if _msg_is_call_resolution_outbound(m):
        return "outbound"

    tokens = _msg_call_status_tokens(m)
    if not tokens:
        return None

    has_negative = any(_status_matches(t, CALL_RESOLVE_NEGATIVE_STATUSES) for t in tokens)
    if has_negative:
        return None

    has_positive = any(_status_matches(t, CALL_RESOLVE_POSITIVE_STATUSES) for t in tokens)
    if has_positive:
        return "call_status_positive"

    return None


async def _recent_staff_outbound_ts(conversation_id: str) -> Optional[dt.datetime]:
    try:
        msgs = await ghl_list_messages(conversation_id, limit=30)
    except Exception:
        return None

    latest_staff_ts: Optional[dt.datetime] = None
    latest_staff_uid: Optional[str] = None
    for m in msgs:
        if not _msg_is_staff_outbound(m):
            continue
        mts = _msg_ts(m)
        if mts is None:
            continue
        if latest_staff_ts is None or mts > latest_staff_ts:
            latest_staff_ts = mts
            latest_staff_uid = str(m.get("userId") or "")

    if latest_staff_ts is not None:
        try:
            set_last_internal_outbound(
                conversation_id,
                latest_staff_ts.astimezone(ZoneInfo(TZ_NAME)).isoformat(),
                latest_staff_uid or None,
            )
        except Exception:
            pass

    return latest_staff_ts


def _set_issue_status(issue_id: int, status: str) -> None:
    conn = db()
    conn.execute("UPDATE issues SET status=? WHERE id=?", (status, issue_id))
    conn.commit()
    conn.close()


def _has_outbound_after(msgs: List[Dict[str, Any]], first_inbound_ts: str) -> bool:
    cutoff = _parse_iso_dt(first_inbound_ts)
    if not cutoff:
        return False

    for m in msgs or []:
        direction = _msg_direction(m)
        if direction != "outbound":
            continue
        mts = _msg_ts(m)
        if not mts:
            continue
        try:
            cutoff_utc = cutoff.astimezone(dt.timezone.utc) if cutoff.tzinfo else cutoff.replace(
                tzinfo=ZoneInfo(TZ_NAME)
            ).astimezone(dt.timezone.utc)
            if mts.astimezone(dt.timezone.utc) > cutoff_utc:
                return True
        except Exception:
            continue

    return False


async def _poll_resolver_fetch_messages(
    issue_rows: List[sqlite3.Row],
    *,
    fetch_limit: int,
) -> Dict[str, List[Dict[str, Any]]]:
    sem = asyncio.Semaphore(POLL_RESOLVER_CONCURRENCY)

    async def _fetch(conversation_id: str) -> Tuple[str, List[Dict[str, Any]]]:
        async with sem:
            msgs = await ghl_list_messages(conversation_id, limit=fetch_limit)
            return conversation_id, msgs

    unique_conversation_ids = []
    seen = set()
    for row in issue_rows:
        conv_id = row["conversation_id"]
        if not conv_id or conv_id in seen:
            continue
        seen.add(conv_id)
        unique_conversation_ids.append(conv_id)

    if not unique_conversation_ids:
        return {}

    results = await asyncio.gather(
        *[_fetch(conv_id) for conv_id in unique_conversation_ids],
        return_exceptions=True,
    )

    out: Dict[str, List[Dict[str, Any]]] = {}
    for result in results:
        if isinstance(result, Exception):
            continue
        conv_id, msgs = result
        out[conv_id] = msgs
    return out

@app.post("/jobs/poll_resolver")
async def poll_resolver(request: Request, limit: int = 200):
    """
        For each active SMS issue:
      Fetch messages for conversation_id
      Resolve if ANY outbound where dateAdded > first_inbound_ts

        For each active CALL issue:
      Fetch messages for conversation_id
      Resolve if ANY staff outbound where dateAdded > created_ts
    """
    _auth_or_401(request)
    started_at = time.perf_counter()

    conn = db()
    try:
        rows = conn.execute("""
            SELECT id, conversation_id, first_inbound_ts, outbound_count
            FROM issues
            WHERE status IN ('OPEN','PENDING')
              AND issue_type='SMS'
              AND conversation_id IS NOT NULL
            ORDER BY due_ts ASC
            LIMIT ?
        """, (limit,)).fetchall()
        call_rows = conn.execute("""
            SELECT id, conversation_id, created_ts, outbound_count
            FROM issues
            WHERE status IN ('OPEN','PENDING')
              AND issue_type='CALL'
              AND conversation_id IS NOT NULL
            ORDER BY due_ts ASC
            LIMIT ?
        """, (limit,)).fetchall()
    finally:
        conn.close()

    checked = 0
    resolved = 0
    updated_counts = 0
    call_checked = 0
    call_resolved = 0
    call_updated_counts = 0
    fetch_started_at = time.perf_counter()
    sms_messages_by_conversation = await _poll_resolver_fetch_messages(rows, fetch_limit=50)
    call_messages_by_conversation = await _poll_resolver_fetch_messages(call_rows, fetch_limit=50)
    fetch_elapsed_ms = round((time.perf_counter() - fetch_started_at) * 1000, 1)

    update_conn = db()
    try:
        for r in rows:
            checked += 1
            issue_id = r["id"]
            conv_id = r["conversation_id"]
            if not conv_id:
                continue

            msgs = sms_messages_by_conversation.get(conv_id)
            if msgs is None:
                continue

            try:
                fi = dt.datetime.fromisoformat((r["first_inbound_ts"] or "").replace("Z", "+00:00"))
            except Exception:
                fi = None

            outbound_after = False
            out_count = 0
            latest_staff_ts: Optional[dt.datetime] = None
            latest_staff_uid: Optional[str] = None
            latest_customer_inbound_ts: Optional[dt.datetime] = None
            latest_customer_inbound_text: str = ""

            for m in msgs:
                if _msg_is_staff_outbound(m):
                    out_count += 1

                    mts0 = _msg_ts(m)
                    if mts0 is not None:
                        if latest_staff_ts is None or mts0 > latest_staff_ts:
                            latest_staff_ts = mts0
                            latest_staff_uid = str(m.get("userId") or "")

                    if fi is not None:
                        if mts0 is None:
                            continue
                        try:
                            # compare as UTC
                            fi_utc = fi.astimezone(dt.timezone.utc) if fi.tzinfo else fi.replace(tzinfo=ZoneInfo(TZ_NAME)).astimezone(dt.timezone.utc)
                            if mts0.astimezone(dt.timezone.utc) > fi_utc:
                                outbound_after = True
                        except Exception:
                            pass

            if latest_staff_ts is not None:
                try:
                    _set_last_internal_outbound_conn(
                        update_conn,
                        conv_id,
                        latest_staff_ts.astimezone(ZoneInfo(TZ_NAME)).isoformat(),
                        latest_staff_uid or None,
                    )
                except Exception:
                    pass

            prev_out = r["outbound_count"] if r["outbound_count"] is not None else 0
            if out_count != prev_out:
                update_conn.execute("UPDATE issues SET outbound_count=? WHERE id=?", (out_count, issue_id))
                updated_counts += 1

            if outbound_after:
                now = _now_local().isoformat()
                cur = update_conn.execute("""
                    UPDATE issues
                    SET status='RESOLVED', resolved_ts=?
                    WHERE id=? AND status IN ('OPEN','PENDING')
                """, (now, issue_id))
                if cur.rowcount and cur.rowcount > 0:
                    resolved += 1
                    _set_resolved_metadata_conn(update_conn, issue_id, "RULE_POLL_RESOLVER_SMS_OUTBOUND")
                    _flow_log(
                        "sms.auto_resolved",
                        issue_id=issue_id,
                        conversation_id=conv_id,
                        via="poll_resolver",
                    )

        update_conn.commit()

        for r in call_rows:
            call_checked += 1
            issue_id = r["id"]
            conv_id = r["conversation_id"]
            if not conv_id:
                continue

            msgs = call_messages_by_conversation.get(conv_id)
            if msgs is None:
                _flow_log(
                    "call.resolver_fetch_error",
                    issue_id=issue_id,
                    conversation_id=conv_id,
                    status_code=None,
                )
                continue

            created = _parse_iso_dt(r["created_ts"])
            created_utc: Optional[dt.datetime] = None
            cutoff_utc: Optional[dt.datetime] = None
            if created is not None:
                try:
                    created_utc = created.astimezone(dt.timezone.utc) if created.tzinfo else created.replace(
                        tzinfo=ZoneInfo(TZ_NAME)
                    ).astimezone(dt.timezone.utc)
                    cutoff_utc = created_utc - dt.timedelta(minutes=max(0.0, CALL_RESOLVE_LOOKBACK_MINUTES))
                except Exception:
                    created_utc = None
                    cutoff_utc = None

            call_resolution_after = False
            resolution_signal: Optional[str] = None
            out_count = 0
            latest_staff_ts: Optional[dt.datetime] = None
            latest_staff_uid: Optional[str] = None

            for m in msgs:
                signal = _msg_call_resolution_signal(m)

                if _msg_is_call_resolution_outbound(m):
                    out_count += 1
                    mts0 = _msg_ts(m)
                    if mts0 is not None:
                        if latest_staff_ts is None or mts0 > latest_staff_ts:
                            latest_staff_ts = mts0
                            latest_staff_uid = str(m.get("userId") or "")

                if signal is None or cutoff_utc is None:
                    continue

                mts = _msg_ts(m)
                if mts is None:
                    continue

                try:
                    if mts.astimezone(dt.timezone.utc) > cutoff_utc:
                        call_resolution_after = True
                        if resolution_signal is None:
                            resolution_signal = signal
                except Exception:
                    pass

            if latest_staff_ts is not None:
                try:
                    _set_last_internal_outbound_conn(
                        update_conn,
                        conv_id,
                        latest_staff_ts.astimezone(ZoneInfo(TZ_NAME)).isoformat(),
                        latest_staff_uid or None,
                    )
                except Exception:
                    pass

            prev_out = r["outbound_count"] if r["outbound_count"] is not None else 0
            if out_count != prev_out:
                update_conn.execute("UPDATE issues SET outbound_count=? WHERE id=?", (out_count, issue_id))
                call_updated_counts += 1

            if call_resolution_after:
                now = _now_local().isoformat()
                cur = update_conn.execute("""
                    UPDATE issues
                    SET status='RESOLVED', resolved_ts=?
                    WHERE id=? AND status IN ('OPEN','PENDING')
                """, (now, issue_id))
                if cur.rowcount and cur.rowcount > 0:
                    call_resolved += 1
                    resolved_by = (
                        "RULE_POLL_RESOLVER_CALL_OUTBOUND"
                        if resolution_signal == "outbound"
                        else "RULE_POLL_RESOLVER_CALL_ACTIVITY"
                    )
                    _set_resolved_metadata_conn(
                        update_conn,
                        issue_id,
                        resolved_by,
                        {"resolution_signal": resolution_signal or "unknown"},
                    )
                    _flow_log(
                        "call.auto_resolved",
                        issue_id=issue_id,
                        conversation_id=conv_id,
                        via="poll_resolver",
                        signal=resolution_signal or "unknown",
                    )

        update_conn.commit()

    finally:
        update_conn.close()

    total_elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
    _flow_log(
        "poll_resolver.completed",
        limit=limit,
        sms_issue_rows=len(rows),
        call_issue_rows=len(call_rows),
        sms_conversations_fetched=len(sms_messages_by_conversation),
        call_conversations_fetched=len(call_messages_by_conversation),
        fetch_elapsed_ms=fetch_elapsed_ms,
        total_elapsed_ms=total_elapsed_ms,
        checked=checked,
        resolved=resolved,
        updated_counts=updated_counts,
        call_checked=call_checked,
        call_resolved=call_resolved,
        call_updated_counts=call_updated_counts,
        concurrency=POLL_RESOLVER_CONCURRENCY,
    )

    return {
        "job": "poll_resolver",
        "checked": checked,
        "resolved": resolved,
        "updated_counts": updated_counts,
        "call_checked": call_checked,
        "call_resolved": call_resolved,
        "call_updated_counts": call_updated_counts,
    }



# ==========================


def _ai_gate_config() -> AIGateConfig:
    return AIGateConfig(
        enabled=AI_GATE_ENABLED,
        openai_api_key=OPENAI_API_KEY,
        openai_base_url=OPENAI_BASE_URL,
        model=AI_GATE_MODEL,
        gap_hours=AI_GATE_GAP_HOURS,
        max_messages=AI_GATE_MAX_MESSAGES,
        timeout_seconds=AI_GATE_TIMEOUT_SECONDS,
        max_output_tokens=AI_GATE_MAX_OUTPUT_TOKENS,
        on_every_inbound=AI_GATE_ON_EVERY_INBOUND,
        decision_mode=DECISION_MODE,
        on_every_inbound_no_confidence=AI_GATE_ON_EVERY_INBOUND_NO_CONFIDENCE,
        primary_suppress_no_confidence=AI_PRIMARY_SUPPRESS_NO_CONFIDENCE,
        now_local=_now_local,
        msg_is_staff_outbound=_msg_is_staff_outbound,
        msg_ts=_msg_ts,
        msg_text=_msg_text,
        suppress_no_confidence=AI_GATE_SUPPRESS_NO_CONFIDENCE,
        redact_pii=AI_GATE_REDACT_PII,
    )

async def ai_gate_classify(conversation_id: str, msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return await _ai_gate_classify(conversation_id, msgs, _ai_gate_config())

async def _ai_inbound_should_suppress(
    conversation_id: Optional[str],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    return await _ai_inbound_should_suppress_impl(
        conversation_id,
        _ai_gate_config(),
        ghl_list_messages,
    )

@app.post("/jobs/verify_pending")
async def verify_pending(request: Request, limit: int = 200):
    _auth_or_401(request)

    # Legacy compatibility endpoint.
    # Current runtime behavior delegates to poll_resolver and preserves the
    # historical response shape, including promotion counters that now remain 0.
    try:
        poll_result = await poll_resolver(request, limit=limit)
        if not isinstance(poll_result, dict):
            poll_result = {}
    except Exception:
        poll_result = {}
        
    return {
        "job": "verify_pending",
        "checked": int(poll_result.get("checked", 0)),
        "promoted_open": 0,
        "auto_resolved": int(poll_result.get("resolved", 0)),
        "updated_counts": int(poll_result.get("updated_counts", 0)),
        "errors": 0,
        "call_checked": int(poll_result.get("call_checked", 0)),
        "call_promoted_open": 0,
        "call_auto_resolved": int(poll_result.get("call_resolved", 0)),
        "call_updated_counts": int(poll_result.get("call_updated_counts", 0)),
        "ai_checked": 0,
        "ai_suppressed": 0,
        "ai_skipped_budget": 0,
    }
@app.post("/jobs/cleanup_raw_events")
async def cleanup_raw_events(request: Request, days: Optional[int] = None, source: Optional[str] = None, dry_run: int = 1):
    """
    Retention cleanup for raw webhook events.
    Default is dry-run to prevent accidental data loss.
    """
    _auth_or_401(request)
    keep_days = int(days if days is not None else RAW_EVENTS_RETENTION_DAYS)
    result = purge_raw_events(
        retention_days=keep_days,
        source=(source.strip() if isinstance(source, str) and source.strip() else None),
        dry_run=bool(dry_run),
    )
    return {
        "job": "cleanup_raw_events",
        "retention_days": keep_days,
        "source": source if source else None,
        "dry_run": bool(dry_run),
        "eligible": int(result.get("eligible", 0)),
        "deleted": int(result.get("deleted", 0)),
    }


@app.post("/jobs/skimmer_link")
async def skimmer_link(request: Request):
    _auth_or_401(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    link = None
    if isinstance(payload, dict):
        link = payload.get("skimmer_url") or payload.get("skimmer_link") or payload.get("skimmer_file_id")
    if not link or not isinstance(link, str):
        raise HTTPException(
            status_code=400,
            detail="Expected JSON body with skimmer_url, skimmer_link, or skimmer_file_id",
        )
    link = link.strip()
    if not link:
        raise HTTPException(status_code=400, detail="Expected non-empty link")
    if not re.fullmatch(r"https?://.+", link) and not re.fullmatch(r"[A-Za-z0-9_-]+", link):
        raise HTTPException(
            status_code=400,
            detail="skimmer_url must be a valid URL or Google Drive file ID",
        )

    if link.lower().startswith("http://") or link.lower().startswith("https://"):
        try:
            _download_and_save_skimmer(link, SKIMMER_DB_PATH)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Skimmer download failed: {exc}")
        import_result = _run_skimmer_import()
        return {
            "status": "downloaded",
            "skimmer_db_path": SKIMMER_DB_PATH,
            "download_url": link,
            "import": import_result,
        }

    path = _write_skimmer_link(link)
    return {
        "status": "stored",
        "link_file": path,
        "stored_value": link,
    }


def _run_skimmer_import() -> dict:
    tables = os.getenv("SKIMMER_IMPORT_TABLES", "all")
    script = os.path.join(os.path.dirname(__file__), "scripts", "import_skimmer_customers.py")
    script = os.path.normpath(script)
    result = subprocess.run(
        [sys.executable, script, "--tables", tables, "--sqlite", SKIMMER_DB_PATH],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        return {"status": "error", "detail": result.stderr.strip() or result.stdout.strip()}
    return {"status": "ok", "output": result.stdout.strip()}


@app.post("/jobs/skimmer_import")
async def skimmer_import_job(request: Request):
    _auth_or_401(request)
    if not os.path.exists(SKIMMER_DB_PATH):
        raise HTTPException(status_code=422, detail=f"Skimmer DB not found at {SKIMMER_DB_PATH}")
    result = _run_skimmer_import()
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("detail", "Import failed"))
    return result


def _fetch_sk_customers(limit: int) -> List[Dict[str, Any]]:
    if not DATABASE_URL:
        raise HTTPException(status_code=500, detail="DATABASE_URL is not configured")
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    source_customer_id,
                    first_name,
                    last_name,
                    company_name,
                    email,
                    phone,
                    mobile_phone,
                    is_inactive,
                    is_lead,
                    customer_status,
                    ghl_contact_id
                FROM sk_customer
                WHERE source_system = 'skimmer'
                ORDER BY id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return rows if isinstance(rows, list) else []


def _update_sk_customer_sync_state(
    sk_customer_id: int,
    *,
    ghl_contact_id: Optional[str],
    error: Optional[str],
) -> None:
    if not DATABASE_URL:
        return
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE sk_customer
                SET
                    ghl_contact_id = COALESCE(%s, ghl_contact_id),
                    ghl_last_sync_ts = NOW(),
                    ghl_last_sync_error = %s
                WHERE id = %s
                """,
                (ghl_contact_id, error, sk_customer_id),
            )
        conn.commit()


@app.post("/jobs/skimmer_customer_sync")
async def skimmer_customer_sync(request: Request, limit: int = 1000, dry_run: int = 1):
    _auth_or_401(request)

    started_at = time.perf_counter()
    customers = [build_customer_sync_record(row) for row in _fetch_sk_customers(limit=max(1, limit))]
    contacts = await ghl_fetch_all_contacts()
    indexes = build_ghl_contact_indexes(contacts)

    summary = {
        "job": "skimmer_customer_sync",
        "dry_run": bool(dry_run),
        "customers_considered": len(customers),
        "ghl_contacts_loaded": len(contacts),
        "matched": 0,
        "updated": 0,
        "created": 0,
        "skipped_protected": 0,
        "skipped_ambiguous": 0,
        "skipped_no_identifiers": 0,
        "errors": 0,
        "items": [],
    }

    for customer in customers:
        sk_customer_id = int(customer["id"])
        payload = build_ghl_contact_payload(customer)
        if not any([payload.get("email"), payload.get("phone"), payload.get("name"), payload.get("companyName")]):
            summary["skipped_no_identifiers"] += 1
            summary["items"].append(
                {
                    "sk_customer_id": sk_customer_id,
                    "customer": customer_display_name(customer),
                    "action": "skip_no_identifiers",
                }
            )
            continue

        matched_contact, methods, ambiguous = match_ghl_contact(customer, indexes)
        if ambiguous:
            summary["skipped_ambiguous"] += 1
            summary["items"].append(
                {
                    "sk_customer_id": sk_customer_id,
                    "customer": customer_display_name(customer),
                    "action": "skip_ambiguous",
                    "match_methods": methods,
                }
            )
            continue

        if matched_contact is not None:
            summary["matched"] += 1
            current_type = sync_clean_text(matched_contact.get("type"))
            if current_type in PROTECTED_GHL_TYPES:
                summary["skipped_protected"] += 1
                summary["items"].append(
                    {
                        "sk_customer_id": sk_customer_id,
                        "customer": customer_display_name(customer),
                        "action": "skip_protected_type",
                        "ghl_contact_id": sync_clean_text(matched_contact.get("id")),
                        "ghl_type": current_type,
                    }
                )
                continue

            needs_update = contact_needs_update(matched_contact, payload)
            contact_id = sync_clean_text(matched_contact.get("id"))
            if not needs_update:
                if not dry_run:
                    _update_sk_customer_sync_state(sk_customer_id, ghl_contact_id=contact_id, error=None)
                summary["items"].append(
                    {
                        "sk_customer_id": sk_customer_id,
                        "customer": customer_display_name(customer),
                        "action": "no_change",
                        "ghl_contact_id": contact_id,
                        "match_methods": methods,
                    }
                )
                continue

            if dry_run:
                summary["updated"] += 1
                summary["items"].append(
                    {
                        "sk_customer_id": sk_customer_id,
                        "customer": customer_display_name(customer),
                        "action": "would_update",
                        "ghl_contact_id": contact_id,
                        "match_methods": methods,
                        "payload": payload,
                    }
                )
                continue

            try:
                await ghl_update_contact(contact_id, payload)
                _update_sk_customer_sync_state(sk_customer_id, ghl_contact_id=contact_id, error=None)
                summary["updated"] += 1
                summary["items"].append(
                    {
                        "sk_customer_id": sk_customer_id,
                        "customer": customer_display_name(customer),
                        "action": "updated",
                        "ghl_contact_id": contact_id,
                        "match_methods": methods,
                    }
                )
            except Exception as exc:
                _update_sk_customer_sync_state(sk_customer_id, ghl_contact_id=contact_id or None, error=str(exc)[:500])
                summary["errors"] += 1
                summary["items"].append(
                    {
                        "sk_customer_id": sk_customer_id,
                        "customer": customer_display_name(customer),
                        "action": "error_update",
                        "ghl_contact_id": contact_id,
                        "error": str(exc),
                    }
                )
            if CUSTOMER_SYNC_DELAY_SECONDS > 0:
                await asyncio.sleep(CUSTOMER_SYNC_DELAY_SECONDS)
            continue

        if dry_run:
            summary["created"] += 1
            summary["items"].append(
                {
                    "sk_customer_id": sk_customer_id,
                    "customer": customer_display_name(customer),
                    "action": "would_create",
                    "payload": payload,
                }
            )
            continue

        try:
            data = await ghl_create_contact(payload)
            contact_id = _ghl_contact_id_from_response(data)
            _update_sk_customer_sync_state(sk_customer_id, ghl_contact_id=contact_id, error=None)
            summary["created"] += 1
            summary["items"].append(
                {
                    "sk_customer_id": sk_customer_id,
                    "customer": customer_display_name(customer),
                    "action": "created",
                    "ghl_contact_id": contact_id,
                }
            )
        except Exception as exc:
            _update_sk_customer_sync_state(sk_customer_id, ghl_contact_id=None, error=str(exc)[:500])
            summary["errors"] += 1
            summary["items"].append(
                {
                    "sk_customer_id": sk_customer_id,
                    "customer": customer_display_name(customer),
                    "action": "error_create",
                    "error": str(exc),
                }
            )
        if CUSTOMER_SYNC_DELAY_SECONDS > 0:
            await asyncio.sleep(CUSTOMER_SYNC_DELAY_SECONDS)

    summary["elapsed_ms"] = round((time.perf_counter() - started_at) * 1000, 1)
    return summary


@app.post("/jobs/skimmer_drive_sync")
async def skimmer_drive_sync(request: Request, import_after: int = 1):
    _auth_or_401(request)
    result = await _sync_skimmer_from_google_drive(import_after=bool(import_after))
    if result.get("import", {}).get("status") == "error":
        raise HTTPException(status_code=500, detail=result["import"].get("detail", "Import failed"))
    return result


# ==========================
# Summary logic (Managers only, v1)
# ==========================
def _parse_iso(ts: Optional[str]) -> Optional[dt.datetime]:
    return parse_iso(ts)

def _is_recent(ts: Optional[str], now_local: dt.datetime, window_minutes: int) -> bool:
    return is_recent(TZ_NAME, ts, now_local, window_minutes)

def _is_escalated(issue_type: str, first_inbound_ts: Optional[str], created_ts: str, now_local: dt.datetime) -> bool:
    return is_escalated(BUSINESS_TIME, issue_type, first_inbound_ts, created_ts, now_local)

async def _manager_conversation_for_contact(contact_id: str) -> Optional[str]:
    # lookup via conversations/search?contactId=...
    return await ghl_find_conversation_id_for_contact(contact_id, None)


register_sms_routes(
    app,
    SMSRouteDeps(
        tz_name=TZ_NAME,
        sms_sla_hours=SMS_SLA_HOURS,
        ack_close_enabled=ACK_CLOSE_ENABLED,
        ack_close_window_mode=ACK_CLOSE_WINDOW_MODE,
        ack_close_window_hours=ACK_CLOSE_WINDOW_HOURS,
        ack_close_max_len=ACK_CLOSE_MAX_LEN,
        ack_close_ignore_window_for_pure_ack=ACK_CLOSE_IGNORE_WINDOW_FOR_PURE_ACK,
        internal_contact_ids=INTERNAL_CONTACT_IDS,
        auth_or_401=_auth_or_401,
        parse_request_payload=_parse_request_payload,
        log_raw_event=_log_raw_event,
        flow_who=_flow_who,
        now_local=_now_local,
        add_business_hours=add_business_hours,
        ghl_find_conversation_id_for_contact=ghl_find_conversation_id_for_contact,
        set_last_internal_outbound=set_last_internal_outbound,
        manager_conversation_for_contact=_manager_conversation_for_contact,
        ghl_send_message=ghl_send_message,
        recent_staff_outbound_ts=_recent_staff_outbound_ts,
        reply_window_hours=AI_GATE_GAP_HOURS,
        flow_log=_flow_log,
        get_last_internal_outbound=get_last_internal_outbound,
        parse_iso_dt=_parse_iso_dt,
        business_day_end_for=_business_day_end_for,
        ai_inbound_should_suppress=_ai_inbound_should_suppress,
        db=db,
        ghl_get_contact_name=ghl_get_contact_name,
        list_open_issues=list_open_issues,
        set_issue_contact_name=_set_issue_contact_name,
        render_list_like_summary=_render_list_like_summary,
        get_issue_by_id=get_issue_by_id,
        add_note=add_note,
        resolve_by_id=resolve_by_id,
        resolve_target=resolve_target,
        mark_spam=mark_spam,
        resolve_by_phone=resolve_by_phone,
        ghl_conversation_link=ghl_conversation_link,
    ),
)

register_webhook_routes(
    app,
    WebhookRouteDeps(
        auth_or_401=_auth_or_401,
        parse_request_payload=_parse_request_payload,
        log_raw_event=_log_raw_event,
        flow_who=_flow_who,
        has_missed_call_marker=_has_missed_call_marker,
        ai_inbound_should_suppress=_ai_inbound_should_suppress,
        call_require_missed_marker=CALL_REQUIRE_MISSED_MARKER,
        call_missed_marker_keys=CALL_MISSED_MARKER_KEYS,
        call_sla_hours=CALL_SLA_HOURS,
        call_dedupe_window_minutes=CALL_DEDUPE_WINDOW_MINUTES,
        now_local=_now_local,
        add_business_hours=add_business_hours,
        is_recent=_is_recent,
        find_recent_call_issue=_webhook_find_recent_call_issue,
        create_call_issue=_webhook_create_call_issue,
        is_spam=_webhook_is_spam,
        ghl_get_contact_name=ghl_get_contact_name,
        ghl_find_conversation_id_for_contact=ghl_find_conversation_id_for_contact,
        recent_staff_outbound_ts=_recent_staff_outbound_ts,
        reply_window_hours=AI_GATE_GAP_HOURS,
        flow_log=_flow_log,
    ),
)

def _fmt_dt_local(ts: Optional[str]) -> str:
    return fmt_dt_local(TZ_NAME, ts)

def _build_section_lines(rows: List[sqlite3.Row], label: str, now_local: dt.datetime) -> Tuple[List[str], List[str]]:
    return build_section_lines(rows, label, now_local, BUSINESS_TIME, SUMMARY_MAX_ITEMS_PER_SECTION)

def _display_name(r: sqlite3.Row) -> str:
    return display_name(r)

async def _enrich_issues_with_contact_names(issues: List[sqlite3.Row]) -> None:
    await enrich_issues_with_contact_names(issues, ghl_get_contact_name)


@app.post("/jobs/send_summary")
async def send_summary(request: Request, slot: str = "morning", dry_run: int = 0):
    """
    Manager-only scheduled summaries at 8/11/3.

    Sections:
      - Missed / Unanswered Calls
      - Unanswered Customer Texts
      - Resolved since last summary (dopamine, then disappears)

    Escalation:
      - If still OPEN after 24 business hours -> Escalated section
    """
    _auth_or_401(request)

    now_local = _now_local()
    now_iso = now_local.isoformat()

    # (Optional) run resolver first so summaries don't include already-answered threads
    # Keep deterministic, but don't fail summary if resolver has transient API issue.
    try:
        await poll_resolver(request, limit=500)
    except Exception:
        pass

    # Resolved since last summary
    key = "last_summary_ts"
    slot_key = f"last_summary_ts_{slot.lower()}"  # backward-compat fallback
    last_ts = kv_get(key) or kv_get(slot_key)
    overdue_sms = fetch_overdue_issues(now_iso, "SMS")
    overdue_calls = fetch_overdue_issues(now_iso, "CALL")
    resolved_since = fetch_resolved_since(last_ts, now_iso)

    # Enrich issues with contact names if missing
    await _enrich_issues_with_contact_names(list(overdue_sms) + list(overdue_calls) + list(resolved_since))

    # Re-fetch issues after enrichment to get updated meta data
    overdue_sms = fetch_overdue_issues(now_iso, "SMS")
    overdue_calls = fetch_overdue_issues(now_iso, "CALL")
    resolved_since = fetch_resolved_since(last_ts, now_iso)

    title = summary_title(slot)
    lines: List[str] = []
    lines.append(f"NTPP Sentinel — {title} ({_fmt_date_local(now_local)}) • as of {_fmt_as_of_local(now_local)}")
    lines.append(f"Overdue: Calls {len(overdue_calls)} | Texts {len(overdue_sms)}")
    lines.append("")

    # Calls
    sec_calls, esc_calls = _build_section_lines(overdue_calls, "Calls", now_local)
    lines.extend(sec_calls)

    # SMS
    sec_sms, esc_sms = _build_section_lines(overdue_sms, "Texts", now_local)
    lines.extend(sec_sms)

    # Escalations section (manager-only rollup)
    escalated_lines = []
    if esc_calls or esc_sms:
        escalated_lines.append("⚠️ Escalated (24+ business hrs):")
        escalated_lines.extend(esc_calls[:SUMMARY_MAX_ITEMS_PER_SECTION])
        escalated_lines.extend(esc_sms[:SUMMARY_MAX_ITEMS_PER_SECTION])

    if escalated_lines:
        lines.extend(escalated_lines)

    # Dopamine section: show once then disappears
    if last_ts:
        if resolved_since:
            lines.append(f"✅ Resolved since last summary ({len(resolved_since)}):")
            for r in resolved_since[:RESOLVED_SINCE_MAX_ITEMS]:
                who = _display_name(r)
                rt = _fmt_dt_local(r["resolved_ts"])
                lines.append(f"#{r['id']} {r['issue_type']} {who} at {rt}")
        else:
            lines.append("✅ Resolved since last summary: none")
    lines.append("")
    lines.append("Reply:")
    lines.append("Open 3 | Resolve 3 5 6 | Spam 7 | Note 3 <text> | List | More")

    # keep SMS concise
    body = "\n".join(lines)
    if len(body) > 1450:
        body = body[:1450] + "\n…"

    # Update last_summary_ts for this slot (even in dry_run, so set after send unless dry_run)
    result = {
        "job": "send_summary",
        "slot": slot,
        "overdue_sms": len(overdue_sms),
        "overdue_calls": len(overdue_calls),
        "resolved_since": len(resolved_since),
        "dry_run": bool(dry_run),
        "body": body,
    }

    if dry_run:
        return result

    if not MANAGER_CONTACT_IDS:
        result["sent"] = False
        result["error"] = "MANAGER_CONTACT_IDS not configured"
        return result

    sent_to: List[str] = []
    errors: List[str] = []

    for mgr_contact_id in MANAGER_CONTACT_IDS:
        try:
            conv_id = await _manager_conversation_for_contact(mgr_contact_id)
            if not conv_id:
                errors.append(f"manager contact {mgr_contact_id}: no conversation found")
                continue
            await ghl_send_message(conv_id, mgr_contact_id, body)
            sent_to.append(mgr_contact_id)
        except Exception as e:
            errors.append(f"manager contact {mgr_contact_id}: {type(e).__name__}")

    kv_set(key, now_iso)
    kv_set(slot_key, now_iso)

    result["sent"] = True if sent_to else False
    result["sent_to"] = sent_to
    if errors:
        result["errors"] = errors
    return result


# ==========================
# Escalations job (optional separate rollup; v1 placeholder)
# ==========================
@app.post("/jobs/escalations")
async def escalations(request: Request, dry_run: int = 0, limit: int = 200):
    _auth_or_401(request)

    now_local = _now_local()
    now_iso = now_local.isoformat()

    # Freshen issue states before scanning for breaches.
    try:
        await poll_resolver(request, limit=500)
    except Exception:
        pass

    rows = fetch_unnotified_breaches(now_iso, limit)
    if not rows:
        return {
            "job": "escalations",
            "new_breaches": 0,
            "dry_run": bool(dry_run),
            "sent": False,
        }

    await _enrich_issues_with_contact_names(list(rows))

    rows = fetch_unnotified_breaches(now_iso, limit)
    if not rows:
        return {
            "job": "escalations",
            "new_breaches": 0,
            "dry_run": bool(dry_run),
            "sent": False,
        }

    lines: List[str] = []
    lines.append(f"NTPP Sentinel — SLA Breach Alert ({_fmt_date_local(now_local)}) • as of {_fmt_as_of_local(now_local)}")
    lines.append(f"New breaches: {len(rows)}")
    lines.append("")

    calls = [r for r in rows if (r["issue_type"] or "").upper() == "CALL"]
    texts = [r for r in rows if (r["issue_type"] or "").upper() == "SMS"]

    if calls:
        lines.append(f"Calls ({len(calls)}):")
        for r in calls[:SUMMARY_MAX_ITEMS_PER_SECTION]:
            lines.append(f"#{r['id']} {_display_name(r)} — due {_fmt_dt_local(r['due_ts'])}")

    if texts:
        lines.append(f"Texts ({len(texts)}):")
        for r in texts[:SUMMARY_MAX_ITEMS_PER_SECTION]:
            inc = r["inbound_count"] if r["inbound_count"] is not None else 0
            lines.append(f"#{r['id']} {_display_name(r)} — due {_fmt_dt_local(r['due_ts'])} in={inc}")

    shown = min(len(calls), SUMMARY_MAX_ITEMS_PER_SECTION) + min(len(texts), SUMMARY_MAX_ITEMS_PER_SECTION)
    if len(rows) > shown:
        lines.append(f"+{len(rows) - shown} more")

    body = "\n".join(lines)
    if len(body) > 1450:
        body = body[:1450] + "\n…"

    result = {
        "job": "escalations",
        "new_breaches": len(rows),
        "dry_run": bool(dry_run),
        "body": body,
    }

    if dry_run:
        return result

    if not MANAGER_CONTACT_IDS:
        result["sent"] = False
        result["error"] = "MANAGER_CONTACT_IDS not configured"
        return result

    sent_to: List[str] = []
    errors: List[str] = []

    for mgr_contact_id in MANAGER_CONTACT_IDS:
        try:
            conv_id = await _manager_conversation_for_contact(mgr_contact_id)
            if not conv_id:
                errors.append(f"manager contact {mgr_contact_id}: no conversation found")
                continue
            await ghl_send_message(conv_id, mgr_contact_id, body)
            sent_to.append(mgr_contact_id)
        except Exception as e:
            errors.append(f"manager contact {mgr_contact_id}: {type(e).__name__}")

    # Mark alerted only if at least one manager received the alert.
    if sent_to:
        conn = db()
        ids = [r["id"] for r in rows]
        q = "UPDATE issues SET breach_notified_ts=? WHERE id IN (%s) AND breach_notified_ts IS NULL" % ",".join(["?"] * len(ids))
        conn.execute(q, [now_iso] + ids)
        conn.commit()
        conn.close()
        _flow_log("escalations.sent", issue_ids=ids, sent_to_count=len(sent_to))

    result["sent"] = True if sent_to else False
    result["sent_to"] = sent_to
    if errors:
        result["errors"] = errors
    result["marked_notified"] = len(rows) if sent_to else 0
    return result
