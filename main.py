"""BMS Ticket Notifier — Hyderabad / ALLU Cinemas / Dolby Cinema.

Checks configured BookMyShow dates and emails when a new matching show session
appears. It also reports ticket-category changes for sessions already known.

Required GitHub Actions secrets:
  RESEND_API_KEY, RESEND_TO_EMAIL
Optional variables:
  BMS_URL, BMS_DATES, BMS_THEATRE, BMS_FORMAT, BMS_TIME,
  RESEND_FROM_EMAIL, BMS_STATE_FILE
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BMS_URL = os.getenv(
    "BMS_URL",
    "https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/buytickets/ET00439706",
).strip()

BMS_DATES = os.getenv(
    "BMS_DATES",
    "20261217,20261218,20261219,20261220,20261221,20261222,20261223,"
    "20261224,20261225,20261226,20261227,20261228,20261229,20261230,20261231",
).strip()

# ALLU is deliberately a broad substring match for the Kokapet venue name.
BMS_THEATRE = os.getenv("BMS_THEATRE", "ALLU").strip()
BMS_FORMAT = os.getenv("BMS_FORMAT", "DOLBY CINEMA").strip()
# Empty means all showtimes. Otherwise use e.g. morning,afternoon,evening,night.
BMS_TIME = os.getenv("BMS_TIME", "").strip()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "").strip()
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL", "onboarding@resend.dev"
).strip()

STATE_FILE = os.getenv("BMS_STATE_FILE", "bms_state.json")
REQUEST_TIMEOUT = 20

API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

REGION_MAP = {
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
}

STATUS_LABELS = {
    "0": "SOLD OUT",
    "1": "ALMOST FULL",
    "2": "FILLING FAST",
    "3": "AVAILABLE",
}

TIME_PERIODS = {
    "morning": (600, 1200),
    "afternoon": (1200, 1600),
    "evening": (1600, 1900),
    "night": (1900, 2400),
}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def parse_bms_url(url: str) -> tuple[str, str]:
    """Return (event_code, region_slug) from a standard BMS movie URL."""
    parts = [part for part in urlparse(url).path.strip("/").split("/") if part]
    event_code = next((p for p in parts if re.fullmatch(r"ET\d{8,}", p)), "")
    region_slug = ""
    if "movies" in parts:
        index = parts.index("movies")
        if index + 1 < len(parts):
            region_slug = parts[index + 1].lower()
    if not event_code or not region_slug:
        raise ValueError(
            "BMS_URL must contain a region and event code, e.g. "
            "https://in.bookmyshow.com/movies/hyderabad/.../ET00439706"
        )
    return event_code, region_slug


def parse_dates() -> list[str]:
    dates = [value.strip() for value in BMS_DATES.split(",") if value.strip()]
    if not dates:
        raise ValueError("BMS_DATES is empty. Supply comma-separated YYYYMMDD dates.")
    for value in dates:
        if not re.fullmatch(r"\d{8}", value):
            raise ValueError(f"Invalid date {value!r}; expected YYYYMMDD.")
    return dates


def searchable_text(value: Any) -> str:
    """Flatten strings/lists/dicts used by BookMyShow format attributes."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(searchable_text(item) for item in value)
    if isinstance(value, dict):
        preferred = ("name", "title", "label", "value", "text", "attribute", "code")
        pieces = [searchable_text(value[key]) for key in preferred if key in value]
        return " ".join(piece for piece in pieces if piece) or json.dumps(value, ensure_ascii=False)
    return str(value)


def normalise_for_match(value: str) -> str:
    """Normalise labels so DOLBY-CINEMA and Dolby Cinema compare consistently."""
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def parse_time_code(show: dict[str, Any], extra: dict[str, Any]) -> int | None:
    raw = extra.get("showTimeCode") or show.get("showTimeCode")
    if raw is not None:
        digits = re.sub(r"\D", "", str(raw))
        if digits:
            try:
                number = int(digits)
                if 0 <= number <= 2359 and number % 100 < 60:
                    return number
            except ValueError:
                pass

    title = str(show.get("title") or "").strip().upper()
    match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", title)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3) == "AM":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return hour * 100 + minute


# ---------------------------------------------------------------------------
# BOOKMYSHOW API
# ---------------------------------------------------------------------------

