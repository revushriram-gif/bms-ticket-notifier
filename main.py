
"""
BMS Ticket Notifier — Hyderabad
Avengers: Doomsday | ALLU Cinemas
BookMyShow v5 API monitor
"""

import os
import re
import sys
import json
import html
import requests

from datetime import datetime
from urllib.parse import urlparse

# ============================================================
# CONFIGURATION
# ============================================================

BMS_URL = os.getenv(
    "BMS_URL",
    "https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/buytickets/ET00439706"
)

BMS_DATES = os.getenv(
    "BMS_DATES",
    "20261217,20261218,20261219,20261220,20261221,20261222,20261223"
)

BMS_THEATRE = os.getenv(
    "BMS_THEATRE",
    "ALLU Cinemas: Kokapet"
)

BMS_FORMAT = os.getenv("BMS_FORMAT", "ATMOS")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL", "onboarding@resend.dev"
)

STATE_FILE = "bms_state.json"

API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v5/"
    "showtimes-by-event/primary-dynamic"
)

REGION = {
    "code": "HYD",
    "slug": "hyderabad",
    "lat": "17.385",
    "lon": "78.487",
    "geohash": "tep"
}

STATUS = {
    "0": "SOLD OUT",
    "1": "ALMOST FULL",
    "2": "FILLING FAST",
    "3": "AVAILABLE"
}


# ============================================================
# EVENT DETAILS
# ============================================================

def get_event_code():
    parts = urlparse(BMS_URL).path.split("/")

    for part in parts:
        if re.fullmatch(r"ET\d{8,}", part):
            return part

    raise ValueError("Could not identify BookMyShow event code")


# ============================================================
# BOOKMYSHOW REQUEST
# ============================================================

def fetch_bms(event_code, date_code):

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BMS_URL,
        "x-app-code": "WEB",
        "x-region-code": REGION["code"],
        "x-region-slug": REGION["slug"],
        "x-geohash": REGION["geohash"],
        "x-latitude": REGION["lat"],
        "x-longitude": REGION["lon"],
        "x-location-selection": "manual"
    }

    params = {
        "etCodes": "*",
        "dateCode": date_code,
        "isDesktop": "true",
        "regionCode": REGION["code"],
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "appCode": "WEB",
        "language": "english",
        "refEventCode": event_code
    }

    try:
        response = requests.get(
            API_URL,
            headers=headers,
            params=params,
            timeout=20
        )

        print(f"  HTTP {response.status_code} | {date_code}")

        if response.status_code != 200:
            print("  Request rejected.")
            print(
                "  Content-Type:",
                response.headers.get("Content-Type", "unknown")
            )
            print(
                "  Response:",
                re.sub(r"\s+", " ", response.text[:250])
            )

            return None

        try:
            return response.json()
        except ValueError:
            print("  Invalid JSON response.")
            return None

    except requests.RequestException as error:
        print(f"  Network error: {error}")
        return None


# ============================================================
# SHOWTIME PARSER
# ============================================================

def parse_shows(data, requested_date):

    shows = []

    root = data.get("data", {})

    for widget in root.get("showtimeWidgets", []):

        if widget.get("type") != "groupList":
            continue

        for group in widget.get("data", []):

            if group.get("type") != "venueGroup":
                continue

            for card in group.get("data", []):

                if card.get("type") != "venue-card":
                    continue

                venue_data = card.get("additionalData", {})

                venue_name = venue_data.get(
                    "venueName", "Unknown"
                )

                venue_code = venue_data.get(
                    "venueCode", ""
                )

                for show in card.get("showtimes", []):

                    extra = show.get("additionalData", {})

                    date_code = str(
                        extra.get("showDateCode")
                        or extra.get("dateCode")
                        or requested_date
                    )

                    screen_format = (
                        show.get("screenAttr")
                        or extra.get("attributes")
                        or ""
                    )

                    categories = []

                    for category in extra.get("categories", []):

                        status = str(
                            category.get("availStatus", "0")
                        )

                        categories.append({
                            "name": category.get(
                                "priceDesc", "Standard"
                            ),
                            "price": category.get(
                                "curPrice", "0"
                            ),
                            "status": status,
                            "status_text": STATUS.get(
                                status, "UNKNOWN"
                            )
                        })

                    shows.append({
                        "venue": venue_name,
                        "venue_code": venue_code,
                        "session_id": str(
                            extra.get("sessionId", "")
                        ),
                        "date": date_code,
                        "time": show.get("title", ""),
                        "time_code": str(
                            extra.get("showTimeCode", "")
                        ),
                        "format": screen_format,
                        "categories": categories
                    })

    return shows


# ============================================================
# FILTER SHOWTIMES
# ============================================================

def filter_shows(shows):

    result = []

    theatre_filter = BMS_THEATRE.lower().strip()
    format_filter = BMS_FORMAT.lower().strip()

    for show in shows:

        if theatre_filter:
            if theatre_filter not in show["venue"].lower():
                continue

        if format_filter:
            if format_filter not in show["format"].lower():
                continue

        result.append(show)

    return result


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():

    try:
        with open(STATE_FILE, "r") as file:
            return json.load(file)

    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):

    with open(STATE_FILE, "w") as file:
        json.dump(state, file, indent=2)


