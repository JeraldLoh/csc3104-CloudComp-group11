// Shapes from the Journey Service (services/journey-service/app/models.py and the plan
// output in journey_client.py) and contracts/examples/route.json. Keep in step with those.

export type StepMode = "walk" | "bus" | "train";
export type TravelMode = "any" | "bus" | "train";
export type SortBy = "walking" | "crowding" | "time";
export type Crowd = "low" | "moderate" | "high";

export interface LatLng {
  lat: number;
  lng: number;
}

/** [lat, lng], the order Leaflet and the Journey Service both use. */
export type LatLngTuple = [number, number];

/** One result of GET /api/places/search. */
export interface PlaceResult extends LatLng {
  name: string;
  address?: string | null;
  postalCode?: string | null;
}

export interface Stop {
  name: string | null;
  code: string | null;
  lat: number | null;
  lng: number | null;
}

/** A building the step's wording tells the traveller to look out for ("Simplify with AI"), with a photo of it. */
export interface Landmark {
  name: string;
  /** "mall", "station", "hawker centre", ... or "building". */
  kind?: string | null;
  lat: number;
  lng: number;
  /** [lat, lng] of the traveller's spot on the route it is seen from (and photographed from). */
  seenFrom?: [number, number] | null;
  /** A Street View photo of it, taken from the street, in S3. */
  photoKey?: string | null;
  /** A temporary link to that photo, made by the service for each answer (never saved). */
  photoUrl?: string | null;
  /** No photo of its own, as it would look the same as the step's photo ("step") or another landmark's (its name). */
  sameView?: string | null;
}

export interface RouteStep {
  stepIndex: number;
  mode: StepMode;
  instruction: string;
  easyRead: string | null;
  from: Stop;
  to: Stop;
  line?: string | null;
  expectedDurationS?: number | null;
  underground?: boolean | null;
  /** The step's photo in S3: a Street View photo of where it ends, or one the caregiver uploaded. */
  photoKey?: string | null;
  /** A temporary link to that photo, made by the service for each answer (never saved). */
  photoUrl?: string | null;
  audioKey?: string | null;
  backup?: { instruction: string; line?: string | null } | null;
  /** Train steps: every station on the leg, so a disruption between the two ends is noticed. */
  stops?: { code: string | null; name: string | null }[];
  /** What the wording tells the traveller to look out for. The service keeps only those the wording still names. */
  landmarks?: Landmark[] | null;
}

export interface PlanRequest {
  from: LatLng;
  to: LatLng;
  mode: TravelMode;
  sortBy: SortBy;
  departAt?: string;
  maxWalkM?: number;
  maxOptions?: number;
}

export interface PlanSummary {
  durationS: number | null;
  startTime: string | null;
  endTime: string | null;
  walkDistanceM: number;
  transfers: number | null;
  fare: number | null;
  crowd: Crowd | null;
  modes: StepMode[];
}

export interface PlanWarning {
  type: string;
  severity: "info" | "high" | string;
  stepIndex: number | null;
  message: string;
}

/** Live information for one step: a train platform's crowding, or the next buses at a stop. */
export interface LiveEntry {
  stepIndex: number;
  crowd?: Crowd | null;
  source?: string;
  station?: string;
  busStopCode?: string;
  serviceNo?: string;
  nextBuses?: { minutes: number; display: string; load?: string | null; crowd?: Crowd | null }[];
}

/** One route option from POST /api/routes/plan. */
export interface PlanOption {
  option: number;
  summary: PlanSummary;
  steps: RouteStep[];
  /** All steps joined; saved with the route. */
  path: LatLngTuple[];
  /** stepPaths[i] is the path of steps[i]. */
  stepPaths: LatLngTuple[][];
  live: LiveEntry[];
  warnings: PlanWarning[];
}

export interface RoutePlace extends LatLng {
  /** null when the place is only a map pin or coordinates. */
  name: string | null;
  safeLocationId?: string | null;
}

/** Body of POST /api/routes: route.json without routeId, version and approved. */
export interface RouteDraft {
  travellerId: string;
  caregiverId: string;
  name: string;
  origin: RoutePlace;
  destination: RoutePlace;
  steps: RouteStep[];
  path: LatLngTuple[];
}

/** Body of PUT /api/routes/{id}: the route as you last saw it, with your changes. `version` is the one you edited. */
export interface RouteUpdate extends RouteDraft {
  version: number;
}

export type TripStatus = "planned" | "active" | "completed";

/** contracts/examples/trip.json */
export interface Trip {
  tripId: string;
  routeId: string;
  /** The version of the route this trip was made with. If the route changes, the trip stops working. */
  routeVersion: number;
  travellerId: string;
  /** YYYY-MM-DD */
  date: string;
  status: TripStatus;
  startedAt: string | null;
  endedAt: string | null;
  currentStepIndex: number;
  planVersion: number;
  override: unknown;
}

/** GET /api/trips/{id}/pack: a temporary link to the offline pack the traveller's phone downloads. */
export interface PackLink {
  packUrl: string;
  packVersion: string;
}

/** contracts/examples/route.json */
export interface SavedRoute extends RouteDraft {
  routeId: string;
  version: number;
  approved: boolean;
  /** ISO 8601; set by the service. */
  updatedAt?: string;
}

/** POST /api/routes/{id}/easy-read: AI wording for each step, for the caregiver to check before saving. */
export interface EasyReadSuggestion {
  /** `fallback`: the AI's text didn't pass the checks (or the AI was unavailable), so this is the standard wording. */
  steps: { stepIndex: number; easyRead: string; fallback: boolean; landmarks?: Landmark[] }[];
  /** "template": the AI was unavailable, so every step has the standard wording. */
  provider: "ollama" | "template" | string;
}

/** POST /api/routes/{id}/photos: the uploaded photo, to put in a step's photoKey and save. */
export interface UploadedPhoto {
  photoKey: string;
  photoUrl: string | null;
}

/** "recorded" = the saved API responses (app/saved_data in the service), "live" = the real OneMap and LTA. */
export type DataSource = "live" | "recorded";

/** GET/PUT /api/dev/data-mode: only exists when the service runs with DEMO_UI=true (local demos). */
export interface DataModeInfo {
  onemap: DataSource;
  lta: DataSource;
  /** When the saved data was captured (YYYY-MM-DD); null when nothing is saved yet. */
  onemapRecordedAt: string | null;
  ltaRecordedAt: string | null;
}

/** POST /api/routes/check-disruptions: the disruption warnings for some steps, as LTA reports things now. */
export interface DisruptionCheck {
  warnings: PlanWarning[];
  /** ISO 8601, when the service checked. */
  checkedAt: string;
}
