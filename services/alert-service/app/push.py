"""Push notifications (issue 5.4) through Firebase Cloud Messaging, so people are reached when the app isn't open:
urgent alerts go to the traveller's caregivers, and "Where are you?" goes to the traveller's phone.

Browsers register through POST /api/devices (caregivers) and POST /api/devices/traveller (phones). Their tokens are
kept in the device-tokens table (contracts/dynamo-db-tables.md), one item per person and browser. Sending happens on
a background thread: the alert is saved and on the dashboard first, and a slow or failing Firebase never holds that
up. Messages carry data only, so each app's service worker decides how to show one and what a tap opens. Tokens that
Firebase says are gone (the browser unsubscribed) are deleted.

An alert's push is never lost, even if the replica that saved the alert dies before sending it. The push is kept on
the incident as an outbox (incidents.py): the write that saves the alert also marks its push as pending, with a lease
held by that replica. The sender claims the lease, sends, then marks the push sent. Every replica runs PushRetry,
which sends any push whose lease ran out (PUSH_LEASE_S) without it being sent: its replica died, or Firebase failed.
Claiming is a conditional write, so of several replicas exactly one takes it over. A push can still go twice if a
replica dies in the few milliseconds between Firebase accepting it and marking it sent; it has the same tag
(the incidentId), so the browser shows it as one notification.

Without FIREBASE_CREDENTIALS (the service account key file) the service runs without push and says so in the log.
"""
import logging
import os
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from app.events import alert_message
from app.incidents import _from_dynamo, _to_dynamo, lease_time, now_iso
from app.levels import IMPORTANT, URGENT, level_of

log = logging.getLogger("alert.push")

MAX_TOKENS = 500  # Firebase's limit for one multicast

# firebase-admin 7 calls `tokens` deprecated in favour of `fids`, but those are Firebase Installation IDs, sent in a
# different field. Browsers get registration tokens (getToken in the web SDK), which still go in `tokens`.
warnings.filterwarnings("ignore", message="MulticastMessage.tokens is deprecated")


class DeviceStore:
    def __init__(self, dynamodb_client, table: str):
        self.db = dynamodb_client
        self.table = table

    def register(self, user_id: str, token: str, app: str) -> None:
        """Safe to repeat: a browser that registers again just refreshes its item."""
        item = {"userId": user_id, "token": token, "app": app, "registeredAt": now_iso()}
        self.db.put_item(TableName=self.table, Item={k: _to_dynamo(v) for k, v in item.items()})

    def tokens(self, user_ids: Iterable[str]) -> list[tuple[str, str]]:
        """(userId, token) for every browser these people registered."""
        found = []
        for user_id in user_ids:
            resp = self.db.query(TableName=self.table, KeyConditionExpression="userId = :u",
                                 ExpressionAttributeValues={":u": {"S": user_id}})
            found += [(user_id, _from_dynamo(item)["token"]) for item in resp["Items"]]
        return found

    def remove(self, user_id: str, token: str) -> None:
        self.db.delete_item(TableName=self.table, Key={"userId": {"S": user_id}, "token": {"S": token}})


class FirebaseSender:
    def __init__(self, credentials_path: str):
        import firebase_admin  # here, so the service and its tests run without the package's setup
        from firebase_admin import credentials, messaging

        self._messaging = messaging
        self._app = firebase_admin.initialize_app(credentials.Certificate(credentials_path), name="careroute")

    def send(self, tokens: list[str], data: dict[str, str]) -> tuple[int, list[str]]:
        """Sends `data` to every token. Returns (how many Firebase accepted, the tokens it says no longer exist)."""
        m = self._messaging
        delivered, gone = 0, []
        for start in range(0, len(tokens), MAX_TOKENS):
            batch = tokens[start:start + MAX_TOKENS]
            message = m.MulticastMessage(tokens=batch, data=data,
                                         webpush=m.WebpushConfig(headers={"Urgency": "high", "TTL": "3600"}))
            replies = m.send_each_for_multicast(message, app=self._app).responses
            for token, reply in zip(batch, replies):
                if reply.success:
                    delivered += 1
                    continue
                if isinstance(reply.exception, (m.UnregisteredError, m.SenderIdMismatchError)):
                    gone.append(token)
                else:
                    log.warning("push to a browser failed: %s", reply.exception)
        return delivered, gone


def make_sender(credentials_path: str):
    """A FirebaseSender, or None (push off) if there's no usable key. Never stops the service starting."""
    if not credentials_path:
        log.info("push notifications are off: FIREBASE_CREDENTIALS isn't set")
        return None
    if not os.path.isfile(credentials_path):
        log.warning("push notifications are off: no key file at %s", credentials_path)
        return None
    try:
        sender = FirebaseSender(credentials_path)
    except Exception as e:
        log.warning("push notifications are off: the Firebase key couldn't be used (%s)", e)
        return None
    log.info("push notifications are on")
    return sender


