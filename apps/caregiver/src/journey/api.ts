// For talking to the Journey Planner service (the backend). The service is in apps/journey-service. 
// It is a REST API, and the types here match the JSON it sends and receives. 
import type {
  DataModeInfo, DataSource, DisruptionCheck, EasyReadSuggestion, PackLink, PlaceResult, PlanOption, PlanRequest,
  BridgePreview, LatLngTuple, Replan, RouteDraft, RouteStep, RouteUpdate, SavedRoute, Trip, TripLocation, TripPlan, UploadedPhoto,
} from "./types";

/** What the route planner needs from the backend. */
export interface PlannerApi {
  searchPlaces(query: string): Promise<PlaceResult[]>;
  plan(request: PlanRequest): Promise<PlanOption[]>;
  createRoute(draft: RouteDraft): Promise<SavedRoute>;
}

/** What the saved-routes list needs from the backend. */
export interface SavedRoutesApi {
  /** A traveller's routes, most recently changed first (only those the caller may see). */
  listRoutes(travellerId: string): Promise<SavedRoute[]>;
  getRoute(routeId: string): Promise<SavedRoute>;
  /** Only the route's caregiver may approve. Approving twice is fine. */
  approveRoute(routeId: string): Promise<SavedRoute>;
  /**
   * Replaces the route's content. The version goes up by one and the approval is cleared. If someone
   * saved since `update.version` the service answers 409 (an ApiError) instead of overwriting.
   */
  updateRoute(routeId: string, update: RouteUpdate): Promise<SavedRoute>;
  /**
   * Removes the route. Only its caregiver may. With `version`, a route that has changed since is
   * refused (409) instead of deleted. Refused too while one of its trips is under way (409).
   */
  deleteRoute(routeId: string, version?: number): Promise<void>;
  /**
   * AI wording for every step, for the caregiver to check and edit. Nothing is saved. Slow (the AI runs on
   * CPU); when the AI is unavailable the answer has the standard wording and `provider: "template"`.
   */
  suggestEasyRead(routeId: string): Promise<EasyReadSuggestion>;
  /** The longest wording one step may have: the service's own setting (EASYREAD_MAX_CHARS), so the editor matches it. */
  getEasyReadLimits?(): Promise<{ maxChars: number }>;
  /** Stores a photo for a step (JPEG, PNG or WebP, up to 5 MB). The route only changes when it is saved with the key. */
  uploadStepPhoto(routeId: string, photo: File): Promise<UploadedPhoto>;
}

/** What the trips list needs from the backend. */
export interface TripsApi {
  /** A traveller's trips, newest day first (only those the caller may see). */
  listTrips(travellerId: string): Promise<Trip[]>;
  // one trip, to find the route it follows
  getTrip(tripId: string): Promise<Trip>;
  /** Needs an approved route. `date` is YYYY-MM-DD. */
  createTrip(routeId: string, date: string): Promise<Trip>;
  startTrip(tripId: string): Promise<Trip>;
  endTrip(tripId: string): Promise<Trip>;
  tripPack(tripId: string): Promise<PackLink>;
  /** Changing a trip under way (the route's caregiver only): send or dismiss the proposed bridging bus. */
  sendBridge?(tripId: string): Promise<Trip>;
  dismissBridge?(tripId: string): Promise<Trip>;
  /** New routes from where the traveller is, and sending one of them to the traveller. */
  replanTrip?(tripId: string): Promise<Replan>;
  applyReplan?(tripId: string, steps: RouteStep[], stepPaths?: LatLngTuple[][]): Promise<Trip>;
  /** What the free bus past a breakdown would change the trip's steps to (nothing is changed). */
  previewBridge?(tripId: string): Promise<BridgePreview>;
  /** Switch the trip (under way or not started) to the free bus past a breakdown now. */
  applyBridge?(tripId: string): Promise<Trip>;
  /** Send a route saved (and approved) for the trip: it replaces the traveller's steps from the current one on. */
  sendRoute?(tripId: string, routeId: string): Promise<Trip>;
  /** Where the traveller of a trip under way is (their check-ins). */
  tripLocation?(tripId: string): Promise<TripLocation>;
  /** The steps the traveller follows now, with their lines on the map and where trains aren't running. */
  tripPlan?(tripId: string): Promise<TripPlan>;
}

