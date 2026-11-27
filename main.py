#!/usr/bin/env python3
"""
BookMyShow showtime notifier
Tracks Avengers: Doomsday at ALLU Cinemas, Hyderabad and emails when a
matching DOLBY CINEMA session first appears.

Required environment variables:
  BMS_URL, BMS_DATES, BMS_THEATRE, BMS_FORMAT
  RESEND_API_KEY, RESEND_TO_EMAIL, RESEND_FROM_EMAIL

The script uses BookMyShow's web showtimes endpoint. It does not bypass
Cloudflare challenges. A 403 is reported as a failed check (non-zero exit).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

BMS_URL = os.getenv(
    "BMS_URL",
    "https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/"
    "buytickets/ET00439706/20261217?etCodes=*&language=English"
    "&refEventCode=ET00439706",
).strip()

DEFAULT_DATES = ",".join(
    [f"202612{day:02d}" for day in range(17, 32)] + ["20261129"]
)
BMS_DATES = os.getenv("BMS_DATES", DEFAULT_DATES).strip()
BMS_THEATRE = os.getenv("BMS_THEATRE", "ALLU").strip()
BMS_FORMAT = os.getenv("BMS_FORMAT", "DOLBY CINEMA").strip()
BMS_TIME = os.getenv("BMS_TIME", "").strip()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "").strip()
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL", "onboarding@resend.dev"
).strip()

STATE_FILE = Path(os.getenv("BMS_STATE_FILE", "bms_state.json"))
REQUEST_TIMEOUT = int(os.getenv("BMS_REQUEST_TIMEOUT", "25"))
REQUEST_DELAY = float(os.getenv("BMS_REQUEST_DELAY", "1.0"))

REGION = {
    "code": os.getenv("BMS_REGION_CODE", "HYD"),
    "slug": os.getenv("BMS_REGION_SLUG", "hyderabad"),
    "lat": os.getenv("BMS_LATITUDE", "17.385"),
    "lon": os.getenv("BMS_LONGITUDE", "78.487"),
    "geohash": os.getenv("BMS_GEOHASH", "tep"),
}


def log(message: str) -> None:
    print(message, flush=True)


def event_code_from_url(url: str) -> str:
    for part in urlparse(url).path.split("/"):
        if re.fullmatch(r"ET\d{8,}", part):
            return part
    raise ValueError("BMS_URL must contain an event code such as ET00439706.")


def parse_dates(raw: str) -> list[str]:
    dates = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if not re.fullmatch(r"\d{8}", value):
            raise ValueError(f"Invalid date {value!r}; use YYYYMMDD.")
        datetime.strptime(value, "%Y%m%d")
        if value not in dates:
            dates.append(value)
    if not dates:
        raise ValueError("BMS_DATES contains no dates.")
    return dates


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def scalar_strings(value: Any) -> list[str]:
    """Return scalar text values from a nested object."""
    found: list[str] = []
    if isinstance(value, dict):
        for v in value.values():
            found.extend(scalar_strings(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(scalar_strings(v))
    elif isinstance(value, (str, int, float)):
        found.append(str(value))
    return found


def first_value(obj: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = obj.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return str(value).strip()
    return ""


VENUE_KEYS = (
    "venue_name", "venueName", "cinema_name", "cinemaName",
    "company_name", "companyName", "name",
)
TIME_KEYS = (
    "show_time", "showTime", "showtime", "time", "time_text",
    "timeText", "session_time", "sessionTime",
)
FORMAT_KEYS = (
    "screen_attr", "screenAttr", "screen_attributes", "screenAttributes",
    "format", "formats", "dimension", "attributes", "session_attributes",
    "sessionAttributes", "experience", "experience_name", "experienceName",
)


def matching_text(obj: dict[str, Any]) -> str:
    pieces = []
    for key in FORMAT_KEYS:
        if key in obj:
            pieces.extend(scalar_strings(obj[key]))
    # Some BMS responses put format labels in sibling fields not named above.
    for key, value in obj.items():
        if any(word in key.casefold() for word in ("format", "screen", "attr", "experience")):
            pieces.extend(scalar_strings(value))
    return norm(" ".join(pieces))


def extract_sessions(payload: Any, date_code: str) -> list[dict[str, str]]:
    """
    Walk the response recursively. Venue names are inherited from parent
    objects; a candidate session must itself contain a time field.
    """
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    wanted_theatre = norm(BMS_THEATRE)
    wanted_format = norm(BMS_FORMAT)

    def walk(node: Any, inherited_venue: str = "") -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, inherited_venue)
            return
        if not isinstance(node, dict):
            return

        venue = first_value(node, VENUE_KEYS) or inherited_venue
        own_time = first_value(node, TIME_KEYS)
        fmt_text = matching_text(node)
        # Also check a node's full scalar text for exact experience wording.
        if not fmt_text:
            fmt_text = norm(" ".join(scalar_strings(node)))

        if own_time and venue:
            venue_norm = norm(venue)
            venue_ok = (
                wanted_theatre in venue_norm
                or venue_norm in wanted_theatre
            )
            format_ok = wanted_format in fmt_text
            if venue_ok and format_ok:
                if BMS_TIME and norm(BMS_TIME) not in norm(own_time):
                    pass
                else:
                    record = {
                        "date": date_code,
                        "venue": venue,
                        "time": own_time,
                        "format": BMS_FORMAT,
                    }
                    key = json.dumps(record, sort_keys=True)
                    if key not in seen:
                        seen.add(key)
                        results.append(record)

        for child in node.values():
            walk(child, venue)

    walk(payload)
    return results


def fetch_date(session: requests.Session, event_code: str, date_code: str) -> Any:
    page_url = (
        f"https://in.bookmyshow.com/movies/{REGION['slug']}/"
        f"buytickets/{event_code}/{date_code}"
    )
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": page_url,
        "Origin": "https://in.bookmyshow.com",
        "x-app-code": "WEB",
        "x-region-code": REGION["code"],
        "x-region-slug": REGION["slug"],
        "x-geohash": REGION["geohash"],
        "x-latitude": REGION["lat"],
        "x-longitude": REGION["lon"],
        "x-location-selection": "manual",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code,
        "isDesktop": "true",
        "regionCode": REGION["code"],
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": REGION["lat"],
        "lon": REGION["lon"],
    }

    response = session.get(
        API_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT
    )
    log(f"  HTTP {response.status_code}")
    if response.status_code in (403, 429):
        excerpt = re.sub(r"\s+", " ", response.text[:220]).strip()
        raise RuntimeError(
            f"BookMyShow blocked the request (HTTP {response.status_code}). "
            f"This run cannot verify showtimes. Response: {excerpt}"
        )
    response.raise_for_status()
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise RuntimeError("BookMyShow returned a non-JSON response.") from exc


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"known_sessions": [], "last_checked": None}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("State file must contain a JSON object.")
        data.setdefault("known_sessions", [])
        return data
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(f"Cannot read state file {STATE_FILE}: {exc}") from exc


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(STATE_FILE)


def send_email(new_sessions: list[dict[str, str]], all_matches: list[dict[str, str]]) -> None:
    if not RESEND_API_KEY or not RESEND_TO_EMAIL or not RESEND_FROM_EMAIL:
        raise RuntimeError(
            "New Dolby Cinema session(s) found, but Resend is not configured. "
            "Set RESEND_API_KEY, RESEND_TO_EMAIL and RESEND_FROM_EMAIL."
        )

    rows = "\n".join(
        f"<li><b>{s['date']}</b> — {s['time']} — "
        f"{s['venue']} — {s['format']}</li>"
        for s in new_sessions
    )
    html_body = f"""
    <h2>New BookMyShow Dolby Cinema session detected</h2>
    <p>Avengers: Doomsday — ALLU Cinemas, Hyderabad</p>
    <ul>{rows}</ul>
    <p><a href="{BMS_URL}">Open BookMyShow</a></p>
    <p>Detected at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
    """
    text_body = (
        "New BookMyShow DOLBY CINEMA session detected:\n"
        + "\n".join(
            f"{s['date']} | {s['time']} | {s['venue']} | {s['format']}"
            for s in new_sessions
        )
        + f"\n\nBookMyShow: {BMS_URL}"
    )

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": RESEND_FROM_EMAIL,
            "to": [RESEND_TO_EMAIL],
            "subject": "Avengers: Doomsday — New Dolby Cinema session",
            "html": html_body,
            "text": text_body,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code >= 300:
        raise RuntimeError(
            f"Resend email failed (HTTP {response.status_code}): "
            f"{response.text[:500]}"
        )
    log(f"Email sent successfully. Resend response: {response.text[:250]}")


def main() -> int:
    log("=" * 62)
    log("BMS TICKET NOTIFIER — START")
    log(f"Started: {datetime.now().astimezone().isoformat(timespec='seconds')}")
    log(f"Event: {event_code_from_url(BMS_URL)}")
    log(f"Theatre filter: {BMS_THEATRE}")
    log(f"Format filter: {BMS_FORMAT}")
    log(f"Dates to check: {BMS_DATES}")
    log("=" * 62)

    event_code = event_code_from_url(BMS_URL)
    dates = parse_dates(BMS_DATES)
    state = load_state()
    previous = set(state.get("known_sessions", []))
    current: list[dict[str, str]] = []

    with requests.Session() as session:
        for index, date_code in enumerate(dates, start=1):
            log(f"[{index}/{len(dates)}] Checking {date_code} ...")
            payload = fetch_date(session, event_code, date_code)
            matches = extract_sessions(payload, date_code)
            log(f"  Matching {BMS_THEATRE} / {BMS_FORMAT} sessions: {len(matches)}")
            for item in matches:
                log(f"    {item['date']} | {item['time']} | {item['venue']} | {item['format']}")
            current.extend(matches)
            if index < len(dates) and REQUEST_DELAY > 0:
                time.sleep(REQUEST_DELAY)

    current_keys = {
        json.dumps(item, sort_keys=True, ensure_ascii=False) for item in current
    }
    new_keys = current_keys - previous
    new_sessions = [
        json.loads(key) for key in sorted(new_keys)
    ]

    log("-" * 62)
    log(f"Total matching sessions found: {len(current)}")
    log(f"New sessions compared with saved state: {len(new_sessions)}")

    # First successful run establishes a baseline and deliberately does not
    # email every already-listed show. Set BMS_ALERT_ON_FIRST_RUN=true to alert.
    first_successful_run = not state.get("last_checked")
    alert_first_run = norm(os.getenv("BMS_ALERT_ON_FIRST_RUN", "false")) in {
        "true", "1", "yes"
    }
    if first_successful_run and not alert_first_run:
        log("First successful run: saving baseline without sending an email.")
        new_sessions = []

    if new_sessions:
        log("New matching session(s) detected; sending email...")
        send_email(new_sessions, current)

    state["known_sessions"] = sorted(current_keys)
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    state["event_code"] = event_code
    state["filters"] = {"theatre": BMS_THEATRE, "format": BMS_FORMAT}
    save_state(state)

    log(f"State saved to {STATE_FILE}.")
    log("BMS TICKET NOTIFIER — COMPLETED SUCCESSFULLY")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Interrupted.")
        raise SystemExit(130)
    except Exception as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
