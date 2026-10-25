// Incidents as the dashboard holds them, and how live events (alert-event.json) update them.
// Pure functions, no I/O, so they're easy to test (tests/incidents.test.ts).

export type Severity = "normal" | "urgent";
export type State = "open" | "acknowledged" | "resolved";

/** incident.json (contracts/examples), plus fields the Alert Service adds. */
export interface Incident {
  incidentId: string;
  travellerId: string;
  travellerName?: string;
  tripId?: string;
  stepIndex?: number | null;
  problemType: string;
  severity: Severity;
  state: State;
  detectionCount?: number;
  lastLocation?: { lat: number; lng: number } | null;
  acknowledgedBy?: string | null;
  disruption?: { message?: string; line?: string | null } | null;
  createdAt: string;
  updatedAt?: string;
}

/** alert-event.json: what GET /api/stream sends. */
export interface AlertEvent {
  event: string; // incident.opened | updated | reopened | acknowledged | resolved
  incident: Incident & { message?: string };
  sentAt: string;
}

export type IncidentMap = Record<string, Incident>;

const PROBLEM_TITLES: Record<string, string> = {
  off_route: "May have gone off route",
  wrong_direction: "May be going the wrong way",
  missed_stop: "May have missed their stop",
  long_dwell: "Hasn't moved for a while",
  no_response: "Phone has stopped responding",
  help_requested: "Asked for help",
  disruption: "Journey affected by a disruption",
  unknown: "Something may be wrong",
};

export function problemTitle(problemType: string): string {
  return PROBLEM_TITLES[problemType] ?? PROBLEM_TITLES.unknown;
}

/** Incidents loaded with GET /api/incidents. */
export function mergeIncidents(current: IncidentMap, loaded: Incident[]): IncidentMap {
  const next = { ...current };
  for (const incident of loaded) {
    const known = next[incident.incidentId];
    if (!known || !isOlder(incident, known)) next[incident.incidentId] = { ...known, ...incident };
  }
  return next;
}

/** One live event. An event older than what we already have (e.g. it arrived after a reload) is ignored. */
export function applyEvent(current: IncidentMap, event: AlertEvent): IncidentMap {
  const incident: Incident & { message?: string } = { ...event.incident };
  delete incident.message; // the card builds its own text from problemType
  const known = current[incident.incidentId];
  if (known && isOlder(incident, known)) return current;
  return { ...current, [incident.incidentId]: { ...known, ...incident } };
}

function isOlder(candidate: Incident, known: Incident): boolean {
  return !!candidate.updatedAt && !!known.updatedAt && candidate.updatedAt < known.updatedAt;
}

/** What needs attention first: unresolved before resolved, urgent before normal, then newest first. */
export function sortForDisplay(incidents: Incident[]): Incident[] {
  const rank = (i: Incident) => (i.state === "resolved" ? 2 : i.severity === "urgent" ? 0 : 1);
  return [...incidents].sort((a, b) => rank(a) - rank(b) || b.createdAt.localeCompare(a.createdAt));
}