/** Re-checking planned or saved steps against LTA's alerts (used by the plan tab, saved routes and trips). */
export interface DisruptionsApi {
  checkDisruptions(steps: RouteStep[]): Promise<DisruptionCheck>;
}

/** An error answer from the service. `status` 0 means it could not be reached at all. */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export interface JourneyApiOptions {
  /** Empty (default): same address as the page, which is what the dev proxy and an Amplify rewrite expect. */
  baseUrl?: string;
  /** The login token (Cognito, issue 1.5). Sent as `Authorization: Bearer <token>`. */
  getToken?: () => string | null;
  /** Local testing only: the service believes this user when it runs with AUTH_MODE=dev. */
  getDevUser?: () => string | null;
  fetchImpl?: typeof fetch;
}

export class JourneyApi implements PlannerApi, SavedRoutesApi, TripsApi, DisruptionsApi {
  private baseUrl: string;
  private getToken: () => string | null;
  private getDevUser: () => string | null;
  private fetchImpl: typeof fetch;

  constructor(o: JourneyApiOptions = {}) {
    this.baseUrl = (o.baseUrl ?? "").replace(/\/$/, "");
    this.getToken = o.getToken ?? (() => null);
    this.getDevUser = o.getDevUser ?? (() => null);
    this.fetchImpl = o.fetchImpl ?? ((...args) => fetch(...args));
  }

  async searchPlaces(query: string): Promise<PlaceResult[]> {
    const body = await this.request<{ places: PlaceResult[] }>(`/api/places/search?q=${encodeURIComponent(query)}`);
    return body.places;
  }

  async plan(request: PlanRequest): Promise<PlanOption[]> {
    const body = await this.request<{ options: PlanOption[] }>("/api/routes/plan", {
      method: "POST",
      body: JSON.stringify(request),
    });
    return body.options;
  }

  createRoute(draft: RouteDraft): Promise<SavedRoute> {
    return this.request<SavedRoute>("/api/routes", { method: "POST", body: JSON.stringify(draft) });
  }

  async listRoutes(travellerId: string): Promise<SavedRoute[]> {
    const body = await this.request<{ routes: SavedRoute[] }>(`/api/routes?travellerId=${encodeURIComponent(travellerId)}`);
    return body.routes;
  }

  getRoute(routeId: string): Promise<SavedRoute> {
    return this.request<SavedRoute>(`/api/routes/${encodeURIComponent(routeId)}`);
  }

  approveRoute(routeId: string): Promise<SavedRoute> {
    return this.request<SavedRoute>(`/api/routes/${encodeURIComponent(routeId)}/approve`, { method: "POST" });
  }

  updateRoute(routeId: string, update: RouteUpdate): Promise<SavedRoute> {
    return this.request<SavedRoute>(`/api/routes/${encodeURIComponent(routeId)}`, { method: "PUT", body: JSON.stringify(update) });
  }

  async deleteRoute(routeId: string, version?: number): Promise<void> {
    const query = version === undefined ? "" : `?version=${encodeURIComponent(String(version))}`;
    await this.request<null>(`/api/routes/${encodeURIComponent(routeId)}${query}`, { method: "DELETE" });
  }

  suggestEasyRead(routeId: string): Promise<EasyReadSuggestion> {
    return this.request<EasyReadSuggestion>(`/api/routes/${encodeURIComponent(routeId)}/easy-read`, { method: "POST" });
  }

  getEasyReadLimits(): Promise<{ maxChars: number }> {
    return this.request<{ maxChars: number }>("/api/easy-read/limits");
  }

  uploadStepPhoto(routeId: string, photo: File): Promise<UploadedPhoto> {
    const form = new FormData();
    form.append("photo", photo);
    return this.request<UploadedPhoto>(`/api/routes/${encodeURIComponent(routeId)}/photos`, { method: "POST", body: form });
  }

