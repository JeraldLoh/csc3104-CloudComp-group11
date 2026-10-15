import { cacheKey, entryKey, downloadMedia, forgetTrip, mediaUrls, PackError, storedMediaUrl, validatePack, mediaCacheName } from "../src/lib/offline";
import { checkForNewPack, loadPack } from "../src/lib/pack";
import { getMeta, metaKeys, setMeta } from "../src/lib/db";
import { DEMO_PACK } from "../src/lib/demo";
import { freshDb } from "./helpers";
import type { RoutePack } from "../src/types";

/** Minimal Cache Storage (jsdom doesn't have one). Same behaviour for what we use. */
class FakeCache {
  store = new Map<string, Response>();
  async match(key: string | Request) {
    const r = this.store.get(typeof key === "string" ? key : key.url);
    return r ? r.clone() : undefined;
  }
  async put(key: string, res: Response) {
    this.store.set(key, res);
  }
  async keys() {
    return [...this.store.keys()].map((k) => new Request(k));
  }
  async delete(key: string | Request) {
    return this.store.delete(typeof key === "string" ? key : key.url);
  }
}
class FakeCacheStorage {
  caches = new Map<string, FakeCache>();
  async open(name: string) {
    if (!this.caches.has(name)) this.caches.set(name, new FakeCache());
    return this.caches.get(name)!;
  }
  async delete(name: string) {
    return this.caches.delete(name);
  }
}

const BASE = "https://bucket.example/packs/R-007/";
const pack = (photos: (string | null)[]): RoutePack => ({
  ...DEMO_PACK,
  steps: DEMO_PACK.steps.slice(0, photos.length).map((s, i) => ({ ...s, stepIndex: i, photoUrl: photos[i], audioUrl: null })),
});
const img = (body = "img") => new Response(body, { status: 200, headers: { "Content-Type": "image/jpeg" } });

function fakeFetch(handler: (url: string, n: number) => Response | Promise<Response>) {
  const counts = new Map<string, number>();
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const n = (counts.get(url) ?? 0) + 1;
    counts.set(url, n);
    return handler(url, n);
  });
  return fn as unknown as typeof fetch & typeof fn;
}

describe("validatePack", () => {
  it("accepts the contract example", () => {
    expect(validatePack(structuredClone(DEMO_PACK)).routeId).toBe("R-007");
  });
  it.each([
    ["not JSON object", null],
    ["no steps", { ...DEMO_PACK, steps: [] }],
    ["steps out of order", { ...DEMO_PACK, steps: [DEMO_PACK.steps[1], DEMO_PACK.steps[0]] }],
    ["empty instruction", { ...DEMO_PACK, steps: [{ ...DEMO_PACK.steps[0], easyRead: "  " }] }],
    ["no caregiver phone", { ...DEMO_PACK, caregiver: { name: "Sam" } }],
    ["path too short", { ...DEMO_PACK, path: [[1, 2]] }],
  ])("rejects %s", (_name, bad) => {
    expect(() => validatePack(bad)).toThrow(PackError);
  });
});

describe("mediaUrls", () => {
  it("resolves paths against the pack's address, skips gaps, removes repeats", () => {
    const p = pack(["media/a.jpg", null, "media/a.jpg", "https://cdn.example/b.jpg"]);
    expect(mediaUrls(p, BASE)).toEqual([`${BASE}media/a.jpg`, "https://cdn.example/b.jpg"]);
  });
});

describe("cacheKey", () => {
  it("ignores the signature in signed S3 links, so a new link finds the stored file", () => {
    expect(cacheKey("https://b.example/m/a.jpg?X-Amz-Signature=1&X-Amz-Expires=900"))
      .toBe(cacheKey("https://b.example/m/a.jpg?X-Amz-Signature=2"));
  });
});

