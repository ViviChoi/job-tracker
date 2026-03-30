from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    return json.loads(config_path.read_text())


def get_timezone(config: dict) -> ZoneInfo:
    tz_str = config["notification"].get("timezone", "auto")
    if tz_str == "auto":
        try:
            res = requests.get("https://ipapi.co/json/", timeout=10)
            tz_str = res.json().get("timezone", "UTC")
        except Exception:
            tz_str = "UTC"
    return ZoneInfo(tz_str)


def is_scheduled_time(config: dict) -> bool:
    """Check if current time matches any scheduled notification time."""
    scheduled_times = config["notification"].get("scheduled_times", [])
    tz = get_timezone(config)
    now = datetime.now(tz)
    current = now.strftime("%H:%M")
    return current in scheduled_times


def is_quiet_hours(config: dict) -> bool:
    from notifier import is_quiet_hours as _check
    return _check(config)


def should_notify_now(config: dict) -> bool:
    mode = config["notification"].get("mode", "realtime")
    quiet = is_quiet_hours(config)

    if quiet:
        return False

    if mode == "realtime":
        return True

    # scheduled mode: only at configured times
    return is_scheduled_time(config)


def get_interval_seconds(config: dict) -> int:
    return config["search"].get("interval_minutes", 60) * 60
