"""Escalation (issue 5.4): an alert nobody acknowledges is sent again, depending on its level (levels.py).

Every replica runs this loop in a background thread. Every ESCALATION_CHECK_S it looks for open incidents
ESCALATE_AFTER_S after they opened, or after their last escalation, and escalates each by one level, up to
ESCALATE_MAX times: a push to the traveller's caregivers and an incident.escalated event for the dashboards.
Acknowledging or resolving stops it; a reopened incident starts again from level 0.

  urgent      always, until acknowledged.
  important   only while the problem is still happening: the detectors reported it again in the last
              ESCALATE_STILL_DETECTED_S. long_dwell is the exception, because the dwell detector stops answering
              once someone has been still for a while: it's still happening while the phone's latest check-in is
              within DWELL_RADIUS_M of where they stopped (`still_there`). An important alert that has stopped
              isn't sent again, but is checked again on the next tick in case it starts again.
  notice      never.

Two replicas can find the same incident at the same moment. Each level is claimed with a conditional write on
escalationLevel (IncidentStore.claim_escalation), so only one of them sends it. The incidents table is the only
state, so nothing is lost when a replica restarts.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.levels import ESCALATING_TYPES, URGENT, level_of

log = logging.getLogger("alert.escalation")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Escalator:
    def __init__(self, incidents, publisher, notifier, after_s: int, max_level: int, check_s: float = 30,
                 within_s: int = 3600, still_detected_s: int = 60,
                 still_there: Callable[[dict, datetime], bool | None] | None = None):
        self.incidents = incidents
        self.publisher = publisher  # AlertPublisher: incident.escalated to dashboards
        self.notifier = notifier  # push.Notifier: the push again
        self.after = timedelta(seconds=after_s)
        self.max_level = max_level
        self.check_s = check_s
        # Only incidents opened this recently: after an outage, alerts from hours ago aren't suddenly sent again.
        self.within = timedelta(seconds=within_s)
        # An important problem counts as still happening if it was reported again this recently.
        self.still_detected = timedelta(seconds=still_detected_s)
        # For long_dwell: is the traveller still where they stopped? None = can't tell (detections decide then).
        self.still_there = still_there
        self._stop = threading.Event()

    def due(self, now: datetime) -> list[dict]:
        """Recent open incidents whose last alert (opening, reopening or escalation) is at least `after` ago, and
        that are still happening if their level asks for that."""
        def opened(i: dict) -> datetime:
            return _time(i.get("reopenedAt") or i["createdAt"])

        def last_alerted(i: dict) -> datetime:
            return _time(i["lastEscalatedAt"]) if i.get("lastEscalatedAt") else opened(i)

        return [i for i in self.incidents.open_of_types(ESCALATING_TYPES, self.max_level)
                if now - opened(i) <= self.within and now - last_alerted(i) >= self.after
                and self.still_happening(i, now)]

    def still_happening(self, incident: dict, now: datetime) -> bool:
        if level_of(incident["problemType"]) == URGENT:
            return True
        if incident["problemType"] == "long_dwell" and self.still_there:
            there = self.still_there(incident, now)
            if there is not None:
                return there
        detected = incident.get("lastDetectedAt") or incident["createdAt"]
        return now - _time(detected) <= self.still_detected

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
        log.info("escalating urgent and still-happening important alerts every %ss, up to %s times",
                 int(self.after.total_seconds()), self.max_level)
        while not self._stop.wait(self.check_s):
            try:
                self.run_once()
            except Exception:
                log.exception("escalation check failed; trying again next time")

    def start(self) -> None:
        threading.Thread(target=self.run, name="escalation", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
