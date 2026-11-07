
"""FlightLine schedule notifications using OneSignal tags.

Sends one notification per squadron when a new or revised
schedule is detected. Does not require listing OneSignal users.

The notification explicitly includes duty and ground events.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parent

APP_ID = os.environ.get("ONESIGNAL_APP_ID", "").strip()
API_KEY = os.environ.get(
    "ONESIGNAL_REST_API_KEY", ""
).strip()

API_URL = "https://api.onesignal.com/notifications"

SITE_URL = "https://flightskeds.github.io/"


def send_notification(squadron, day, status):
    """Send to subscribed users tagged with this squadron."""

    if status == "downloaded":
        title = f"{squadron} schedule posted"
        message = (
            f"The {day} schedule is available. "
            "Check your flights, simulators, "
            "ground events, and duty assignments."
        )

    elif status == "revised":
        title = f"{squadron} schedule updated"
        message = (
            f"The {day} schedule has been revised. "
            "Check all assignments, including duty "
            "and ground events, for changes."
        )

    else:
        return

    destination = SITE_URL + "?" + urlencode({
        "squadron": squadron,
        "date": day,
    })

    payload = {
        "app_id": APP_ID,
        "target_channel": "push",
        "filters": [
            {
                "field": "tag",
                "key": "squadron",
                "relation": "=",
                "value": squadron,
            }
        ],
        "headings": {
            "en": title
        },
        "contents": {
            "en": message
        },
        "url": destination,
    }

    response = requests.post(
        API_URL,
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
            f"OneSignal error: {result['errors']}"
        )

    print(
        f"OneSignal accepted: {squadron} {day} "
        f"({status}); response: {result.get('id')}"
    )


def main():
    events_file = (
        ROOT / "flightline_notification_events.json"
    )

    if not events_file.exists():
        print("No notification event file.")
        return

    events = json.loads(
        events_file.read_text(encoding="utf-8")
    )

    if not events:
        print("No new or revised schedules.")
        return

    if not APP_ID or not API_KEY:
        sys.exit(
            "Missing ONESIGNAL_APP_ID or "
            "ONESIGNAL_REST_API_KEY"
        )

    failures = []

    for key, status in events.items():
        if "/" not in key:
            print(f"Skipping invalid event: {key}")
            continue

        if status not in ("downloaded", "revised"):
            continue

        squadron, day = key.split("/", 1)

        # Do not send a notification unless the schedule
        # was successfully downloaded and parsed.
        schedule_file = (
            ROOT
            / "schedules"
            / squadron
            / day
            / "full_schedule.json"
        )

        if not schedule_file.is_file():
            print(
                f"Skipping {key}: schedule JSON missing"
            )
            continue

        try:
            send_notification(squadron, day, status)

        except Exception as error:
            failures.append(key)
            print(
                f"Notification failed for {key}: {error}",
                file=sys.stderr,
            )

    if failures:
        sys.exit(
            "Failed notifications: "
            + ", ".join(failures)
        )


if __name__ == "__main__":
    main()