describe("downloadMedia", () => {
  it("stores every photo and reports progress", async () => {
    const caches = new FakeCacheStorage();
    const fetchImpl = fakeFetch(() => img());
    const seen: number[] = [];
    const result = await downloadMedia("T-001", pack(["a.jpg", "b.jpg", "c.jpg"]), BASE, (p) => seen.push(p.done), { caches: caches as unknown as CacheStorage, fetchImpl });
    expect(result).toMatchObject({ total: 3, done: 3, failed: [], finished: true });
    expect(seen.at(-1)).toBe(3);
    expect((await caches.open(mediaCacheName("T-001"))).store.size).toBe(3);
  });

  it("only downloads what's missing the second time", async () => {
    const caches = new FakeCacheStorage();
    const fetchImpl = fakeFetch(() => img());
    const p = pack(["a.jpg", "b.jpg"]);
    await downloadMedia("T-001", p, BASE, undefined, { caches: caches as unknown as CacheStorage, fetchImpl });
    await downloadMedia("T-001", p, BASE, undefined, { caches: caches as unknown as CacheStorage, fetchImpl });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("retries a flaky download", async () => {
    const caches = new FakeCacheStorage();
    const fetchImpl = fakeFetch((_u, n) => (n < 3 ? Promise.reject(new TypeError("Failed to fetch")) : img()));
    const r = await downloadMedia("T-001", pack(["a.jpg"]), BASE, undefined, { caches: caches as unknown as CacheStorage, fetchImpl });
    expect(r.failed).toEqual([]);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it("never stores a Wi-Fi login page (200 + HTML) as a photo", async () => {
    const caches = new FakeCacheStorage();
    const fetchImpl = fakeFetch(() => new Response("<html>Log in to Free_WiFi</html>", { status: 200, headers: { "Content-Type": "text/html" } }));
    const r = await downloadMedia("T-001", pack(["a.jpg"]), BASE, undefined, { caches: caches as unknown as CacheStorage, fetchImpl, retries: 0 });
    expect(r.failed).toEqual([`${BASE}a.jpg`]);
    expect((await caches.open(mediaCacheName("T-001"))).store.size).toBe(0);
  });

  it("never stores a download that was cut off part-way", async () => {
    const caches = new FakeCacheStorage();
    const broken = () => {
      const stream = new ReadableStream({
        start(c) {
          c.enqueue(new TextEncoder().encode("half a pho"));
          c.error(new TypeError("network connection lost"));
        },
      });
      return new Response(stream, { status: 200, headers: { "Content-Type": "image/jpeg" } });
    };
    const r = await downloadMedia("T-001", pack(["a.jpg"]), BASE, undefined, { caches: caches as unknown as CacheStorage, fetchImpl: fakeFetch(broken), retries: 0 });
    expect(r.failed).toHaveLength(1);
    expect((await caches.open(mediaCacheName("T-001"))).store.size).toBe(0);
  });

  it("after a route change, never shows a photo from the old version, even at the same address", async () => {
    const caches = new FakeCacheStorage();
    const cs = caches as unknown as CacheStorage;
    const v3 = { ...pack(["bus.jpg"]), packVersion: "R-007-v3" };
    const v4 = { ...pack(["bus.jpg"]), packVersion: "R-007-v4" }; // caregiver replaced the photo at the same address
    await downloadMedia("T-001", v3, BASE, undefined, { caches: cs, fetchImpl: fakeFetch(() => img("bus 190")) });

    // v4 not downloaded yet (no signal): the v3 photo must NOT be offered for v4.
    expect(await storedMediaUrl("T-001", `${BASE}bus.jpg`, "R-007-v4", { caches: cs })).toBe(`${BASE}bus.jpg`);

    await downloadMedia("T-001", v4, BASE, undefined, { caches: cs, fetchImpl: fakeFetch(() => img("bus 83")) });
    const stored = await (await caches.open(mediaCacheName("T-001"))).match(entryKey(`${BASE}bus.jpg`, "R-007-v4"));
    expect(await stored!.text()).toBe("bus 83");
  });

  it("removes the old version's files only once the new version is complete", async () => {
    const caches = new FakeCacheStorage();
    const cs = caches as unknown as CacheStorage;
    const cache = await caches.open(mediaCacheName("T-001"));
    await downloadMedia("T-001", { ...pack(["a.jpg"]), packVersion: "v3" }, BASE, undefined, { caches: cs, fetchImpl: fakeFetch(() => img()) });
    const failing = fakeFetch(() => new Response("", { status: 503 }));
    await downloadMedia("T-001", { ...pack(["a.jpg", "b.jpg"]), packVersion: "v4" }, BASE, undefined, { caches: cs, fetchImpl: failing, retries: 0 });
    expect(cache.store.size).toBe(1); // v3 kept while v4 is incomplete
    await downloadMedia("T-001", { ...pack(["a.jpg", "b.jpg"]), packVersion: "v4" }, BASE, undefined, { caches: cs, fetchImpl: fakeFetch(() => img()) });
    expect([...cache.store.keys()].every((k) => k.includes("careroute-version=v4"))).toBe(true);
    expect(cache.store.size).toBe(2);
  });

  it("serves the stored copy even when the link's signature has changed, and forgets it after the trip", async () => {
    const caches = new FakeCacheStorage();
    const cs = caches as unknown as CacheStorage;
    const p = pack(["a.jpg?sig=1"]);
    await downloadMedia("T-001", p, BASE, undefined, { caches: cs, fetchImpl: fakeFetch(() => img()) });
    const url = await storedMediaUrl("T-001", `${BASE}a.jpg?sig=2`, p.packVersion, { caches: cs });
    expect(url.startsWith("blob:")).toBe(true);
    await forgetTrip("T-001", { caches: cs });
    expect(await storedMediaUrl("T-001", `${BASE}a.jpg?sig=2`, p.packVersion, { caches: cs })).toBe(`${BASE}a.jpg?sig=2`);
  });
});

describe("loadPack", () => {
  const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

  it("downloads, validates and saves the pack", async () => {
    const db = await freshDb();
    const fetchImpl = fakeFetch((u) => (u.includes("/api/trips/") ? ok({ packUrl: BASE + "pack.json" }) : ok(DEMO_PACK)));
    const r = await loadPack(db, "T-001", { fetchImpl });
    expect(r?.source).toBe("network");
    expect(r?.baseUrl).toBe(BASE);
    expect(await getMeta(db, metaKeys.pack("T-001"))).toBeTruthy();
  });

  it("a broken download never replaces the saved pack", async () => {
    const db = await freshDb();
    await setMeta(db, metaKeys.pack("T-001"), { pack: DEMO_PACK, baseUrl: BASE });
    const fetchImpl = fakeFetch((u) => (u.includes("/api/trips/") ? ok({ packUrl: BASE + "pack.json" }) : ok({ steps: "truncated" })));
    const r = await loadPack(db, "T-001", { fetchImpl });
    expect(r?.source).toBe("saved");
    expect(r?.pack.steps).toHaveLength(4);
  });

  it("no signal: uses the saved pack", async () => {
    const db = await freshDb();
    await setMeta(db, metaKeys.pack("T-001"), { pack: DEMO_PACK, baseUrl: BASE });
    const r = await loadPack(db, "T-001", { fetchImpl: fakeFetch(() => Promise.reject(new TypeError("Failed to fetch"))) });
    expect(r?.source).toBe("saved");
  });
});

describe("demo pack", () => {
  it("has a full base address, so its photos resolve and get downloaded", async () => {
    const db = await freshDb();
    const r = await loadPack(db, "T-001", { demo: DEMO_PACK });
    expect(mediaUrls(r!.pack, r!.baseUrl)).toHaveLength(4);
  });
});

describe("checkForNewPack", () => {
  const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  const V4 = { ...DEMO_PACK, packVersion: "R-007-v4", routeVersion: 4 };

  it("same version: asks only for the tiny link, downloads nothing", async () => {
    const db = await freshDb();
    const fetchImpl = fakeFetch(() => ok({ packUrl: BASE + "pack.json", packVersion: "R-007-v3" }));
    expect(await checkForNewPack(db, "T-001", "R-007-v3", { fetchImpl })).toBeNull();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("new version: downloads, validates and saves it", async () => {
    const db = await freshDb();
    const fetchImpl = fakeFetch((u) => (u.includes("/api/trips/") ? ok({ packUrl: BASE + "v4.json", packVersion: "R-007-v4" }) : ok(V4)));
    const r = await checkForNewPack(db, "T-001", "R-007-v3", { fetchImpl });
    expect(r?.pack.packVersion).toBe("R-007-v4");
    const saved = await getMeta<{ pack: RoutePack }>(db, metaKeys.pack("T-001"));
    expect(saved?.pack.packVersion).toBe("R-007-v4");
  });

  it("broken new version: keeps the current one", async () => {
    const db = await freshDb();
    await setMeta(db, metaKeys.pack("T-001"), { pack: DEMO_PACK, baseUrl: BASE });
    const fetchImpl = fakeFetch((u) => (u.includes("/api/trips/") ? ok({ packUrl: BASE + "v4.json", packVersion: "R-007-v4" }) : ok({ ...V4, steps: [] })));
    expect(await checkForNewPack(db, "T-001", "R-007-v3", { fetchImpl })).toBeNull();
    expect((await getMeta<{ pack: RoutePack }>(db, metaKeys.pack("T-001")))?.pack.packVersion).toBe("R-007-v3");
  });

  it("no signal: no change, no error", async () => {
    const db = await freshDb();
    const fetchImpl = fakeFetch(() => Promise.reject(new TypeError("Failed to fetch")));
    expect(await checkForNewPack(db, "T-001", "R-007-v3", { fetchImpl })).toBeNull();
  });
});
