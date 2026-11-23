"""Dispatch committed archive changes to the matching private OneSignal subscriptions."""
import csv
import gzip
import io
import json
import os
import re
import subprocess
import time
import uuid
from collections import Counter
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests

from notification_schedule import personal, read_schedule

ROOT = Path(__file__).resolve().parent
CURSOR = ROOT / ".github/notification-cursor"
APP_ID = os.getenv("ONESIGNAL_APP_ID") or "49cb48e4-3498-4e8b-aa6c-9b64dcc63329"
API = "https://api.onesignal.com"
SITE = "https://flightskeds.github.io/"


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def api(path, payload, **kwargs):
    key = os.environ["ONESIGNAL_REST_API_KEY"]
    response = requests.post(API + path, json=payload, headers={"Authorization": "Key " + key},
                             timeout=40, **kwargs)
    if not response.ok:
        raise RuntimeError(f"OneSignal {path} returned HTTP {response.status_code}")
    return response.json()


def subscriptions():
    result = api("/players/csv_export", {"extra_fields": ["notification_types"]},
                 params={"app_id": APP_ID})
    url = result["csv_file_url"]
    if urlsplit(url).scheme != "https":
        raise RuntimeError("Invalid export URL")
    for _ in range(24):
        try:
            response = requests.get(url, timeout=30)
        except requests.RequestException:
            raise RuntimeError("Private subscription export download failed") from None
        if response.status_code == 404:
            time.sleep(5)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"Private export returned HTTP {response.status_code}")
        data = response.content
        if data.startswith(b"\x1f\x8b"):
            data = gzip.decompress(data)
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        required = {"id", "tags", "device_type", "invalid_identifier", "notification_types"}
        if not required.issubset(reader.fieldnames or []):
            raise RuntimeError("Unexpected subscription export schema")
        devices = {}
        for row in reader:
            if (row["device_type"] not in {"5", "7", "8", "12", "17"} or
                    row["invalid_identifier"].lower() in {"true", "t", "1"} or
                    row["notification_types"] != "1"):
                continue
            tags = json.loads(row["tags"] or "{}") or {}
            devices[row["id"]] = tags
        return devices
    raise RuntimeError("Private export was not ready")


def label(event):
    return re.sub(r"^([A-Z]+)(\d)", r"\1 \2", event.label)


def short(event):
    return f"{label(event)} at {event.start}" if event.start else label(event)


def summary(parts, limit=170):
    visible = []
    for i, part in enumerate(parts):
        if len(", ".join(visible + [part])) > limit:
            if not visible:
                visible.append(part[:limit-2] + "…")
                i += 1
            if i < len(parts):
                visible.append(f"+{len(parts)-i} more")
            break
        visible.append(part)
    return ", ".join(visible)


def changes(before, after):
    removed = list((Counter(before) - Counter(after)).elements())
    added = list((Counter(after) - Counter(before)).elements())
    notes = []
    # Pair only mutually unique candidates, preserving duplicate event codes.
    for key in (lambda e: (e.section, e.label, e.crew), lambda e: (e.section, e.label, e.start),
                lambda e: (e.section, e.start, e.crew)):
        for old in removed[:]:
            matches = [e for e in added if key(old) == key(e)]
            if len(matches) != 1 or sum(key(e) == key(old) for e in removed) != 1:
                continue
            new = matches[0]
            removed.remove(old)
            added.remove(new)
            if old.label != new.label:
                notes.append(f"{label(old)} changed to {short(new)}")
            if old.start != new.start:
                notes.append(f"{label(new)} moved from {old.start} to {new.start}")
            for attr, title in (("brief", "brief"), ("end", "end time"), ("formation", "formation")):
                if getattr(old, attr) != getattr(new, attr):
                    notes.append(f"{label(new)} {title} changed to {getattr(new, attr) or 'unlisted'}")
            if old.crew != new.crew:
                notes.append(f"{short(new)} crew changed")
    return notes + ["removed " + short(e) for e in removed] + ["added " + short(e) for e in added]


