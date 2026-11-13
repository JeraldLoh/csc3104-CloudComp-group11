// Profiles (issue 5.1) on the Alert Service: the caregiver's travellers, links and safe locations.
import { call } from "../alerts/api";
import type { WhereRequest } from "../alerts/incidents";

export interface Traveller {
  travellerId: string;
  name: string;
  preferences?: Record<string, unknown>;
  createdBy?: string;
  createdAt?: string;
  /** Only when just added with logins on (issue 1.5): the phone's login. null if it couldn't be made. */
  login?: TravellerLogin | null;
}

/** How the traveller's phone signs in: their ID and a setup code (the password). Shown once. */
export interface TravellerLogin {
  username: string;
  setupCode: string;
}

export interface SafeLocation {
  travellerId: string;
  locationId: string;
  name: string;
  lat: number;
  lng: number;
}

/** Asks the traveller "Where are you?" on their phone. A question already waiting is returned, not repeated. */
export function askWhere(caregiverId: string, travellerId: string): Promise<WhereRequest> {
  return call(`/api/travellers/${encodeURIComponent(travellerId)}/where-are-you`, caregiverId, { method: "POST" });
}

/** The traveller's latest question, waiting or answered, or null. */
export function latestWhere(caregiverId: string, travellerId: string): Promise<WhereRequest | null> {
  return call(`/api/travellers/${encodeURIComponent(travellerId)}/where-are-you`, caregiverId);
}

/** A traveller's latest trip, from their phone's check-ins. */
export interface LiveTrip {
  tripId: string;
  /** Counted from 0: the phone's "Step 1 of 4" is 0. */
  stepIndex: number | null;
  status: string | null;
  lastSeenAt: string;
  /** When they pressed "I've arrived", if they have. */
  arrivedAt: string | null;
  lat: number;
  lng: number;
  accuracyM: number | null;
  batteryPct: number | null;
  /** Their last few positions, oldest first. */
  trail: [number, number][];
}

/** Only travellers with a recent check-in are in the answer. */
export function fetchLive(caregiverId: string): Promise<Record<string, LiveTrip>> {
  return call("/api/travellers/live", caregiverId);
}

export function listTravellers(caregiverId: string): Promise<Traveller[]> {
  return call("/api/travellers", caregiverId);
}

export function addTraveller(caregiverId: string, name: string): Promise<Traveller> {
  return call("/api/travellers", caregiverId, { method: "POST", body: JSON.stringify({ name }) });
}

/** Take a traveller off the caller's list. If nobody else looks after them, their profile is deleted too. */
/** A new setup code for the traveller's phone; the old one stops working (logins on only). */
export function newSetupCode(caregiverId: string, travellerId: string): Promise<TravellerLogin> {
  return call(`/api/travellers/${encodeURIComponent(travellerId)}/login`, caregiverId, { method: "POST" });
}

export function removeTraveller(caregiverId: string, travellerId: string): Promise<{ profileDeleted: boolean }> {
  return call(`/api/travellers/${encodeURIComponent(travellerId)}`, caregiverId, { method: "DELETE" });
}

/** Add another caregiver to a traveller; only works if `caregiverId` (the caller) is linked already. */
export function addCaregiver(caregiverId: string, travellerId: string, otherCaregiverId: string): Promise<unknown> {
  return call("/api/links", caregiverId, {
    method: "POST",
    body: JSON.stringify({ travellerId, caregiverId: otherCaregiverId }),
  });
}

/** Every caregiver linked to a traveller, the caller included. */
export function listCaregivers(caregiverId: string, travellerId: string): Promise<string[]> {
  return call(`/api/travellers/${encodeURIComponent(travellerId)}/caregivers`, caregiverId);
}

const locationsPath = (travellerId: string) => `/api/travellers/${encodeURIComponent(travellerId)}/safe-locations`;

export function listSafeLocations(caregiverId: string, travellerId: string): Promise<SafeLocation[]> {
  return call(locationsPath(travellerId), caregiverId);
}

export function addSafeLocation(caregiverId: string, travellerId: string,
                                place: { name: string; lat: number; lng: number }): Promise<SafeLocation> {
  return call(locationsPath(travellerId), caregiverId, { method: "POST", body: JSON.stringify(place) });
}
