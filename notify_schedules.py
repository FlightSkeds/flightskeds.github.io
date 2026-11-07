
"""FlightLine personalized schedule notifications.

Counts flight, simulator, ground, and duty assignments.
Never claims that a user is not scheduled.
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent
APP_ID = os.getenv("ONESIGNAL_APP_ID", "").strip()
API_KEY = os.getenv("ONESIGNAL_REST_API_KEY", "").strip()

API = "https://api.onesignal.com"

NAME_PATTERN = re.compile(
    r"([A-Z][A-Z'-]*(?: [A-Z][A-Z'-]*){0,2}),\s*"
    r"([A-Z][A-Z'-]*)"
)

DUTY_ROLES = [
    "QA Skeds/Duty OPS",
    "EVERGREEN AM WHEELS",
    "EVERGREEN AM RDO",
    "WKDAY AM DUTY DRIVER",
    "Night SDO",
    "Day SDO",
    "AM AFDO",
    "PM AFDO",
    "AM FDO",
    "PM FDO",
    "CDO",
]

ROLE_PATTERN = re.compile(
    r"^(" + "|".join(
        re.escape(role) for role in DUTY_ROLES
    ) + r")\s+(.+)$",
    re.IGNORECASE,
)

FLIGHT_PATTERN = re.compile(
    r"^\d{1,3}\s+\d{4}\s+\d{4}\s+\d{4}\s+"
)

SIM_PATTERN = re.compile(
    r"^\d{1,3}\s+\d{4}\s+\d{4}\s+"
)

FLIGHT_CODE_PATTERN = re.compile(
    r"\s([A-Z]{1,5}\d{3,5}[A-Z]?|BIS|QIP)"
    r"\s+\d+(?:\.\d+)?/\d+(?:\.\d+)?(?:\s|$)"
)

SIM_CODE_PATTERN = re.compile(
    r"\s([A-Z]{1,5}\d{3,5}[A-Z]?)"
    r"\s+\d+(?:\.\d+)?(?:\s|$)"
)


def normalize(value):
    return re.sub(
        r"\s+", " ", str(value or "").upper()
    ).strip()


def person_matches(text, first, last):
    if not last:
        return False

    for found_last, found_first in NAME_PATTERN.findall(
        normalize(text)
    ):
        if found_last != last:
            continue

        if not first or found_first == first:
            return True

        if (
            len(found_first) == 1
            and found_first == first[:1]
        ):
            return True

    return False


def duty_assignments(schedule, first, last):
    """Read duty rows from the first-page roster."""
    pages = schedule.get("pages", [])

    first_page = next(
        (
            str(page.get("text", ""))
            for page in pages
            if page.get("page") == 1
        ),
        ""
    )

    if not first_page:
        return []

    marker = "DUTY/WATCH ASSIGNED SIGN IN SIGN OUT"

    if marker not in first_page:
        return []

    duty_text = first_page.split(marker, 1)[1]
    duty_text = duty_text.split("\nKNSE ", 1)[0]

    assignments = []

    for line in duty_text.splitlines():
        line = line.strip()
        match = ROLE_PATTERN.match(line)

        if not match:
            continue

        role, assigned = match.groups()

        if person_matches(assigned, first, last):
            assignments.append(role)

    return assignments


def event_counts(schedule, first, last):
    counts = Counter()
    section = ""

    for record in schedule.get("records", []):
        line = normalize(record.get("text", ""))

        if line.startswith("FLIGHT (FLIGHT EVENT) SCHEDULE"):
            section = "flight"
            continue

        if line.startswith("SIMULATOR (SIM EVENT) SCHEDULE"):
            section = "sim"
            continue

        if line.startswith("GROUND EVENTS"):
            section = "ground"
            continue

        if section == "flight":
            if not FLIGHT_PATTERN.match(line):
                continue

            code = FLIGHT_CODE_PATTERN.search(line)

            if not code:
                continue

            # Crew appears before the event code.
            crew_text = line[:code.start()]

            if not person_matches(crew_text, first, last):
                continue

            if code.group(1).startswith("I"):
                counts["instrument"] += 1
            else:
                counts["flight"] += 1

        elif section == "sim":
            if not SIM_PATTERN.match(line):
                continue

            code = SIM_CODE_PATTERN.search(line)

            if not code:
                continue

            crew_text = line[:code.start()]

            if person_matches(crew_text, first, last):
                counts["sim"] += 1

        elif section == "ground":
            if (
                re.match(r"^\d{4}-\d{4}\s+", line)
                and person_matches(line, first, last)
            ):
                counts["ground"] += 1

    duties = duty_assignments(schedule, first, last)
    counts["duty"] = len(duties)

    return counts, duties


def plural(number, label):
    return f"{number} {label}" + (
        "" if number == 1 else "s"
    )


def make_summary(counts, duties):
    parts = []

    if counts["instrument"]:
        parts.append(
            plural(counts["instrument"], "instrument flight")
        )

    if counts["flight"]:
        parts.append(plural(counts["flight"], "flight"))

    if counts["sim"]:
        parts.append(plural(counts["sim"], "simulator"))

    if counts["ground"]:
        parts.append(
            plural(counts["ground"], "ground event")
        )

    if duties:
        if len(duties) == 1:
            parts.append(f"{duties[0]} duty")
        else:
            parts.append(
                plural(len(duties), "duty assignment")
            )

    if not parts:
        return (
            "Your squadron's schedule is posted. "
            "Check the full schedule for assignments, "
            "including duty and ground events."
        )

    if len(parts) == 1:
        description = parts[0]
    elif len(parts) == 2:
        description = " and ".join(parts)
    else:
        description = (
            ", ".join(parts[:-1]) + ", and " + parts[-1]
        )

    return (
        "You've got " + description
        + ". Open FlightLine to verify the details."
    )


def get_users():
    """Retrieve OneSignal users and their subscriptions."""
    cursor = None
    seen_cursors = set()

    while True:
        params = {"limit": 100}

        if cursor:
            params["cursor"] = cursor

        response = requests.get(
            f"{API}/apps/{APP_ID}/users",
            headers={
                "Authorization": f"Key {API_KEY}"
            },
            params=params,
            timeout=35,
        )
        response.raise_for_status()

        result = response.json()

        for user in result.get("users", []):
            yield user

        next_cursor = result.get("next_cursor")

        if (
            not next_cursor
            or next_cursor in seen_cursors
        ):
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor


def send_notification(ids, title, message, squadron, day):
    if not ids:
        return

    url = (
        "https://flightskeds.github.io/?"
        + urlencode({
            "squadron": squadron,
            "date": day
        })
    )

    for start in range(0, len(ids), 2000):
        batch = ids[start:start + 2000]

        payload = {
            "app_id": APP_ID,
            "target_channel": "push",
            "include_subscription_ids": batch,
            "headings": {"en": title},
            "contents": {"en": message},
            "url": url,
        }

        response = requests.post(
            f"{API}/notifications",
            headers={
                "Authorization": f"Key {API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=40,
        )

        response.raise_for_status()

        result = response.json()

        if result.get("errors"):
            raise RuntimeError(
                f"OneSignal returned errors: {result['errors']}"
            )

        print(
            f"Sent {title} to {len(batch)} subscriptions"
        )


def main():
    event_file = (
        ROOT / "flightline_notification_events.json"
    )

    if not event_file.exists():
        print("No notification events file")
        return

    events = json.loads(
        event_file.read_text(encoding="utf-8")
    )

    if not events:
        print("No new or revised schedules")
        return

    if not APP_ID or not API_KEY:
        sys.exit("OneSignal credentials are missing")

    subscribers = defaultdict(list)

    for user in get_users():
        properties = user.get("properties", {}) or {}
        tags = properties.get("tags", {}) or {}

        squadron = normalize(tags.get("squadron"))

        if not squadron:
            continue

        for subscription in user.get(
            "subscriptions", []
        ):
            if not subscription.get("enabled", False):
                continue

            subscription_type = normalize(
                subscription.get("type")
            )

            if subscription_type not in {
                "CHROMEPUSH",
                "FIREFOXPUSH",
                "SAFARIPUSH",
                "EDGEPUSH",
            }:
                continue

            subscription_id = subscription.get("id")

            if subscription_id:
                subscribers[squadron].append(
                    (subscription_id, tags)
                )

    for key, status in events.items():
        if "/" not in key:
            continue

        squadron, day = key.split("/", 1)

        if status not in {"downloaded", "revised"}:
            continue

        audience = subscribers.get(squadron, [])

        if not audience:
            print(f"No subscribers for {squadron}")
            continue

        title = (
            f"{squadron} schedule "
            + (
                "posted"
                if status == "downloaded"
                else "updated"
            )
        )

        if status == "revised":
            send_notification(
                [item[0] for item in audience],
                title,
                (
                    f"The {day} schedule has been revised. "
                    "Check all your assignments."
                ),
                squadron,
                day,
            )
            continue

        schedule_path = (
            ROOT / "schedules" / squadron
            / day / "full_schedule.json"
        )

        if not schedule_path.exists():
            print(f"Missing schedule: {schedule_path}")
            continue

        schedule = json.loads(
            schedule_path.read_text(encoding="utf-8")
        )

        grouped_messages = defaultdict(list)

        for subscription_id, tags in audience:
            first = normalize(tags.get("first"))
            last = normalize(tags.get("last"))

            if last and tags.get("profile") == "yes":
                counts, duties = event_counts(
                    schedule, first, last
                )
                message = make_summary(counts, duties)
            else:
                message = (
                    f"The {day} schedule is posted. "
                    "Open FlightLine to check your assignments."
                )

            grouped_messages[message].append(
                subscription_id
            )

        for message, ids in grouped_messages.items():
            send_notification(
                ids, title, message, squadron, day
            )


if __name__ == "__main__":
    main()
