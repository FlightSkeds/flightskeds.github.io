
"""Send FlightLine schedule notifications and report OneSignal acceptance."""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode
import requests

ROOT = Path(__file__).resolve().parent
APP_ID = os.getenv('ONESIGNAL_APP_ID', '').strip()
API_KEY = os.getenv('ONESIGNAL_REST_API_KEY', '').strip()
API_URL = 'https://api.onesignal.com/notifications'
SITE_URL = 'https://flightskeds.github.io/'


def send_notification(squadron, day, status):
    if status == 'downloaded':
        title = f'{squadron} schedule posted'
        message = f'The {day} schedule is available. Check flights, simulators, ground events, and duty assignments.'
    elif status == 'revised':
        title = f'{squadron} schedule updated'
        message = f'The {day} schedule has been revised. Check all assignments for changes.'
    else:
        return

    payload = {
        'app_id': APP_ID,
        'target_channel': 'push',
        'filters': [{'field': 'tag', 'key': 'squadron', 'relation': '=', 'value': squadron}],
        'headings': {'en': title},
        'contents': {'en': message},
        'url': SITE_URL + '?' + urlencode({'squadron': squadron, 'date': day}),
    }
    response = requests.post(
        API_URL,
        headers={'Authorization': f'Key {API_KEY}', 'Content-Type': 'application/json'},
        json=payload,
        timeout=40,
    )
    try:
        result = response.json()
    except ValueError:
        raise RuntimeError(f'OneSignal returned HTTP {response.status_code} with non-JSON response')
    if not response.ok or result.get('errors'):
        raise RuntimeError(f'OneSignal HTTP {response.status_code}: {result.get("errors", result.get("message", "request rejected"))}')
    notification_id = result.get('id')
    recipients = result.get('recipients')
    print(f'OneSignal request accepted: {squadron} {day} ({status}); id={notification_id}; recipients={recipients}')
    if not notification_id:
        raise RuntimeError('OneSignal did not return a notification ID')
    if recipients == 0:
        print(f'WARNING: No recipients matched {squadron}; verify OneSignal subscription and squadron tag', file=sys.stderr)


def main():
    events_file = ROOT / 'flightline_notification_events.json'
    if not events_file.exists():
        print('No notification event file.')
        return
    events = json.loads(events_file.read_text(encoding='utf-8'))
    if not events:
        print('No new or revised schedules.')
        return
    if not APP_ID or not API_KEY:
        sys.exit('Missing ONESIGNAL_APP_ID or ONESIGNAL_REST_API_KEY')
    failures = []
    for key, status in events.items():
        if '/' not in key or status not in ('downloaded', 'revised'):
            print(f'Skipping invalid event: {key}')
            continue
        squadron, day = key.split('/', 1)
        schedule_file = ROOT / 'schedules' / squadron / day / 'full_schedule.json'
        if not schedule_file.is_file():
            print(f'Skipping {key}: schedule JSON missing')
            continue
        try:
            send_notification(squadron, day, status)
        except Exception as error:
            failures.append(key)
            print(f'Notification failed for {key}: {error}', file=sys.stderr)
    if failures:
        sys.exit('Failed notifications: ' + ', '.join(failures))


if __name__ == '__main__':
    main()
