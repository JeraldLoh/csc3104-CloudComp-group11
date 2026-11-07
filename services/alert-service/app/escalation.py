"""Escalation (issue 5.4): an urgent alert nobody acknowledges is sent again.

Every replica runs this loop in a background thread. Every ESCALATION_CHECK_S it looks for urgent incidents still
open ESCALATE_AFTER_S after they opened, or after their last escalation, and escalates each by one level, up to
ESCALATE_MAX times: a push to the traveller's caregivers ("Still not answered: ...") and an incident.escalated event
for the dashboards. Acknowledging or resolving stops it; a reopened incident starts again from level 0.

Two replicas can find the same incident at the same moment. Each level is claimed with a conditional write on
escalationLevel (IncidentStore.claim_escalation), so only one of them sends it. The incidents table is the only
state, so nothing is lost when a replica restarts.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("alert.escalation")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Escalator:
    def __init__(self, incidents, publisher, notifier, after_s: int, max_level: int, check_s: float = 30,
                 within_s: int = 3600):
        self.incidents = incidents
        self.publisher = publisher  # AlertPublisher: incident.escalated to dashboards
        self.notifier = notifier  # push.Notifier: the push again
        self.after = timedelta(seconds=after_s)
        self.max_level = max_level
        self.check_s = check_s
        # Only incidents opened this recently: after an outage, alerts from hours ago aren't suddenly sent again.
        self.within = timedelta(seconds=within_s)
        self._stop = threading.Event()

    def due(self, now: datetime) -> list[dict]:
        """Recent open urgent incidents whose last alert (opening, reopening or escalation) is at least `after` ago."""
        def opened(i: dict) -> datetime:
            return _time(i.get("reopenedAt") or i["createdAt"])

        def last_alerted(i: dict) -> datetime:
            return _time(i["lastEscalatedAt"]) if i.get("lastEscalatedAt") else opened(i)

        return [i for i in self.incidents.open_urgent(self.max_level)
                if now - opened(i) <= self.within and now - last_alerted(i) >= self.after]

    def run_once(self, now: datetime | None = None) -> list[dict]:
        """Escalates what's due. Returns the incidents this replica escalated."""
        escalated = []
        now = now or datetime.now(timezone.utc)
        stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for incident in self.due(now):
            claimed = self.incidents.claim_escalation(incident["incidentId"], incident["escalationLevel"], stamp)
            if not claimed:
                continue  # another replica took it, or a caregiver acknowledged it just now
            log.info("escalated %s to level %s", claimed["incidentId"], claimed["escalationLevel"])
            self.publisher.publish("escalated", claimed)
            self.notifier.escalated(claimed)
            escalated.append(claimed)
        return escalated

    def run(self) -> None:
        log.info("escalating urgent alerts open for %ss, up to %s times", int(self.after.total_seconds()), self.max_level)
        while not self._stop.wait(self.check_s):
            try:
                self.run_once()
            except Exception:
                log.exception("escalation check failed; trying again next time")

    def start(self) -> None:
        threading.Thread(target=self.run, name="escalation", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
