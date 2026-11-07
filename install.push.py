
"""Install FlightLine push integration without replacing existing code."""

from pathlib import Path

APP_ID = "49cb48e4-3498-4e8b-aa6c-9b64dcc63329"

html_path = Path("index.html")
html = html_path.read_text(encoding="utf-8")

if "flightline-push-integration-v1" not in html:
    panel = """
  <div class="panel" id="flightlinePushPanel">
    <h2>Schedule Notifications</h2>
    <p class="muted small">
      Receive an alert when your squadron posts a schedule.
      Your saved profile can personalize the notification.
    </p>
    <button id="flightlineEnablePush" class="primary"
      style="width:100%">
      Enable Notifications
    </button>
    <p id="flightlinePushStatus" class="hint">
      Notifications are optional.
    </p>
  </div>
"""

    marker = "</section>\n\n</main>"
    if marker not in html:
        raise RuntimeError("Cannot locate Profile Settings section")

    html = html.replace(
        marker, panel + "</section>\n\n</main>", 1
    )

    sdk = """
<script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
<script id="flightline-push-integration-v1">
window.OneSignalDeferred = window.OneSignalDeferred || [];
let flightlinePushReady = false;

function flightlinePushStatus(message) {
  const element = document.getElementById('flightlinePushStatus');
  if (element) element.textContent = message;
}

async function flightlineSyncPushProfile() {
  if (!flightlinePushReady || !window.OneSignal || !profile) return;
  if (!OneSignal.User.PushSubscription.optedIn) return;

  try {
    await OneSignal.User.addTags({
      squadron: String(profile.squadron || ''),
      last: String(profile.last || '').trim().toUpperCase(),
      first: String(profile.first || '').trim().toUpperCase(),
      profile: 'yes'
    });
  } catch (error) {
    console.warn('FlightLine push profile:', error);
  }
}

OneSignalDeferred.push(async function(OneSignal) {
  try {
    await OneSignal.init({
      appId: '49cb48e4-3498-4e8b-aa6c-9b64dcc63329',
      serviceWorkerPath: 'push/OneSignalSDKWorker.js',
      serviceWorkerParam: {scope: '/push/'}
    });

    flightlinePushReady = true;
    await flightlineSyncPushProfile();

    const button = document.getElementById('flightlineEnablePush');

    if (button) {
      button.onclick = async () => {
        try {
          if (!profile) {
            flightlinePushStatus('Save your profile first.');
            return;
          }

          await OneSignal.Notifications.requestPermission();

          if (!OneSignal.Notifications.permission) {
            flightlinePushStatus(
              'Notifications blocked. Check Android app settings.'
            );
            return;
          }

          await OneSignal.User.PushSubscription.optIn();
          await flightlineSyncPushProfile();

          flightlinePushStatus(
            'Notifications enabled for ' + profile.squadron + '.'
          );
        } catch (error) {
          flightlinePushStatus(
            'Notification setup failed: ' + error.message
          );
        }
      };
    }

    if (OneSignal.User.PushSubscription.optedIn) {
      flightlinePushStatus('Notifications enabled.');
    }
  } catch (error) {
    flightlinePushStatus('Push setup error: ' + error.message);
  }
});
</script>
"""

    if "</body>" not in html:
        raise RuntimeError("Cannot find closing body tag")

    html = html.replace("</body>", sdk + "</body>", 1)

    html = html.replace(
        "  saveLocal(KEY,profile);\n  setTheme();",
        "  saveLocal(KEY,profile);\n"
        "  flightlineSyncPushProfile();\n"
        "  setTheme();",
        1
    )

    html = html.replace(
        "    saveLocal(KEY,profile);\n    fillSettings();",
        "    saveLocal(KEY,profile);\n"
        "    flightlineSyncPushProfile();\n"
        "    fillSettings();",
        1
    )

    html_path.write_text(html, encoding="utf-8")
    print("Updated index.html")

worker = Path("push/OneSignalSDKWorker.js")
worker.parent.mkdir(parents=True, exist_ok=True)
worker.write_text(
    'importScripts("https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.sw.js");\n',
    encoding="utf-8"
)
print("Created OneSignal service worker")

checker = Path("fetch_schedules.py")
source = checker.read_text(encoding="utf-8")

if "flightline_notification_events.json" not in source:
    anchor = (
        '    print("\\n" + "=" * 60)\n'
        '    print("SCHEDULE CHECK SUMMARY")'
    )

    if anchor not in source:
        raise RuntimeError("Schedule checker anchor not found")

    insertion = """
    (ROOT / "flightline_notification_events.json").write_text(
        json.dumps({
            key: status for key, status in outcomes.items()
            if status in ("downloaded", "revised")
        }, indent=2),
        encoding="utf-8"
    )

"""

    source = source.replace(anchor, insertion + anchor, 1)
    checker.write_text(source, encoding="utf-8")
    print("Updated fetch_schedules.py")

workflow = Path(".github/workflows/check-schedules.yml")
content = workflow.read_text(encoding="utf-8")

if "Send FlightLine notifications" not in content:
    anchor = "      - name: Commit downloaded schedules\n"

    if anchor not in content:
        raise RuntimeError("Schedule workflow anchor not found")

    addition = """      - name: Send FlightLine notifications
        if: success()
        env:
          ONESIGNAL_APP_ID: ${{ vars.ONESIGNAL_APP_ID }}
          ONESIGNAL_REST_API_KEY: ${{ secrets.ONESIGNAL_REST_API_KEY }}
        run: python notify_schedules.py

"""

    content = content.replace(anchor, addition + anchor, 1)
    workflow.write_text(content, encoding="utf-8")
    print("Updated schedule workflow")

print("FlightLine push installation complete")
