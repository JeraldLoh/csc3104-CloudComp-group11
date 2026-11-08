// Shapes from contracts/examples/*.json. Keep these in step with the contracts folder.

export type TravellerStatus = "active" | "backgrounded";

/** contracts/examples/checkin.json */
export interface Checkin {
  tripId: string;
  travellerId: string;
  seq: number;
  timestamp: string; // ISO 8601 UTC ending in Z
  lat: number;
  lng: number;
  accuracyM: number | null;
  speedMps: number | null;
  headingDeg: number | null;
  stepIndex: number | null;
  routeVersion: number | null;
  batteryPct: number | null;
  status: TravellerStatus;
  bufferedOffline: boolean;
}

/** contracts/examples/checkin-batch.json: tripId and travellerId move up to the batch. */
export interface CheckinBatch {
  tripId: string;
  travellerId: string;
  checkins: Omit<Checkin, "tripId" | "travellerId">[];
}

/** Response from POST /api/checkins and /api/checkins/batch */
export interface CheckinAck {
  accepted: number;
  duplicates: number;
  /** How many of the sent check-ins (oldest first) the server handled. A busy server
   *  stops a batch after its time budget; the rest are sent next. Missing = all. */
  processed?: number;
}

export interface RoutePackStep {
  stepIndex: number;
  mode: "walk" | "bus" | "train" | string;
  easyRead: string;
  lat: number;
  lng: number;
  underground: boolean;
  photoUrl: string | null;
  audioUrl: string | null;
  backup: string | null;
}

/** contracts/examples/route-pack.json */
export interface RoutePack {
  packVersion: string;
  routeId: string;
  routeVersion: number;
  travellerName: string | null; // null until Profiles (M5) provides it
  steps: RoutePackStep[];
  path: [number, number][];
  caregiver: { name: string | null; phone: string | null }; // null until Profiles (M5) provides them
  messages: { somethingChanged: string; offRoute: string; help: string };
  generatedAt: string;
}

/** Who is travelling on which trip. Comes from login (issue 1.5) later; from the URL for now. */
export interface Session {
  tripId: string;
  travellerId: string;
}
