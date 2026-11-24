// Android can start this worker while every FlightLine window is closed.
if (/Android/i.test(self.navigator.userAgent)) {
  const registration = self.registration;
  const display = registration.showNotification.bind(registration);
  const earlyDisplays = new Map();

  // Keep OneSignal's receipt and click handlers. When its normal display path
  // catches up, reuse the native display request for the same notification.
  registration.showNotification = function(title, options) {
    const id = options?.data?.notificationId;
    const pending = earlyDisplays.get(id);
    if (!pending) return display(title, options);
    earlyDisplays.delete(id);
    return pending.catch(() => display(title, options));
  };

  self.addEventListener('push', event => {
    let payload;
    try {
      payload = event.data?.json();
    } catch {
      return;
    }
    const custom = payload?.custom;
    if (!custom?.i || typeof payload.alert !== 'string' || typeof payload.title !== 'string') return;
    if (custom.a?.flightline_alert !== 'schedule' && payload.title !== 'FlightLine — Test Notification') return;
    if (!custom.u || payload.o?.length || earlyDisplays.has(custom.i)) return;
    let link;
    try {
      link = new URL(custom.u);
    } catch {
      return;
    }
    if (link.origin !== self.location.origin || link.pathname !== '/') return;

    const data = {notificationId: custom.i, title: payload.title, body: payload.alert,
      additionalData: custom.a, launchURL: link.href, confirmDelivery: custom.rr === 'y'};
    const pending = display(payload.title, {
      body: payload.alert, data, tag: custom.i, renotify: true,
      silent: false, vibrate: [200, 100, 200], requireInteraction: true
    });
    earlyDisplays.set(custom.i, pending);
    // Bound retained entries even if the SDK stops before its display call.
    if (earlyDisplays.size > 128) earlyDisplays.delete(earlyDisplays.keys().next().value);
    event.waitUntil(pending.catch(() => {})); // SDK retries using its original options.
  });
}

importScripts("https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.sw.js");
