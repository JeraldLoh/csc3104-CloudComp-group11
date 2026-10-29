// Profiles (issue 5.1) on the Alert Service: the caregiver's travellers, links and safe locations.
import { call } from "../alerts/api";

export interface Traveller {
  travellerId: string;
  name: string;
  preferences?: Record<string, unknown>;
  createdBy?: string;
  createdAt?: string;
}

export interface SafeLocation {
  travellerId: string;
  locationId: string;
  name: string;
  lat: number;
  lng: number;
}

export function listTravellers(caregiverId: string): Promise<Traveller[]> {
  return call("/api/travellers", caregiverId);
}

export function addTraveller(caregiverId: string, name: string): Promise<Traveller> {
  return call("/api/travellers", caregiverId, { method: "POST", body: JSON.stringify({ name }) });
}

/** Take a traveller off the caller's list. If nobody else looks after them, their profile is deleted too. */
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

const locationsPath = (travellerId: string) => `/api/travellers/${encodeURIComponent(travellerId)}/safe-locations`;

export function listSafeLocations(caregiverId: string, travellerId: string): Promise<SafeLocation[]> {
  return call(locationsPath(travellerId), caregiverId);
}

export function addSafeLocation(caregiverId: string, travellerId: string,
                                place: { name: string; lat: number; lng: number }): Promise<SafeLocation> {
  return call(locationsPath(travellerId), caregiverId, { method: "POST", body: JSON.stringify(place) });
}
