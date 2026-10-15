// Making a trip usable with no signal (issue 2.4).
//
// The traveller may lose signal at any point: in the MRT, in a lift, on a bus with bad
// coverage. Everything they need to keep following the steps must already be on the phone.
//
//   route pack (JSON)   kept in IndexedDB (pack.ts); only ever replaced by a pack that
//                       passes validatePack(), so a cut-off or corrupt download can never
//                       overwrite a good one
//   photos and audio    downloaded into Cache Storage before the trip starts, one cache
//                       per trip, stored per route version: a photo is only ever shown
//                       next to the instructions of the version it was downloaded for.
//                       (If the caregiver changes the route, an old photo of bus 190 must
//                       never appear next to a new "Get on bus 83".) Keyed by URL without
//                       its query string, because S3 signed links change on every request.
//   the app itself      the service worker (sw-register.ts)
//
// Downloads happen in the background and never block "Start my trip": instructions are
// text, so a missing photo only means the pictogram is shown instead.
import type { RoutePack } from "../types";

export const mediaCacheName = (tripId: string) => `careroute-media-${tripId}`;

/** Signed URLs differ on each request; the cache key is the URL without its query string. */
export const cacheKey = (url: string) => {
  const u = new URL(url, typeof location !== "undefined" ? location.href : "http://localhost/");
  u.search = "";
  u.hash = "";
  return u.href;
};

/** Where a file is stored: its address (no signature) plus the route version it belongs to. */
export const entryKey = (url: string, packVersion: string) => {
  const u = new URL(cacheKey(url));
  u.searchParams.set("careroute-version", packVersion);
  return u.href;
};

export class PackError extends Error {}

/** Checks a downloaded pack really is a route pack before it's allowed to replace the saved one. */
export function validatePack(x: unknown): RoutePack {
  const fail = (msg: string): never => {
    throw new PackError(`route pack: ${msg}`);
  };
  if (!x || typeof x !== "object") fail("not an object");
  const p = x as Record<string, unknown>;
  for (const k of ["packVersion", "routeId", "travellerName"]) if (typeof p[k] !== "string" || !p[k]) fail(`missing ${k}`);
  if (typeof p.routeVersion !== "number") fail("missing routeVersion");
  if (!Array.isArray(p.steps) || p.steps.length === 0) fail("no steps");
  (p.steps as unknown[]).forEach((s, i) => {
    const st = s as Record<string, unknown>;
    if (!st || st.stepIndex !== i) fail(`step ${i}: stepIndex must be ${i}`);
    if (typeof st.easyRead !== "string" || !st.easyRead.trim()) fail(`step ${i}: no easyRead text`);
    if (typeof st.lat !== "number" || typeof st.lng !== "number") fail(`step ${i}: no location`);
  });
  if (!Array.isArray(p.path) || p.path.length < 2) fail("path needs at least 2 points");
  const c = p.caregiver as Record<string, unknown> | undefined;
  if (!c || typeof c.name !== "string" || typeof c.phone !== "string") fail("missing caregiver name/phone");
  if (!p.messages || typeof p.messages !== "object") fail("missing messages");
  return x as RoutePack;
}

/** Every photo and audio file the pack refers to, as absolute URLs. */
export function mediaUrls(pack: RoutePack, baseUrl: string): string[] {
  const out = new Set<string>();
  for (const s of pack.steps) {
    for (const path of [s.photoUrl, s.audioUrl]) {
      if (!path) continue;
      try {
        out.add(new URL(path, baseUrl).href);
      } catch { /* bad URL: skip, the step still has its text */ }
    }
  }
  return [...out];
}

export interface DownloadProgress {
  total: number;
  done: number;
  failed: string[];
  finished: boolean;
}

export interface OfflineDeps {
  caches?: CacheStorage;
  fetchImpl?: typeof fetch;
  retries?: number;
}

const isMedia = (res: Response) => {
  const type = res.headers.get("Content-Type") ?? "";
  return /^(image|audio)\//.test(type) || type === "application/octet-stream" || type === "";
};