class Notifier:
    """Decides who gets a push and sends it in the background."""

    def __init__(self, devices: DeviceStore, caregivers_of: Callable[[str], list[str]], sender,
                 name_of: Callable[[str], str], incidents=None):
        self.devices = devices
        self.caregivers_of = caregivers_of
        self.sender = sender
        self.name_of = name_of
        self.incidents = incidents  # IncidentStore: the outbox each alert's push is claimed from and marked sent in
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="push")
        self.run = self._pool.submit  # tests replace this to send straight away

    @property
    def enabled(self) -> bool:
        return self.sender is not None

    def incident(self, incident: dict, outcome: str) -> None:
        """A new or reopened alert, whatever its level (levels.py). Repeat detections ("updated") don't push again;
        escalation does, for the levels that escalate."""
        if self.enabled and outcome in ("created", "reopened"):
            self.run(self._send_alert, incident)

    def escalated(self, incident: dict) -> None:
        """An alert nobody has acknowledged (escalation.py). Same tag, so it replaces the earlier one."""
        if self.enabled:
            self.run(self._send_alert, incident)

    def resend(self, incident: dict) -> None:
        """A pending push whose lease ran out (PushRetry): the alert or reminder it was for, sent now."""
        if self.enabled:
            self.run(self._send_alert, incident)

    @staticmethod
    def title(incident: dict) -> str:
        """By level for a new alert, and "Still ..." for a reminder (escalationLevel 1 and up)."""
        level = level_of(incident["problemType"])
        if incident.get("escalationLevel"):
            return "Still not answered: {name}" if level == URGENT else "Still happening: {name}"
        return {URGENT: "Urgent: {name}", IMPORTANT: "Check on {name}"}.get(level, "About {name}")

    def _send_alert(self, incident: dict) -> None:
        """On the background thread. With the outbox: claim the push, send it, mark it sent. If this replica dies
        anywhere in between, the lease runs out and PushRetry on some replica sends it."""
        iid, lease = incident["incidentId"], incident.get("pushLease")
        outbox = self.incidents is not None and lease is not None
        try:
            if outbox:
                claimed = self.incidents.claim_push(iid, lease)
                if claimed is None:
                    log.info("push for %s already sent or taken over", iid)
                    return
                incident, lease = claimed, claimed["pushLease"]
                if incident["state"] != "open":
                    self.incidents.push_sent(iid, lease)  # acknowledged or resolved meanwhile: no longer needed
                    return
            traveller_id = incident["travellerId"]
            self._deliver([*self.caregivers_of(traveller_id)], {
                "kind": "alert", "travellerId": traveller_id, "incidentId": iid,
                "level": level_of(incident["problemType"]), "url": f"/#dashboard/{traveller_id}",
                "title": self.title(incident).format(name=self.name_of(traveller_id)),
                "body": alert_message(incident, self.name_of(traveller_id)),
            })
            if outbox:
                self.incidents.push_sent(iid, lease)
        except Exception:
            log.exception("push for %s failed; it's tried again once its lease runs out", iid)

    def where_asked(self, traveller_id: str, question: dict) -> None:
        """A new "Where are you?" question, for the traveller's phone. A tap opens the app, which shows it."""
        if not self.enabled:
            return

        def send():
            try:
                self._deliver([traveller_id], {
                    # No url: a tap opens the trip link the phone registered with (firebase-messaging-sw.js).
                    "kind": "where", "travellerId": traveller_id, "requestId": question["requestId"],
                    "title": "Where are you?", "body": "Your caregiver is asking. Tap to answer.",
                })
            except Exception:
                log.exception("push failed")

        self.run(send)

    def _deliver(self, recipients: list[str], payload: dict[str, str]) -> None:
        """Sends to every browser these people registered. Raises if Firebase took it from none of them, so an
        alert's push stays pending and is tried again."""
        pairs = self.devices.tokens(recipients)
        if not pairs:
            return  # nobody to tell: done
        delivered, gone = self.sender.send([token for _, token in pairs], payload)
        for user_id, token in pairs:
            if token in gone:
                self.devices.remove(user_id, token)
        log.info("pushed %s: %d of %d browser(s) accepted, %d gone", payload["kind"], delivered, len(pairs), len(gone))
        if delivered == 0 and len(gone) < len(pairs):
            raise RuntimeError("no browser accepted the push")


class PushRetry:
    """Every replica, every PUSH_RETRY_CHECK_S: sends the alert pushes whose lease ran out before they were sent.

    A lease runs out PUSH_LEASE_S after it was taken, far longer than a push takes, so a push that's on its way isn't
    sent twice. Each push is tried at most PUSH_MAX_ATTEMPTS times (Firebase refusing it for good), and only for alerts
    raised in the last ESCALATE_WITHIN_S, like escalation."""

    def __init__(self, incidents, notifier: Notifier, lease_s: float = 15, check_s: float = 5,
                 max_attempts: int = 5, within_s: float = 3600):
        self.incidents = incidents
        self.notifier = notifier
        self.lease = timedelta(seconds=lease_s)
        self.check_s = check_s
        self.max_attempts = max_attempts
        self.within = timedelta(seconds=within_s)
        self._stop = threading.Event()

    def run_once(self, now: datetime | None = None) -> list[str]:
        """Hands every overdue push to the notifier, which claims it first, so only one replica sends it. Returns
        their incident IDs."""
        now = now or datetime.now(timezone.utc)
        found = []
        for incident in self.incidents.pending_pushes(lease_time(now - self.lease), self.max_attempts):
            raised = incident.get("lastEscalatedAt") or incident.get("reopenedAt") or incident["createdAt"]
            if now - datetime.fromisoformat(raised.replace("Z", "+00:00")) > self.within:
                continue
            log.info("sending the push for %s again: its replica didn't", incident["incidentId"])
            self.notifier.resend(incident)
            found.append(incident["incidentId"])
        return found

    def run(self) -> None:
        log.info("sending pushes left unsent for %ss, every %ss", int(self.lease.total_seconds()), self.check_s)
        while not self._stop.wait(self.check_s):
            try:
                self.run_once()
            except Exception:
                log.exception("push retry check failed; trying again next time")

    def start(self) -> None:
        threading.Thread(target=self.run, name="push-retry", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
