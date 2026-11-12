"""Push notifications (issue 5.4) through Firebase Cloud Messaging, so people are reached when the app isn't open:
urgent alerts go to the traveller's caregivers, and "Where are you?" goes to the traveller's phone.

Browsers register through POST /api/devices (caregivers) and POST /api/devices/traveller (phones). Their tokens are
kept in the device-tokens table (contracts/dynamo-db-tables.md), one item per person and browser. Sending happens on
a background thread: the alert is saved and on the dashboard first, and a slow or failing Firebase never holds that
up. Messages carry data only, so each app's service worker decides how to show one and what a tap opens. Tokens that
Firebase says are gone (the browser unsubscribed) are deleted.

Without FIREBASE_CREDENTIALS (the service account key file) the service runs without push and says so in the log.
"""
import logging
import os
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

from app.events import alert_message
from app.incidents import _from_dynamo, _to_dynamo, now_iso
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
                 name_of: Callable[[str], str]):
        self.devices = devices
        self.caregivers_of = caregivers_of
        self.sender = sender
        self.name_of = name_of
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="push")
        self.run = self._pool.submit  # tests replace this to send straight away

    @property
    def enabled(self) -> bool:
        return self.sender is not None

    def incident(self, incident: dict, outcome: str) -> None:
        """A new or reopened alert, whatever its level (levels.py). Repeat detections ("updated") don't push again;
        escalation does, for the levels that escalate."""
        if not self.enabled or outcome not in ("created", "reopened"):
            return
        level = level_of(incident["problemType"])
        self._alert(incident, {URGENT: "Urgent: {name}", IMPORTANT: "Check on {name}"}.get(level, "About {name}"))

    def escalated(self, incident: dict) -> None:
        """An alert nobody has acknowledged (escalation.py). Same tag, so it replaces the earlier one."""
        if self.enabled:
            urgent = level_of(incident["problemType"]) == URGENT
            self._alert(incident, "Still not answered: {name}" if urgent else "Still happening: {name}")

    def _alert(self, incident: dict, title: str) -> None:
        traveller_id = incident["travellerId"]
        self.run(self._send, lambda: self.caregivers_of(traveller_id), lambda: {
            "kind": "alert", "travellerId": traveller_id, "incidentId": incident["incidentId"],
            "level": level_of(incident["problemType"]), "url": f"/#dashboard/{traveller_id}",
            "title": title.format(name=self.name_of(traveller_id)),
            "body": alert_message(incident, self.name_of(traveller_id)),
        })

    def where_asked(self, traveller_id: str, question: dict) -> None:
        """A new "Where are you?" question, for the traveller's phone. A tap opens the app, which shows it."""
        if not self.enabled:
            return
        self.run(self._send, lambda: [traveller_id], lambda: {
            # No url: a tap opens the trip link the phone registered with (firebase-messaging-sw.js).
            "kind": "where", "travellerId": traveller_id, "requestId": question["requestId"],
            "title": "Where are you?", "body": "Your caregiver is asking. Tap to answer.",
        })

    def _send(self, recipients: Callable[[], list[str]], data: Callable[[], dict[str, str]]) -> None:
        # On the background thread: the lookups are here too, so the request that triggered this never waits.
        try:
            pairs = self.devices.tokens(recipients())
            if not pairs:
                return
            payload = data()
            delivered, gone = self.sender.send([token for _, token in pairs], payload)
            for user_id, token in pairs:
                if token in gone:
                    self.devices.remove(user_id, token)
            log.info("pushed %s: %d of %d browser(s) accepted, %d gone", payload["kind"], delivered, len(pairs), len(gone))
        except Exception:
            log.exception("push failed")
