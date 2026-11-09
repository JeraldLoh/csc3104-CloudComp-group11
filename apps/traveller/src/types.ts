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
  /** What the wording tells them to look out for, each with a photo (Journey Service). Missing in older packs. */
  landmarks?: Landmark[];
  /**
   * Bus and train steps: every stop on the ride, the one they get on at first and the one they get off at last,
   * so the phone can count the stops left as it passes them (src/lib/guide.ts). Missing in older packs, and a
   * stop's lat/lng is null when the plan didn't say.
   */
  stops?: PackStop[];
}

export interface PackStop {
  name: string | null;
  lat: number | null;
  lng: number | null;
}

/** A building the step's wording names. photoUrl is null when its photo would repeat another one. */
export interface Landmark {
  name: string;
  photoUrl: string | null;
  /** Where it is seen instead when there's no photo: "step" or another landmark's name. Not sent by every pack. */
  sameView?: string | null;
  /** Where the building is, and the spot on the route it is seen from (its photo is taken there). Newer packs only. */
  lat?: number | null;
  lng?: number | null;
  seenFrom?: [number, number] | null;
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
