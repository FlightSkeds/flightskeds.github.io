
"""Personalized FlightLine schedule notifications via OneSignal."""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import requests

APP_ID = os.getenv("ONESIGNAL_APP_ID", "").strip()
API_KEY = os.getenv("ONESIGNAL_REST_API_KEY", "").strip()
ROOT = Path(__file__).resolve().parent
API = "https://api.onesignal.com"

HEADERS = {
    "Authorization": f"Key {API_KEY}",
    "Content-Type": "application/json"
}

NAME_PATTERN = re.compile(
    r"([A-Z][A-Z'-]*(?: [A-Z][A-Z'-]*){0,2}),\s*"
    r"([A-Z][A-Z'-]*)"
)


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "").upper()).strip()


def users():
    cursor = None

    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        response = requests.get(
            f"{API}/apps/{APP_ID}/users",
            headers=HEADERS,
            params=params,
            timeout=35
        )
        response.raise_for_status()

        payload = response.json()

        for user in payload.get("users", []):
            yield user

        next_cursor = payload.get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor


def matches_person(text, first, last):
    for found_last, found_first in NAME_PATTERN.findall(
        normalize(text)
    ):
        if found_last != last:
            continue

        if (
            not first
            or found_first == first
            or (len(found_first) == 1 and found_first == first[:1])
        ):
            return True

    return False


def event_counts(schedule, first, last):
    counts = Counter()
    section = ""

    for record in schedule.get("records", []):
        line = normalize(record.get("text"))

        if line.startswith("FLIGHT (FLIGHT EVENT) SCHEDULE"):
            section = "flight"
            continue

        if line.startswith("SIMULATOR (SIM EVENT) SCHEDULE"):
            section = "sim"
            continue

        if line.startswith("GROUND EVENTS"):
            section = "ground"
            continue

        if not matches_person(line, first, last):
            continue

        if section == "flight":
            if not re.match(
                r"^\d{1,3}\s+\d{4}\s+\d{4}\s+\d{4}\s+",
                line
            ):
                continue

            code = re.search(
                r"\b([A-Z]{1,5}\d{3,5}[A-Z]?|BIS|QIP)\b",
                line
            )

            if code and code.group(1).startswith("I"):
                counts["instrument"] += 1
            else:
                counts["flight"] += 1

        elif section == "sim":
            if re.match(
                r"^\d{1,3}\s+\d{4}\s+\d{4}\s+",
                line
            ):
                counts["sim"] += 1

        elif section == "ground":
            if re.match(r"^\d{4}-\d{4}\s+", line):
                counts["ground"] += 1

    # Preserve newlines so each duty assignment is counted separately.
    first_page = next(
        (
            str(page.get("text", ""))
            for page in schedule.get("pages", [])
            if page.get("page") == 1
        ),
        ""
    )

    roster = first_page.split(
        "DUTY/WATCH ASSIGNED SIGN IN SIGN OUT", 1
    )

    if len(roster) == 2:
        duty_block = roster[1].split("KNSE ", 1)[0]

        for line in duty_block.splitlines():
            if matches_person(line, first, last):
                counts["duty"] += 1

    return counts


def summary(counts):
    labels = [
        ("instrument", "instrument flight"),
        ("flight", "flight"),
        ("sim", "simulator"),
        ("ground", "ground event"),
        ("duty", "duty assignment")
    ]

    parts = []

    for key, label in labels:
        number = counts[key]

        if number:
            parts.append(
                f"{number} {label}"
                + ("" if number == 1 else "s")
            )

    if not parts:
        return (
            "Your squadron schedule is posted. "
            "Check your assignments, including duty and ground events."
        )

    if len(parts) == 1:
        description = parts[0]
    else:
        description = ", ".join(parts[:-1]) + " and " + parts[-1]

    return (
        "You've got " + description
        + ". Check the full schedule for details."
    )


def send(subscription_ids, title, message, day, squadron):
    for offset in range(0, len(subscription_ids), 2000):
        batch = subscription_ids[offset:offset + 2000]

        body = {
            "app_id": APP_ID,
            "target_channel": "push",
            "include_subscription_ids": batch,
            "headings": {"en": title},
            "contents": {"en": message},
            "url": (
                "https://flightskeds.github.io/"
                f"?squadron={squadron}&date={day}"
            )
        }

        response = requests.post(
            f"{API}/notifications",
            headers=HEADERS,
            json=body,
            timeout=40
        )
        response.raise_for_status()

        print(
            f"Notification submitted for {len(batch)} "
            f"subscription(s): {title}"
        )


def main():
    events_file = ROOT / "flightline_notification_events.json"

    if not events_file.exists():
        print("No schedule publication events")
        return

    events = json.loads(events_file.read_text(encoding="utf-8"))

    if not events:
        print("No new or revised schedules")
        return

    if not APP_ID or not API_KEY:
        print("OneSignal credentials missing", file=sys.stderr)
        sys.exit(1)

    audience = defaultdict(list)

    for user in users():
        tags = user.get("properties", {}).get("tags", {}) or {}
        squadron = normalize(tags.get("squadron"))

        if not squadron:
            continue

        for subscription in user.get("subscriptions", []):
            if subscription.get("type") not in (
                "ChromePush", "FirefoxPush",
                "SafariPush", "EdgePush"
            ):
                continue

            if not subscription.get("enabled", False):
                continue

            subscription_id = subscription.get("id")

            if subscription_id:
                audience[squadron].append(
                    (subscription_id, tags)
                )

    for path, status in events.items():
        squadron, day = path.split("/", 1)
        entries = audience.get(squadron, [])

        if not entries:
            continue

        title = (
            f"{squadron} schedule "
            + ("posted" if status == "downloaded" else "updated")
        )

        if status == "revised":
            send(
                [subscription_id for subscription_id, _ in entries],
                title,
                f"The {day} schedule has been revised.",
                day,
                squadron
            )
            continue

        schedule_file = (
            ROOT / "schedules" / squadron
            / day / "full_schedule.json"
        )

        if not schedule_file.exists():
            continue

        schedule = json.loads(
            schedule_file.read_text(encoding="utf-8")
        )

        grouped = defaultdict(list)

        for subscription_id, tags in entries:
            first = normalize(tags.get("first"))
            last = normalize(tags.get("last"))

            if last and tags.get("profile") == "yes":
                message = summary(
                    event_counts(schedule, first, last)
                )
            else:
                message = (
                    f"The {day} schedule is now available. "
                    "Open FlightLine to check your assignments."
                )

            grouped[message].append(subscription_id)

        for message, subscription_ids in grouped.items():
            send(
                subscription_ids,
                title,
                message,
                day,
                squadron
            )


if __name__ == "__main__":
    main()
  
