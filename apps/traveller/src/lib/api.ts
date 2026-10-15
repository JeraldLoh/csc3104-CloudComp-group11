// Talking to the backend. Every answer is sorted into one of four outcomes, because
// what the app should do next depends only on that:
//
//   ok            202/2xx            remove from the outbox
//   invalid       400, 404, 409, 422 the request itself is wrong; retrying won't help,
//                                    so drop it (and count it) instead of retrying forever
//   unauthorized  401, 403           stop sending until the traveller logs in again
//   retry         timeout, no network, 408, 429, 5xx
//                                    keep it and try again later (the server is
//                                    idempotent, so repeats are safe)
import type { CheckinAck, CheckinBatch, Checkin } from "../types";
import { API_BASE } from "../config";

export type SendResult =
  | { kind: "ok"; body: unknown }
  | { kind: "invalid"; status: number; message: string }
  | { kind: "unauthorized"; status: number }
  | { kind: "retry"; reason: string };

export interface Transport {
  postCheckin(c: Checkin, opts?: { keepalive?: boolean }): Promise<SendResult>;
  postBatch(b: CheckinBatch): Promise<SendResult>;
  post(path: string, body: unknown): Promise<SendResult>;
}

export interface HttpTransportOptions {
  baseUrl?: string;
  timeoutMs?: number;
  /** Returns the login token once Cognito (issue 1.5) exists. */
  getToken?: () => string | null;
  fetchImpl?: typeof fetch;
}

export function classify(status: number): SendResult["kind"] {
  if (status >= 200 && status < 300) return "ok";
  if (status === 401 || status === 403) return "unauthorized";
  if (status === 408 || status === 429 || status >= 500) return "retry";
  return "invalid";
}

export class HttpTransport implements Transport {
  private baseUrl: string;
  private timeoutMs: number;
  private getToken: () => string | null;
  private fetchImpl: typeof fetch;

  constructor(o: HttpTransportOptions = {}) {
    this.baseUrl = o.baseUrl ?? API_BASE;
    this.timeoutMs = o.timeoutMs ?? 8000;
    this.getToken = o.getToken ?? (() => null);
    this.fetchImpl = o.fetchImpl ?? ((...args) => fetch(...args));
  }

  postCheckin(c: Checkin, opts: { keepalive?: boolean } = {}): Promise<SendResult> {
    return this.post("/api/checkins", c, opts.keepalive);
  }

  postBatch(b: CheckinBatch): Promise<SendResult> {
    return this.post("/api/checkins/batch", b);
  }

  async post(path: string, body: unknown, keepalive = false): Promise<SendResult> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = this.getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    try {
      const res = await this.fetchImpl(this.baseUrl + path, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
        // keepalive lets a request finish after the page is hidden or closed
        // (browsers cap its body at 64 KB, so it's only used for single check-ins).
        keepalive,
      });
      const kind = classify(res.status);
      const parsed = await res.json().catch(() => null);
      if (kind === "ok") return { kind, body: parsed };
      if (kind === "unauthorized") return { kind, status: res.status };
      if (kind === "retry") return { kind, reason: `HTTP ${res.status}` };
      const message = (parsed as { error?: { message?: string } } | null)?.error?.message ?? `HTTP ${res.status}`;
      return { kind: "invalid", status: res.status, message };
    } catch (e) {
      const reason = controller.signal.aborted ? "timed out" : (e as Error).message || "network error";
      return { kind: "retry", reason };
    } finally {
      clearTimeout(timer);
    }
  }
}

export function asAck(body: unknown): CheckinAck {
  const b = (body ?? {}) as Partial<CheckinAck>;
  return { accepted: b.accepted ?? 0, duplicates: b.duplicates ?? 0 };
}
