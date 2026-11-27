"""BMS Ticket Notifier — Hyderabad / ALLU Cinemas.

Checks BookMyShow showtimes for Avengers: Doomsday and emails on changes.
Configuration is supplied through environment variables (GitHub Actions Variables
and Secrets, or a local .env/environment).
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
    "https://in.bookmyshow.com/movies/hyderabad/avengers-doomsday/buytickets/ET00526287/20261217?etCodes=*&language=english&refEventCode=ET00526287",
).strip()

BMS_DATES = os.getenv(
    "BMS_DATES",
    "20261217,20261218,20261219,20261220,20261221,20261222,20261223,"
    "20261224,20261225,20261226,20261227,20261228,20261229,20261230,20261231",
).strip()

BMS_THEATRE = os.getenv("BMS_THEATRE", "ALLU Cinemas: Kokapet").strip()
BMS_FORMAT = os.getenv("BMS_FORMAT", "DOLBY CINEMA").strip()
BMS_TIME = os.getenv("BMS_TIME", "").strip()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "").strip()
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL", "onboarding@resend.dev"
).strip()

STATE_FILE = os.getenv("BMS_STATE_FILE", "bms_state.json")
REQUEST_TIMEOUT = 20

# The repository's current implementation uses the v4 endpoint and eventCode
# parameter. The previous edited file used a different v5 URL/parameter set.
API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)

REGION = {
    "code": "HYD",
    "slug": "hyderabad",
    "lat": "17.385",
    "lon": "78.487",
    "geohash": "tep",
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
# GENERAL HELPERS
# ---------------------------------------------------------------------------

def get_event_code() -> str:
    """Extract the BookMyShow event code from BMS_URL."""
    for part in urlparse(BMS_URL).path.split("/"):
        if re.fullmatch(r"ET\d{8,}", part):
            return part
    raise ValueError(
        "Could not find an event code such as ET00439706 in BMS_URL."
    )


def as_text(value: Any) -> str:
    """Convert API values (including nested format attributes) to searchable text."""
    if value is None:
        return ""
