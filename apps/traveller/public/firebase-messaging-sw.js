// Push notifications (issue 5.4) when the app isn't open. Registered by src/push/push.ts with the Firebase web config
// in its address (?config=...), because a service worker can't read the app's build settings. The Alert Service sends
// data only (title, body, url, ...), so this decides how it looks and what a tap opens.
// The same file is in apps/traveller/public; keep the two alike.
importScripts(
  "https://www.gstatic.com/firebasejs/13.1.0/firebase-app-compat.js",
  "https://www.gstatic.com/firebasejs/13.1.0/firebase-messaging-compat.js",
);

const params = new URL(self.location.href).searchParams;
const config = JSON.parse(params.get("config") || "null");
// Where a tap goes when the message doesn't say: the app's page as it was opened, with everything in its address
// (the traveller's trip link). The app gives it when it registers this worker.
const start = params.get("start") || "/";

if (config) {
  firebase.initializeApp(config);
  firebase.messaging().onBackgroundMessage(({ data = {} }) => {
    // One notification per alert or question: a repeat replaces it instead of piling up.
    const tag = data.kind === "where" ? `where-${data.requestId}` : data.incidentId;
    return self.registration.showNotification(data.title || "CareRoute", {
      body: data.body, tag, renotify: true, requireInteraction: true, data: { url: data.url || start },
    });
  });
}

// A tap opens the page the message is about, or brings an open app to the front and tells it where to go: this worker
// has its own scope, so it doesn't control the app's pages and can't move them itself.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || start, self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      const open = windows.find((w) => new URL(w.url).origin === self.location.origin);
      if (!open) return self.clients.openWindow(url);
      open.postMessage({ type: "careroute-open", url });
      return open.focus();
    }),
  );
});