  async listTrips(travellerId: string): Promise<Trip[]> {
    const body = await this.request<{ trips: Trip[] }>(`/api/trips?travellerId=${encodeURIComponent(travellerId)}`);
    return body.trips;
  }

  getTrip(tripId: string): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}`);
  }

  createTrip(routeId: string, date: string): Promise<Trip> {
    return this.request<Trip>("/api/trips", { method: "POST", body: JSON.stringify({ routeId, date }) });
  }

  startTrip(tripId: string): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}/start`, { method: "POST" });
  }

  endTrip(tripId: string): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}/end`, { method: "POST" });
  }

  tripPack(tripId: string): Promise<PackLink> {
    return this.request<PackLink>(`/api/trips/${encodeURIComponent(tripId)}/pack`);
  }

  sendBridge(tripId: string): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}/bridge/send`, { method: "POST" });
  }

  dismissBridge(tripId: string): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}/bridge/dismiss`, { method: "POST" });
  }

  replanTrip(tripId: string): Promise<Replan> {
    return this.request<Replan>(`/api/trips/${encodeURIComponent(tripId)}/replan`, { method: "POST" });
  }

  applyReplan(tripId: string, steps: RouteStep[], stepPaths?: LatLngTuple[][]): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}/replan/apply`,
      { method: "POST", body: JSON.stringify({ steps, stepPaths }) });
  }

  previewBridge(tripId: string): Promise<BridgePreview> {
    return this.request<BridgePreview>(`/api/trips/${encodeURIComponent(tripId)}/bridge/preview`);
  }

  applyBridge(tripId: string): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}/bridge/use`, { method: "POST" });
  }

  sendRoute(tripId: string, routeId: string): Promise<Trip> {
    return this.request<Trip>(`/api/trips/${encodeURIComponent(tripId)}/replan/apply`,
      { method: "POST", body: JSON.stringify({ routeId }) });
  }

  tripPlan(tripId: string): Promise<TripPlan> {
    return this.request<TripPlan>(`/api/trips/${encodeURIComponent(tripId)}/plan`);
  }

  tripLocation(tripId: string): Promise<TripLocation> {
    return this.request<TripLocation>(`/api/trips/${encodeURIComponent(tripId)}/location`);
  }

  checkDisruptions(steps: RouteStep[]): Promise<DisruptionCheck> {
    return this.request<DisruptionCheck>("/api/routes/check-disruptions", { method: "POST", body: JSON.stringify({ steps }) });
  }

  /** Local demos only (the service's DEMO_UI): which data is in use. Fails with 404 when it can't be switched. */
  getDataMode(): Promise<DataModeInfo> {
    return this.request<DataModeInfo>("/api/dev/data-mode");
  }

  /** Switches OneMap and LTA together between saved and live data. */
  setDataMode(source: DataSource): Promise<DataModeInfo> {
    return this.request<DataModeInfo>("/api/dev/data-mode", {
      method: "PUT",
      body: JSON.stringify({ onemap: source, lta: source }),
    });
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers: Record<string, string> = {};
    // A FormData body (a photo) sets its own multipart Content-Type, boundary included.
    if (init.body && !(init.body instanceof FormData)) headers["Content-Type"] = "application/json";
    const token = this.getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const devUser = this.getDevUser()?.trim();
    if (devUser) headers["X-Dev-User"] = devUser;

    let res: Response;
    try {
      res = await this.fetchImpl(this.baseUrl + path, { ...init, headers });
    } catch {
      throw new ApiError("Can't reach the server. Check the connection and try again.", 0);
    }
    const body: unknown = await res.json().catch(() => null);
    if (!res.ok) {
      // Errors always look like {"error": {"code": ..., "message": ...}} (contracts/api.md).
      const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
      throw new ApiError(error?.message ?? `Request failed (${res.status})`, res.status, error?.code);
    }
    return body as T;
  }
}
