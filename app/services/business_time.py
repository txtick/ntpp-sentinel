import datetime as dt
from dataclasses import dataclass
from typing import Optional, Tuple
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BusinessTimeConfig:
    tz_name: str
    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int

    @property
    def start_total(self) -> int:
        return (self.start_hour * 60) + self.start_minute

    @property
    def end_total(self) -> int:
        return (self.end_hour * 60) + self.end_minute


def parse_hhmm(value: str, fallback_hour: int, fallback_minute: int) -> Tuple[int, int]:
    s = (value or "").strip()
    parts = s.split(":", 1)
    if len(parts) != 2:
        return fallback_hour, fallback_minute
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        return fallback_hour, fallback_minute
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return fallback_hour, fallback_minute
    return hour, minute


def now_local(tz_name: str) -> dt.datetime:
    return dt.datetime.now(tz=ZoneInfo(tz_name))


def parse_iso_dt(value) -> Optional[dt.datetime]:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        try:
            return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


def parse_iso(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def parse_ghl_date(value) -> Optional[dt.datetime]:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def next_business_day(ts: dt.datetime) -> dt.datetime:
    cur = ts
    while cur.weekday() == 6:
        cur = cur + dt.timedelta(days=1)
    return cur


def business_day_end_for(cfg: BusinessTimeConfig, ts_local: dt.datetime) -> dt.datetime:
    if ts_local.tzinfo is None:
        ts_local = ts_local.replace(tzinfo=ZoneInfo(cfg.tz_name))
    ts_local = ts_local.astimezone(ZoneInfo(cfg.tz_name))
    end_today = ts_local.replace(hour=cfg.end_hour, minute=cfg.end_minute, second=0, microsecond=0)
    base_day = ts_local
    if ts_local > end_today:
        base_day = (ts_local + dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    base_day = next_business_day(base_day)
    return base_day.replace(hour=cfg.end_hour, minute=cfg.end_minute, second=0, microsecond=0)


def is_business_time(cfg: BusinessTimeConfig, ts: dt.datetime) -> bool:
    if ts.weekday() == 6:
        return False
    start = ts.replace(hour=cfg.start_hour, minute=cfg.start_minute, second=0, microsecond=0)
    end = ts.replace(hour=cfg.end_hour, minute=cfg.end_minute, second=0, microsecond=0)
    return start <= ts <= end


def roll_to_next_business_open(cfg: BusinessTimeConfig, ts: dt.datetime) -> dt.datetime:
    cur = ts
    while True:
        if cur.weekday() == 6:
            cur = (cur + dt.timedelta(days=1)).replace(
                hour=cfg.start_hour,
                minute=cfg.start_minute,
                second=0,
                microsecond=0,
            )
            continue
        cur_mins = (cur.hour * 60) + cur.minute
        if cur_mins < cfg.start_total:
            return cur.replace(hour=cfg.start_hour, minute=cfg.start_minute, second=0, microsecond=0)
        if cur_mins >= cfg.end_total:
            cur = (cur + dt.timedelta(days=1)).replace(
                hour=cfg.start_hour,
                minute=cfg.start_minute,
                second=0,
                microsecond=0,
            )
            continue
        return cur


def add_business_hours(cfg: BusinessTimeConfig, start_local: dt.datetime, hours: float) -> dt.datetime:
    if start_local.tzinfo is None:
        start_local = start_local.replace(tzinfo=ZoneInfo(cfg.tz_name))

    remaining = hours * 3600.0
    cur = roll_to_next_business_open(cfg, start_local)

    while remaining > 0:
        day_end = cur.replace(hour=cfg.end_hour, minute=cfg.end_minute, second=0, microsecond=0)
        available = (day_end - cur).total_seconds()
        if remaining <= available:
            return cur + dt.timedelta(seconds=remaining)

        remaining -= available
        cur = (cur + dt.timedelta(days=1)).replace(
            hour=cfg.start_hour,
            minute=cfg.start_minute,
            second=0,
            microsecond=0,
        )
        while cur.weekday() == 6:
            cur = (cur + dt.timedelta(days=1)).replace(
                hour=cfg.start_hour,
                minute=cfg.start_minute,
                second=0,
                microsecond=0,
            )

    return cur


def fmt_date_local(value: dt.datetime) -> str:
    return value.strftime("%b %-d")


def fmt_as_of_local(value: dt.datetime) -> str:
    return value.strftime("%-I:%M%p").lower() + " CT"


def fmt_dt_local(tz_name: str, ts: Optional[str]) -> str:
    parsed = parse_iso(ts)
    if not parsed:
        return "-"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
    return parsed.astimezone(ZoneInfo(tz_name)).strftime("%-I:%M%p").lower()


def is_recent(tz_name: str, ts: Optional[str], now_value: dt.datetime, window_minutes: int) -> bool:
    if not ts:
        return False
    parsed = parse_iso(ts)
    if parsed is None:
        return False
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
        delta = now_value - parsed.astimezone(ZoneInfo(tz_name))
        return 0 <= delta.total_seconds() <= (max(0, window_minutes) * 60.0)
    except Exception:
        return False


def is_escalated(
    cfg: BusinessTimeConfig,
    issue_type: str,
    first_inbound_ts: Optional[str],
    created_ts: str,
    now_value: dt.datetime,
) -> bool:
    base_ts = first_inbound_ts if issue_type == "SMS" and first_inbound_ts else created_ts
    base = parse_iso(base_ts)
    if not base:
        return False
    if base.tzinfo is None:
        base = base.replace(tzinfo=ZoneInfo(cfg.tz_name))
    threshold = add_business_hours(cfg, base.astimezone(ZoneInfo(cfg.tz_name)), 24.0)
    return now_value >= threshold