/**
 * Downloads every photo and audio file for this pack into the trip's cache.
 * Files already stored are skipped, so calling this again (e.g. when signal returns)
 * only fetches what's missing. Once everything for this pack is stored, files from
 * older versions of the route that this pack no longer uses are deleted.
 */
export async function downloadMedia(
  tripId: string,
  pack: RoutePack,
  baseUrl: string,
  onProgress: (p: DownloadProgress) => void = () => {},
  deps: OfflineDeps = {},
): Promise<DownloadProgress> {
  const cacheStorage = deps.caches ?? globalThis.caches;
  const f = deps.fetchImpl ?? ((...a: Parameters<typeof fetch>) => fetch(...a));
  const urls = mediaUrls(pack, baseUrl);
  const progress: DownloadProgress = { total: urls.length, done: 0, failed: [], finished: false };
  if (!cacheStorage) {
    // Very old browser or insecure page (http on a non-localhost address): no Cache Storage.
    progress.failed = urls;
    progress.finished = true;
    onProgress({ ...progress });
    return progress;
  }
  const cache = await cacheStorage.open(mediaCacheName(tripId));
  onProgress({ ...progress });

  for (const url of urls) {
    const key = entryKey(url, pack.packVersion);
    if (await cache.match(key)) {
      progress.done++;
      onProgress({ ...progress });
      continue;
    }
    let stored = false;
    for (let attempt = 0; attempt <= (deps.retries ?? 2) && !stored; attempt++) {
      try {
        const res = await f(url);
        if (!res.ok || !isMedia(res)) throw new Error(`HTTP ${res.status} ${res.headers.get("Content-Type") ?? ""}`);
        // Read the whole body before storing: a connection that drops halfway throws
        // here, instead of leaving a cut-off file in the cache that looks complete.
        const body = await res.blob();
        await cache.put(key, new Response(body, { headers: { "Content-Type": res.headers.get("Content-Type") ?? body.type } }));
        stored = true;
      } catch { /* try again, then give up on this file */ }
    }
    if (stored) progress.done++;
    else progress.failed.push(url);
    onProgress({ ...progress });
  }

  if (progress.failed.length === 0) {
    // This version is complete: files from older versions of the route can go.
    const keep = new Set(urls.map((u) => entryKey(u, pack.packVersion)));
    for (const req of await cache.keys()) if (!keep.has(req.url)) await cache.delete(req);
  }
  progress.finished = true;
  onProgress({ ...progress });
  return progress;
}

/** A URL the page can show: the stored copy for this route version if there is one, otherwise the network URL. */
export async function storedMediaUrl(tripId: string, url: string, packVersion: string, deps: { caches?: CacheStorage } = {}): Promise<string> {
  const cacheStorage = deps.caches ?? globalThis.caches;
  if (!cacheStorage) return url;
  const hit = await (await cacheStorage.open(mediaCacheName(tripId))).match(entryKey(url, packVersion));
  if (!hit) return url;
  return URL.createObjectURL(await hit.blob());
}

/** Delete everything stored for a finished trip. */
export async function forgetTrip(tripId: string, deps: { caches?: CacheStorage } = {}): Promise<void> {
  await (deps.caches ?? globalThis.caches)?.delete(mediaCacheName(tripId));
}

/**
 * Ask the browser not to clear our storage when the phone runs low on space. Without
 * this, a browser may evict IndexedDB and Cache Storage (taking the trip and any unsent
 * check-ins with it). Chrome usually grants it to installed or frequently used apps.
 */
export async function requestPersistentStorage(): Promise<boolean> {
  try {
    if (!navigator.storage?.persist) return false;
    return (await navigator.storage.persisted()) || (await navigator.storage.persist());
  } catch {
    return false;
  }
}

export async function storageEstimate(): Promise<{ usedMB: number; quotaMB: number } | null> {
  try {
    const e = await navigator.storage?.estimate?.();
    if (!e) return null;
    return { usedMB: Math.round(((e.usage ?? 0) / 1e6) * 10) / 10, quotaMB: Math.round((e.quota ?? 0) / 1e6) };
  } catch {
    return null;
  }
}