def build_state(shows):

    state = {}

    for show in shows:

        for category in show["categories"]:

            key = (
                f"{show['venue_code']}|"
                f"{show['session_id']}|"
                f"{show['date']}|"
                f"{category['name']}"
            )

            state[key] = {
                "venue": show["venue"],
                "date": show["date"],
                "time": show["time"],
                "format": show["format"],
                "category": category["name"],
                "price": category["price"],
                "status": category["status"]
            }

    return state


# ============================================================
# CHANGE DETECTION
# ============================================================

def detect_changes(old_state, new_state):

    changes = []

    for key, new in new_state.items():

        previous = old_state.get(key)

        if previous is None:

            if new["status"] != "0":
                changes.append(
                    f"NEW AVAILABLE: {new['venue']} "
                    f"{new['time']} {new['category']}"
                )

            continue

        if (
            previous["status"] == "0"
            and new["status"] != "0"
        ):

            changes.append(
                f"TICKETS AVAILABLE: {new['venue']} "
                f"{new['time']} - {new['category']} "
                f"({new['status']})"
            )

        elif (
            previous["status"] != "0"
            and new["status"] == "0"
        ):

            changes.append(
                f"SOLD OUT: {new['venue']} {new['time']}"
            )

    return changes


# ============================================================
# EMAIL NOTIFICATION
# ============================================================

def send_email(changes, shows):

    if not RESEND_API_KEY or not RESEND_TO_EMAIL:

        print("Email configuration missing. Skipping email.")
        return False

    rows = []

    for show in shows:

        for category in show["categories"]:

            status = category["status_text"]

            rows.append(
                "<tr>"
                f"<td>{html.escape(show['venue'])}</td>"
                f"<td>{html.escape(show['date'])}</td>"
                f"<td>{html.escape(show['time'])}</td>"
                f"<td>{html.escape(show['format'])}</td>"
                f"<td>{html.escape(category['name'])}</td>"
                f"<td>{html.escape(str(category['price']))}</td>"
                f"<td>{html.escape(status)}</td>"
                "</tr>"
            )

    changes_html = "".join(
        f"<li>{html.escape(change)}</li>"
        for change in changes
    )

    body = f"""
    <html>
    <body>
        <h2>Avengers: Doomsday - Ticket Alert</h2>

        <h3>Changes detected</h3>
        <ul>{changes_html}</ul>

        <h3>Current matching showtimes</h3>

        <table border="1" cellpadding="8">
            <tr>
                <th>Theatre</th>
                <th>Date</th>
                <th>Time</th>
                <th>Format</th>
                <th>Category</th>
                <th>Price</th>
                <th>Status</th>
            </tr>
            {''.join(rows)}
        </table>

        <p>BookMyShow Ticket Notifier</p>
    </body>
    </html>
    """

    try:

        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [RESEND_TO_EMAIL],
                "subject": "Avengers: Doomsday - Ticket Alert",
                "html": body
            },
            timeout=20
        )

        if response.status_code in (200, 201):

            print("Email notification sent.")
            return True

        print(
            f"Email failed: HTTP {response.status_code}"
        )

        print(response.text[:300])
        return False

    except requests.RequestException as error:

        print(f"Email error: {error}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 55)
    print("BMS TICKET NOTIFIER")
    print("AVENGERS: DOOMSDAY")
    print("=" * 55)

    print("Time:", datetime.now().isoformat())
    print("Theatre:", BMS_THEATRE)
    print("Format:", BMS_FORMAT)

    event_code = get_event_code()

    dates = [
        date.strip()
        for date in BMS_DATES.split(",")
        if date.strip()
    ]

    print("Event:", event_code)
    print("Dates:", dates)

    all_shows = []
    successful_dates = []

    # Retrieve each date independently.
    for date in dates:

        print(f"\nChecking {date}...")

        data = fetch_bms(event_code, date)

        if data is None:

            print("Date request failed.")
            continue

        successful_dates.append(date)

        shows = parse_shows(data, date)

        all_shows.extend(shows)

        print(f"Retrieved {len(shows)} showtimes.")

    # Never overwrite the previous state if all requests fail.
    if not successful_dates:

        print("\nERROR: No successful API responses.")
        print("Previous monitoring state preserved.")

        sys.exit(1)

    filtered = filter_shows(all_shows)

    print("\n" + "=" * 55)
    print("MATCHING SHOWTIMES:", len(filtered))
    print("=" * 55)

    for show in filtered:

        print(
            f"\n{show['venue']} | "
            f"{show['date']} | "
            f"{show['time']} | "
            f"{show['format']}"
        )

        for category in show["categories"]:

            print(
                f"  {category['name']} | "
                f"Rs.{category['price']} | "
                f"{category['status_text']}"
            )

    # If no matching shows, do not overwrite the old state.
    if not filtered:

        print("\nNo matching theatre/format showtimes.")
        print("Previous monitoring state preserved.")
        sys.exit(0)

    # Avoid saving incomplete state if some dates failed.
    if len(successful_dates) != len(dates):

        print("\nWARNING: Some dates failed.")
        print("State not updated to avoid false alerts.")
        sys.exit(1)

    old_state = load_state()
    new_state = build_state(filtered)

    changes = detect_changes(old_state, new_state)

    print("\nChanges detected:", len(changes))

    if changes:

        for change in changes:
            print(" -", change)

        if not send_email(changes, filtered):
            sys.exit(1)

    else:

        print("No availability changes.")

    save_state(new_state)

    print("\nMonitoring completed successfully.")


if __name__ == "__main__":
    main()
