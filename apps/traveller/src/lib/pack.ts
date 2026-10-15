// Loading the route pack (contracts/examples/route-pack.json): everything the traveller
// needs to follow the journey. The last copy is kept in IndexedDB, so a reload with no
// signal still shows the journey. (Photos/audio offline and the service worker are 2.4.)
import { getMeta, metaKeys, setMeta, type DB } from "./db";
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
  if (opts.demo) return { pack: opts.demo, baseUrl: "/", source: "demo" };
  const f = opts.fetchImpl ?? fetch;
  try {
    // Journey Service returns a short-lived S3 link to the pack (contracts/api.md).
    const res = await f(`/api/trips/${encodeURIComponent(tripId)}/pack`);
    if (!res.ok) throw new Error(`pack link: HTTP ${res.status}`);
    const { packUrl } = (await res.json()) as { packUrl: string };
    const packRes = await f(packUrl);
    if (!packRes.ok) throw new Error(`pack: HTTP ${packRes.status}`);
    const pack = (await packRes.json()) as RoutePack;
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
