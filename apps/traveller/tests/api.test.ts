import { classify, HttpTransport, parseRetryAfter } from "../src/lib/api";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("classify", () => {
  it.each([
    [202, "ok"], [200, "ok"],
    [401, "unauthorized"], [403, "unauthorized"],
    [408, "retry"], [429, "retry"], [500, "retry"], [503, "retry"],
    [400, "invalid"], [404, "invalid"], [422, "invalid"],
  ])("%i -> %s", (status, kind) => expect(classify(status)).toBe(kind));
});

describe("HttpTransport", () => {
  it("posts JSON and returns the body on success", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(json(202, { accepted: 1, duplicates: 0 }));
    const t = new HttpTransport({ fetchImpl, baseUrl: "https://api.example" });
    const r = await t.post("/api/checkins", { a: 1 });
    expect(r).toEqual({ kind: "ok", body: { accepted: 1, duplicates: 0 } });
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe("https://api.example/api/checkins");
    expect(init.method).toBe("POST");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.headers.Authorization).toBeUndefined();
  });

  it("adds the login token when there is one", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(json(202, {}));
    await new HttpTransport({ fetchImpl, getToken: () => "abc" }).post("/x", {});
    expect(fetchImpl.mock.calls[0][1].headers.Authorization).toBe("Bearer abc");
  });

  it("reads the contract error message on 422", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(json(422, { error: { code: "invalid_request", message: "bad lat" } }));
    expect(await new HttpTransport({ fetchImpl }).post("/x", {})).toEqual({ kind: "invalid", status: 422, message: "bad lat" });
  });

  it("no network -> retry", async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    expect(await new HttpTransport({ fetchImpl }).post("/x", {})).toEqual({ kind: "retry", reason: "Failed to fetch" });
  });

  it("gives up waiting after the timeout -> retry", async () => {
    const fetchImpl = vi.fn((_url: string, init: RequestInit) =>
      new Promise<Response>((_, reject) => init.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))),
    );
    const r = await new HttpTransport({ fetchImpl: fetchImpl as unknown as typeof fetch, timeoutMs: 20 }).post("/x", {});
    expect(r).toEqual({ kind: "retry", reason: "timed out" });
  });

  it("passes keepalive through for background check-ins", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(json(202, {}));
    await new HttpTransport({ fetchImpl }).postCheckin({} as never, { keepalive: true });
    expect(fetchImpl.mock.calls[0][1].keepalive).toBe(true);
  });
});

describe("Retry-After", () => {
  it("parses seconds and HTTP dates, ignores nonsense, caps at 5 minutes", () => {
    const now = Date.parse("2026-10-23T00:00:00Z");
    expect(parseRetryAfter("2", now)).toBe(2000);
    expect(parseRetryAfter("Fri, 23 Oct 2026 00:00:10 GMT", now)).toBe(10_000);
    expect(parseRetryAfter("soon", now)).toBeUndefined();
    expect(parseRetryAfter(null, now)).toBeUndefined();
    expect(parseRetryAfter("99999", now)).toBe(300_000);
  });

  it("is passed on from a 503 so the sender can wait", async () => {
    const res = new Response(JSON.stringify({ error: { code: "overloaded" } }), { status: 503, headers: { "Retry-After": "2" } });
    const r = await new HttpTransport({ fetchImpl: vi.fn().mockResolvedValue(res) }).post("/x", {});
    expect(r).toEqual({ kind: "retry", reason: "HTTP 503", retryAfterMs: 2000 });
  });
});
