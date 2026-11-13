// Push notifications (issue 5.4): urgent alerts reach the caregiver when the dashboard isn't open. The browser asks
// for permission once, Firebase gives this browser a token, and the Alert Service keeps it (POST /api/devices).
// public/firebase-messaging-sw.js shows a notification when no dashboard is open; with one open, this file shows it,
// even while it's on screen, so an alert isn't missed by a caregiver looking at another traveller.
// Without firebase-web-config.json push is "off" and nothing here runs.
import { initializeApp, type FirebaseApp } from "firebase/app";
import { getMessaging, getToken, isSupported, onMessage } from "firebase/messaging";
import { call } from "../alerts/api";

export type PushState = "off" | "unsupported" | "ask" | "blocked" | "on" | "server-off" | "failed";

const config = __FIREBASE__;
const SCOPE = "/firebase-cloud-messaging-push-scope"; // its own scope, so it never replaces another service worker
let app: FirebaseApp | null = null;
let listening = false;

/** Where push stands in this browser, before anything is registered. */
export async function pushState(): Promise<PushState> {
  if (!config) return "off";
  if (typeof Notification === "undefined" || !("serviceWorker" in navigator) || !(await isSupported())) return "unsupported";
  if (Notification.permission === "denied") return "blocked";
  return Notification.permission === "granted" ? "on" : "ask";
}

/** Asks for permission if needed, then registers this browser for the caregiver. Call from a click. */
export async function turnOnPush(caregiverId: string): Promise<PushState> {
  if (!config) return "off";
  const permission = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
  if (permission !== "granted") return permission === "denied" ? "blocked" : "ask";
  try {
    // A service worker can't read the app's build settings, so it gets the config in its address.
    const { vapidKey, ...web } = config;
    const worker = await navigator.serviceWorker.register(
      `/firebase-messaging-sw.js?${new URLSearchParams({ config: JSON.stringify(web), start: "/#dashboard" })}`,
      { scope: SCOPE });
    app ??= initializeApp(web, "careroute");
    const messaging = getMessaging(app);
    const token = await getToken(messaging, { vapidKey, serviceWorkerRegistration: worker });
    if (!listening) {
      listening = true;
      // A dashboard is open, so Firebase gives the message to the page instead of the service worker: show it here,
      // as the worker would. A reminder has the same tag, so it replaces the earlier one and pops up again (renotify).
      onMessage(messaging, ({ data }) => {
        if (!data) return;
        void worker.showNotification(data.title ?? "CareRoute", {
          body: data.body, tag: data.incidentId, renotify: true, requireInteraction: true,
          data: { url: data.url ?? "/" },
        } as NotificationOptions);
      });
    }
    const saved = await call<{ pushEnabled: boolean }>("/api/devices", caregiverId,
      { method: "POST", body: JSON.stringify({ token }) });
    return saved.pushEnabled ? "on" : "server-off";
  } catch (e) {
    console.warn("couldn't turn on push notifications", e);
    return "failed";
  }
}