def message(day, before, after, revision, identity=None):
    prefix = date.fromisoformat(day).strftime("%A, %B ") + str(int(day[-2:])) + " — "
    if revision:
        if before is None and not after:
            return None
        if after is None:
            if not before:
                return None
            return "Schedule Updated", prefix + "Check the revised PDF to confirm your assignments."
        if before is not None:
            diff = changes(before, after)
            if not diff:
                return None
            return "Schedule Updated", prefix + "Your schedule changed: " + summary(diff) + "."
    if after is None:
        return "Schedule Posted", prefix + "Your squadron’s schedule is posted. Check the PDF for your assignments."
    if not after:
        return "No Events", prefix + "Your squadron’s schedule is posted. You have no scheduled events."
    title = "Schedule Updated" if revision else "Schedule Posted"
    ordered = sorted(after, key=lambda e: e.start)
    details = [short(e) for e in ordered]
    if len(ordered) == 1 and identity:
        others = [f"{first.title()} {last.title()}" for last, first in ordered[0].crew
                  if (last, first) != identity]
        if others:
            details[0] += " with " + summary(others, 75)
    return title, prefix + f"You have {len(after)} event{'s' if len(after) != 1 else ''}: " + summary(details) + "."


@lru_cache(maxsize=20)
def historical_text(squadron, day):
    # Confirm an absent identity only from recent publications of the same squadron.
    start = (date.fromisoformat(day) - timedelta(days=30)).isoformat()
    folder = ROOT / "schedules" / squadron
    return "\n".join(p.read_text().upper() for p in folder.glob("*/full_schedule.txt")
                     if start <= p.parent.name < day)


def dispatch(commit, path, devices):
    _, squadron, day, _ = path.split("/")
    targets = [(device, tags) for device, tags in devices.items() if tags.get("squadron") == squadron]
    if not targets:
        return
    pdf = path.replace("full_schedule.json", "original_schedule.pdf")
    revisions = git("diff", "--name-only", commit+"^", commit, "--", pdf)
    if not revisions:
        return
    new = read_schedule(git("show", commit+":"+pdf))
    existed = bool(git("ls-tree", commit+"^", "--", pdf))
    old = read_schedule(git("show", commit+"^:"+pdf)) if existed else None
    for device, tags in targets:
        first, last = [str(tags.get(k) or "").strip().upper() for k in ("first", "last")]
        known = bool(first and last and re.search(r"(?<![A-Z'-])" + re.escape(last) +
                     r",\s*" + re.escape(first) + r"(?![A-Z'-])", historical_text(squadron, day)))
        if old and first and last:
            known |= any((last, first) in e.crew for e in old[0])
        after = personal(new, first, last, known)
        before = personal(old, first, last, known) if old else None
        notice = message(day, before, after, existed, (last, first))
        if not notice:
            continue
        title, body = notice
        result = api("/notifications", {
            "app_id": APP_ID, "include_subscription_ids": [device], "target_channel": "push",
            "idempotency_key": str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join([APP_ID, commit, path, device]))),
            "headings": {"en": "FlightLine — " + title}, "contents": {"en": body},
            "url": SITE + "?" + urlencode({"squadron": squadron, "date": day}),
        })
        if not result.get("id"):
            # A device can unsubscribe after export. Other failures must retry.
            errors = str(result.get("errors", "")).lower()
            if "not subscribed" not in errors and "invalid_subscription_ids" not in errors:
                raise RuntimeError("OneSignal did not accept a targeted notification")


def main():
    cursor = CURSOR.read_text().strip()
    commits = git("rev-list", "--first-parent", "--reverse", cursor+"..HEAD", "--",
                  ":(glob)schedules/*/*/full_schedule.json").decode().splitlines()
    devices = subscriptions() if commits else {}
    for commit in commits:
        paths = git("diff", "--diff-filter=AM", "--name-only", commit+"^", commit, "--",
                    ":(glob)schedules/*/*/full_schedule.json").decode().splitlines()
        for path in paths:
            dispatch(commit, path, devices)
        CURSOR.write_text(commit + "\n")
    print(f"Processed {len(commits)} schedule archive commits.")


if __name__ == "__main__":
    main()
