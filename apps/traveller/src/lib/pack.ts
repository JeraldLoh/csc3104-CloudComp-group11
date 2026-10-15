// Loading the route pack (contracts/examples/route-pack.json): everything the traveller
// needs to follow the journey. Network first, so a route the caregiver just changed is
// picked up; the last good copy is kept in IndexedDB, so opening the app with no signal
// still shows the journey. Photos and audio: see offline.ts.
import { getMeta, metaKeys, setMeta, type DB } from "./db";
import { validatePack } from "./offline";
import type { RoutePack } from "../types";

export interface LoadedPack {
  pack: RoutePack;
  /** Where photoUrl/audioUrl are relative to. */
  baseUrl: string;
  source: "network" | "saved" | "demo";
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
    // Journey Service returns a short-lived S3 link to the pack (contracts/api.md).
    const res = await f(`/api/trips/${encodeURIComponent(tripId)}/pack`);
    if (!res.ok) throw new Error(`pack link: HTTP ${res.status}`);
    const { packUrl } = (await res.json()) as { packUrl: string };
    const packRes = await f(packUrl);
    if (!packRes.ok) throw new Error(`pack: HTTP ${packRes.status}`);
    // Throws on anything that isn't a complete pack, so it never replaces the saved one.
    const pack = validatePack(await packRes.json());
    const baseUrl = new URL(".", new URL(packUrl, location.href)).href;
    await setMeta(db, metaKeys.pack(tripId), { pack, baseUrl });
    return { pack, baseUrl, source: "network" };
  } catch (e) {
    const saved = await getMeta<{ pack: RoutePack; baseUrl: string }>(db, metaKeys.pack(tripId));
    if (saved) return { ...saved, source: "saved" };
    console.warn("CareRoute: no route pack", e);
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
