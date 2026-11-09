// Push notifications (issue 5.4): "Where are you?" reaches the phone when the app isn't open. Permission is asked
// when the traveller presses "Start my trip" (browsers only allow asking after a tap). The Alert Service keeps the
// phone's token (POST /api/devices/traveller). public/firebase-messaging-sw.js shows the notification when the app is
// closed; a tap opens the app, which shows the question as usual. With the app open, a push makes it look for the
// question straight away (WHERE_PUSHED) instead of at its next check.
// Without firebase-web-config.json, or on a page that isn't secure (a phone on http://192.168...), push is off.
import { initializeApp, type FirebaseApp } from "firebase/app";
import { getMessaging, getToken, isSupported, onMessage } from "firebase/messaging";

export const WHERE_PUSHED = "careroute:where-pushed";

const config = __FIREBASE__;
const SCOPE = "/firebase-cloud-messaging-push-scope"; // its own scope, so it never replaces the offline worker
let app: FirebaseApp | null = null;
let listening = false;

function possible(): boolean {
  return !!config && typeof Notification !== "undefined" && "serviceWorker" in navigator && isSecureContext;
}

/** Asks to show notifications. Call straight from the tap, before anything is awaited. */
export async function askToNotify(): Promise<boolean> {
  if (!possible()) return false;
  if (Notification.permission !== "default") return Notification.permission === "granted";
  return (await Notification.requestPermission()) === "granted";
}

/** This phone's token if it may show notifications, or null. Never throws: the trip works without push. */
export async function pushToken(): Promise<string | null> {
  if (!possible() || Notification.permission !== "granted" || !(await isSupported().catch(() => false))) return null;
  try {
    const { vapidKey, ...web } = config!;
    // A service worker can't read the app's build settings, so it gets the config in its address, with this page's
    // own address (the trip link) for a tap to open again.
    const worker = await navigator.serviceWorker.register(
      `/firebase-messaging-sw.js?${new URLSearchParams({ config: JSON.stringify(web), start: location.href })}`,
      { scope: SCOPE });
    app ??= initializeApp(web, "careroute");
    const messaging = getMessaging(app);
    const token = await getToken(messaging, { vapidKey, serviceWorkerRegistration: worker });
    if (!listening) {
      listening = true;
      onMessage(messaging, ({ data }) => {
        if (data?.kind === "where") window.dispatchEvent(new Event(WHERE_PUSHED));
      });
    }
    return token;
  } catch (e) {
    console.warn("couldn't set up push notifications", e);
    return null;
  }
}