def fetch_bms(
    event_code: str,
    date_code: str,
    region_code: str,
    region_slug: str,
    latitude: str,
    longitude: str,
    geohash: str,
) -> dict[str, Any] | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://in.bookmyshow.com/movies/{region_slug}/buytickets/{event_code}/",
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": latitude,
        "x-longitude": longitude,
        "x-location-selection": "manual",
        "x-lsid": "",
    }
    params = {
        "eventCode": event_code,
        "dateCode": date_code,
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": latitude,
        "lon": longitude,
    }

    try:
        response = requests.get(
            API_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT
        )
        print(f"  HTTP {response.status_code} | {date_code}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            print("  ERROR: API response was not a JSON object.")
            return None
        return payload
    except (requests.RequestException, ValueError) as error:
        print(f"  Request/JSON error for {date_code}: {error}")
        return None


# ---------------------------------------------------------------------------
# SHOWTIME PARSER
# ---------------------------------------------------------------------------

def parse_shows(data: dict[str, Any], requested_date: str) -> list[dict[str, Any]]:
    """Parse BMS v4 groupList -> venueGroup -> venue-card showtimes."""
    shows: list[dict[str, Any]] = []
    root = data.get("data") or {}
    if not isinstance(root, dict):
        return shows

    widgets = root.get("showtimeWidgets") or []
    if not isinstance(widgets, list):
        return shows

    for widget in widgets:
        if not isinstance(widget, dict) or widget.get("type") != "groupList":
            continue
        groups = widget.get("data") or []
        if not isinstance(groups, list):
            continue

        for group in groups:
            if not isinstance(group, dict) or group.get("type") != "venueGroup":
                continue
            cards = group.get("data") or []
            if not isinstance(cards, list):
                continue

            for card in cards:
                if not isinstance(card, dict) or card.get("type") != "venue-card":
                    continue
                venue_data = card.get("additionalData") or {}
                if not isinstance(venue_data, dict):
                    venue_data = {}
                venue_name = str(venue_data.get("venueName") or "Unknown")
                venue_code = str(venue_data.get("venueCode") or "")

                for show in card.get("showtimes") or []:
                    if not isinstance(show, dict):
                        continue
                    extra = show.get("additionalData") or {}
                    if not isinstance(extra, dict):
                        extra = {}

                    show_date = str(
                        extra.get("showDateCode")
                        or extra.get("dateCode")
                        or (str(extra.get("cutOffDateTime", ""))[:8])
                        or requested_date
                    )
                    raw_format = show.get("screenAttr") or extra.get("attributes") or ""
                    screen_format = searchable_text(raw_format).strip()
                    raw_categories = extra.get("categories") or show.get("categories") or []
                    categories: list[dict[str, str]] = []
                    if isinstance(raw_categories, list):
                        for category in raw_categories:
                            if not isinstance(category, dict):
                                continue
                            status = str(category.get("availStatus", "0"))
                            categories.append(
                                {
                                    "name": str(category.get("priceDesc") or "Standard"),
                                    "price": str(category.get("curPrice", "0")),
                                    "status": status,
                                    "status_text": STATUS_LABELS.get(status, "UNKNOWN"),
                                }
                            )

                    session_id = str(extra.get("sessionId") or "")
                    time_text = str(show.get("title") or "Unknown time")
                    # A fallback identity is needed if BMS omits sessionId.
                    if not session_id:
                        session_id = f"{show_date}|{time_text}|{screen_format}"

                    shows.append(
                        {
                            "venue": venue_name,
                            "venue_code": venue_code,
                            "session_id": session_id,
                            "date": show_date,
                            "time": time_text,
                            "time_code": parse_time_code(show, extra),
                            "format": screen_format,
                            "categories": categories,
                        }
                    )
    return shows


def print_response_diagnostics(data: dict[str, Any]) -> None:
    root = data.get("data") or {}
    print("  DEBUG top-level keys:", list(data.keys()))
    if not isinstance(root, dict):
        print("  DEBUG data field type:", type(root).__name__)
        return
    print("  DEBUG data keys:", list(root.keys()))
    widgets = root.get("showtimeWidgets", [])
    if isinstance(widgets, list):
        print(
            "  DEBUG widget types:",
            [item.get("type") for item in widgets if isinstance(item, dict)],
        )


# ---------------------------------------------------------------------------
# FILTERING
# ---------------------------------------------------------------------------

def filter_shows(shows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    theatre_filter = BMS_THEATRE.casefold().strip()
    format_filter = normalise_for_match(BMS_FORMAT)
    periods = [item.strip().casefold() for item in BMS_TIME.split(",") if item.strip()]

    invalid = [period for period in periods if period not in TIME_PERIODS]
    if invalid:
        raise ValueError(
            "Invalid BMS_TIME value(s): " + ", ".join(invalid)
            + ". Use morning, afternoon, evening, night, or leave it empty for all times."
        )

    result = []
    for show in shows:
        if theatre_filter and theatre_filter not in show["venue"].casefold():
            continue
        if format_filter and format_filter not in normalise_for_match(show["format"]):
            continue
        if periods:
            time_code = show.get("time_code")
            if time_code is None:
                continue
            if not any(
                start <= time_code < end
                for period in periods
                for start, end in [TIME_PERIODS[period]]
            ):
                continue
        result.append(show)
    return result


# ---------------------------------------------------------------------------
# STATE AND CHANGE DETECTION
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
        if not isinstance(state, dict):
            return {}
        # Migrate the earlier category-keyed state file safely. A fresh baseline
        # is preferable to falsely interpreting the old schema as new sessions.
        if "shows" not in state or not isinstance(state.get("shows"), dict):
            return {}
        return state
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    temporary_path = STATE_FILE + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)
    os.replace(temporary_path, STATE_FILE)


def show_key(show: dict[str, Any]) -> str:
    return "|".join(
        [show["venue_code"], show["session_id"], show["date"]]
    )


def build_state(shows: list[dict[str, Any]]) -> dict[str, Any]:
    """Store every matching session, even when no ticket categories are returned."""
    sessions: dict[str, Any] = {}
    for show in shows:
        key = show_key(show)
        categories = {}
        for category in show["categories"]:
            categories[category["name"]] = {
                "price": category["price"],
                "status": category["status"],
                "status_text": category["status_text"],
            }
        sessions[key] = {
            "venue": show["venue"],
            "venue_code": show["venue_code"],
            "date": show["date"],
            "time": show["time"],
            "format": show["format"],
            "categories": categories,
        }
    return {"initialized": True, "shows": sessions}


def detect_changes(old_state: dict[str, Any], new_state: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    # Crucial: a newly listed Dolby Cinema session triggers an alert even if
    # BookMyShow has not returned ticket categories or all categories are sold out.
    for key, current in new_shows.items():
        previous = old_shows.get(key)
        if previous is None:
            changes.append(
                f"NEW DOLBY CINEMA SHOW: {current['venue']} | {current['date']} | "
                f"{current['time']} | {current['format'] or 'format label unavailable'}"
            )
            continue

        old_categories = previous.get("categories", {})
        new_categories = current.get("categories", {})
        for name, category in new_categories.items():
            old_category = old_categories.get(name)
            if old_category and old_category.get("status") == "0" and category.get("status") != "0":
                changes.append(
                    f"TICKETS AVAILABLE: {current['venue']} | {current['date']} | "
                    f"{current['time']} | {name} ({category['status_text']})"
                )
            elif old_category is None:
                changes.append(
                    f"NEW TICKET CATEGORY: {current['venue']} | {current['date']} | "
                    f"{current['time']} | {name}"
                )
    return changes


# ---------------------------------------------------------------------------
# EMAIL NOTIFICATION
# ---------------------------------------------------------------------------

def send_email(changes: list[str], shows: list[dict[str, Any]]) -> bool:
    if not RESEND_API_KEY or not RESEND_TO_EMAIL:
        print("ERROR: RESEND_API_KEY or RESEND_TO_EMAIL is missing; cannot send alert.")
        return False

    change_html = "".join(f"<li>{html.escape(change)}</li>" for change in changes)
    rows = []
    for show in shows:
        categories = show["categories"] or [
            {"name": "Not provided", "price": "—", "status_text": "Unknown"}
        ]
        for category in categories:
            rows.append(
                "<tr>"
                f"<td>{html.escape(show['venue'])}</td>"
                f"<td>{html.escape(show['date'])}</td>"
                f"<td>{html.escape(show['time'])}</td>"
                f"<td>{html.escape(show['format'] or 'Not provided')}</td>"
                f"<td>{html.escape(category['name'])}</td>"
                f"<td>{html.escape(category['price'])}</td>"
                f"<td>{html.escape(category['status_text'])}</td>"
                "</tr>"
            )

    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    body = f"""<!doctype html>
<html><body>
<h2>New Dolby Cinema listing — Avengers: Doomsday</h2>
<p>Checked: {html.escape(checked_at)}</p>
<h3>Detected changes</h3><ul>{change_html}</ul>
<h3>Current matching showtimes</h3>
<table border="1" cellpadding="7" cellspacing="0">
<tr><th>Theatre</th><th>Date</th><th>Time</th><th>Format</th><th>Category</th><th>Price</th><th>Status</th></tr>
{''.join(rows)}
</table>
<p>Automated BookMyShow Ticket Notifier</p>
</body></html>"""

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [RESEND_TO_EMAIL],
                "subject": "NEW DOLBY CINEMA SHOW — Avengers: Doomsday",
                "html": body,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code in (200, 201):
            print(f"Email notification sent to {RESEND_TO_EMAIL}.")
            return True
        print(f"Email failed: HTTP {response.status_code}: {response.text[:500]}")
        return False
    except requests.RequestException as error:
        print(f"Email request failed: {error}")
        return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 64)
    print("BMS TICKET NOTIFIER — AVENGERS: DOOMSDAY")
    print("=" * 64)
    print("Checked at:", datetime.now().astimezone().isoformat(timespec="seconds"))
    print("Theatre filter:", BMS_THEATRE or "ALL THEATRES")
    print("Format filter:", BMS_FORMAT or "ALL FORMATS")
    print("Time filter:", BMS_TIME or "ALL TIMES")

    try:
        event_code, url_region = parse_bms_url(BMS_URL)
        dates = parse_dates()
        region = REGION_MAP.get(url_region)
        if region is None:
            raise ValueError(f"Unsupported BMS region in URL: {url_region}")
        region_code, region_slug, latitude, longitude, geohash = region
    except ValueError as error:
        print("CONFIGURATION ERROR:", error)
        return 2

    print("Event:", event_code)
    print("Region:", region_code)
    print("Dates:", dates)

    all_shows: list[dict[str, Any]] = []
    successful_dates: list[str] = []
    valid_parser_dates: list[str] = []

    for date_code in dates:
        print(f"\nChecking {date_code}...")
        data = fetch_bms(
            event_code, date_code, region_code, region_slug,
            latitude, longitude, geohash,
        )
        if data is None:
            print("  Request failed; preserving previous monitoring state.")
            continue

        successful_dates.append(date_code)
        root = data.get("data")
        if isinstance(root, dict) and isinstance(root.get("showtimeWidgets"), list):
            valid_parser_dates.append(date_code)
        else:
            print("  WARNING: response does not contain data.showtimeWidgets as a list.")
            print_response_diagnostics(data)
            continue

        shows = parse_shows(data, date_code)
        print(f"  Retrieved {len(shows)} showtime(s) before filters.")
        if not shows:
            print_response_diagnostics(data)
        all_shows.extend(shows)

    # Never replace state after an incomplete API run or unexpected response shape.
    if len(successful_dates) != len(dates) or len(valid_parser_dates) != len(dates):
        print("\nERROR: One or more dates could not be checked reliably.")
        print("Previous monitoring state preserved.")
        return 1

    try:
        matching_shows = filter_shows(all_shows)
    except ValueError as error:
        print("CONFIGURATION ERROR:", error)
        return 2

    print("\n" + "=" * 64)
    print("TOTAL PARSED SHOWTIMES:", len(all_shows))
    print("MATCHING DOLBY CINEMA SHOWTIMES:", len(matching_shows))
    print("=" * 64)

    for show in matching_shows:
        print(
            f"\n{show['venue']} | {show['date']} | {show['time']} | "
            f"{show['format'] or 'FORMAT NOT PROVIDED'}"
        )
        if not show["categories"]:
            print("  Ticket categories not provided by API.")
        for category in show["categories"]:
            print(
                f"  {category['name']} | Rs.{category['price']} | "
                f"{category['status_text']}"
            )

    old_state = load_state()
    was_initialized = bool(old_state.get("initialized"))

    # If there are no matching sessions after a previous non-empty baseline,
    # preserve the previous state. This avoids wiping it on a transient API/filter
    # response. On the very first successful run, an empty baseline is saved so
    # that the first Dolby Cinema session appearing later WILL trigger an alert.
    if not matching_shows and was_initialized and old_state.get("shows"):
        print("\nNo matching sessions currently returned; previous state preserved.")
        return 0

    new_state = build_state(matching_shows)

    if not was_initialized:
        save_state(new_state)
        print("\nInitial baseline saved. Future newly listed Dolby Cinema sessions will trigger an email.")
        if matching_shows:
            print("Existing matching sessions are recorded without sending a first-run alert.")
        return 0

    changes = detect_changes(old_state, new_state)
    if changes:
        print(f"\nChanges detected: {len(changes)}")
        for change in changes:
            print(" -", change)
        if not send_email(changes, matching_shows):
            print("State not updated so the alert can be retried on the next run.")
            return 1
    else:
        print("\nNo new Dolby Cinema sessions or ticket-availability changes.")

    save_state(new_state)
    print("\nMonitoring completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
