// Loading the route pack (contracts/examples/route-pack.json): everything the traveller
// needs to follow the journey. Network first, so a route the caregiver just changed is
// picked up; the last good copy is kept in IndexedDB, so opening the app with no signal
// still shows the journey. Photos and audio: see offline.ts.
//
// Updates (issue 2.4): while the trip is open the app asks the Journey Service which
// version is current. That answer is tiny ({packUrl, packVersion}), so it's cheap to ask
// every minute; the pack itself is only downloaded when the version has changed.
import { getMeta, metaKeys, setMeta, type DB } from "./db";
import { validatePack } from "./offline";
import type { RoutePack } from "../types";
import { API_BASE } from "../config";

export interface LoadedPack {
  pack: RoutePack;
  /** Where photoUrl/audioUrl are relative to. */
  baseUrl: string;
  source: "network" | "saved" | "demo";
}

interface PackLink {
  packUrl: string;
  packVersion?: string;
}

async function fetchLink(tripId: string, f: typeof fetch): Promise<PackLink> {
  // Journey Service returns a short-lived S3 link to the pack (contracts/api.md).
  const res = await f(`${API_BASE}/api/trips/${encodeURIComponent(tripId)}/pack`);
  if (!res.ok) throw new Error(`pack link: HTTP ${res.status}`);
  const link = (await res.json()) as PackLink;
  if (!link?.packUrl) throw new Error("pack link: no packUrl");
  return link;
}

async function fetchPack(db: DB, tripId: string, link: PackLink, f: typeof fetch): Promise<LoadedPack> {
  const packRes = await f(link.packUrl);
  if (!packRes.ok) throw new Error(`pack: HTTP ${packRes.status}`);
  // Throws on anything that isn't a complete pack, so it never replaces the saved one.
  const pack = validatePack(await packRes.json());
  const baseUrl = new URL(".", new URL(link.packUrl, location.href)).href;
  await setMeta(db, metaKeys.pack(tripId), { pack, baseUrl });
  return { pack, baseUrl, source: "network" };
}

export async function loadPack(
  db: DB,
  tripId: string,
  opts: { demo?: RoutePack; fetchImpl?: typeof fetch } = {},
): Promise<LoadedPack | null> {
  // baseUrl must be a full address ("https://host/..."), or photo paths can't be resolved.
  if (opts.demo) return { pack: opts.demo, baseUrl: new URL("/", location.href).href, source: "demo" };
  const f = opts.fetchImpl ?? fetch;
  try {
    return await fetchPack(db, tripId, await fetchLink(tripId, f), f);
  } catch (e) {
    const saved = await getMeta<{ pack: RoutePack; baseUrl: string }>(db, metaKeys.pack(tripId));
    if (saved) return { ...saved, source: "saved" };
    console.warn("CareRoute: no route pack", e);
    return null;
  }
}

/**
 * Is there a newer version of this trip's route? Returns the new pack (already validated
 * and saved), or null if nothing changed or the check couldn't be done (no signal, server
 * down: the current pack simply stays in use and we ask again later).
 */
export async function checkForNewPack(
  db: DB,
  tripId: string,
  currentVersion: string,
  opts: { fetchImpl?: typeof fetch } = {},
): Promise<LoadedPack | null> {
  const f = opts.fetchImpl ?? fetch;
  try {
    const link = await fetchLink(tripId, f);
    if (link.packVersion && link.packVersion === currentVersion) return null; // nothing new, nothing downloaded
    const loaded = await fetchPack(db, tripId, link, f);
    return loaded.pack.packVersion === currentVersion ? null : loaded;
  } catch {
    return null;
  }
}

export function mediaUrl(loaded: LoadedPack, path: string | null): string | null {
  if (!path) return null;
  try {
    return new URL(path, loaded.baseUrl).href;
  } catch {
    return null;
  }
}
