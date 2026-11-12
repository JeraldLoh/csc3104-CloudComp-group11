"""How much each problem type matters to a caregiver (issue 5.4): every new alert is pushed once, and the level
decides whether it's sent again when nobody acknowledges it (escalation.py).

  urgent      the traveller asked for help, their phone went silent, or the detectors agree something is wrong
              but not what. Sent again every ESCALATE_AFTER_S, up to ESCALATE_MAX times, until acknowledged.
  important   a problem the detectors see on the journey. Sent again the same way, but only while it's still
              happening, so a traveller who has rejoined the route isn't reported again (escalation.py).
  notice      worth knowing, not about the traveller's safety: our detectors can't check on them for a while,
              or a disruption on the route. Pushed once, never sent again.

Only urgent incidents have severity "urgent"; the rest are "normal" (incident.json), so the dashboard and other
readers of severity keep their meaning. An incident also keeps its `level`.
"""
URGENT, IMPORTANT, NOTICE = "urgent", "important", "notice"

LEVELS = {
    "help_requested": URGENT,
    "no_response": URGENT,
    "unknown": URGENT,
    "off_route": IMPORTANT,
    "wrong_direction": IMPORTANT,
    "missed_stop": IMPORTANT,
    "long_dwell": IMPORTANT,
    "tracking_lost": NOTICE,
    "disruption": NOTICE,
}


def level_of(problem_type: str) -> str:
    """A type this table doesn't know yet is treated as urgent: better one reminder too many than a missed one."""
    return LEVELS.get(problem_type, URGENT)


def severity_of(problem_type: str) -> str:
    return "urgent" if level_of(problem_type) == URGENT else "normal"


# The problem types escalation looks at.
ESCALATING_TYPES = sorted(t for t, level in LEVELS.items() if level in (URGENT, IMPORTANT))
