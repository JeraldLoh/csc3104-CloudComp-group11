// Other requests that must reach the server eventually: help, trip start, trip end.
// Same idea as the check-in outbox: store first, send after, delete only on success,
// retry with backoff. They're safe to repeat because the server deduplicates them:
// help becomes incident U-042#T-001#help_requested#<step> (contracts/dynamo-db-tables.md),
// and starting an already-active trip changes nothing.
import { backoffDelay } from "./sender";
import type { DB, QueuedRequest } from "./db";
import type { Transport } from "./api";

const priority = (r: QueuedRequest) => (r.label === "help" ? 0 : 1);

export interface RequestQueueState {
  pending: QueuedRequest[];
  lastError: string | null;
  lastDone: { label: string; at: number } | null;
}

export class RequestQueue {
  private running: Promise<void> | null = null;
  private rerun = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private failures = 0;
  private listeners = new Set<(s: RequestQueueState) => void>();
  state: RequestQueueState = { pending: [], lastError: null, lastDone: null };

  constructor(
    private db: DB,
    private transport: Transport,
    private opts: { random?: () => number; baseMs?: number; capMs?: number; now?: () => number } = {},
  ) {}

  subscribe(fn: (s: RequestQueueState) => void): () => void {
    this.listeners.add(fn);
    fn(this.state);
    return () => this.listeners.delete(fn);
  }

  async add(label: string, path: string, body: unknown): Promise<void> {
    const now = (this.opts.now ?? Date.now)();
    await this.db.add("requests", { method: "POST", path, body, label, createdAt: now, attempts: 0 });
    await this.flush();
  }

  retryNow(): Promise<void> {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    return this.flush();
  }

  flush(): Promise<void> {
    if (this.running) {
      this.rerun = true;
      return this.running;
    }
    this.running = (async () => {
      try {
        do {
          this.rerun = false;
          await this.drain();
        } while (this.rerun);
      } finally {
        this.running = null;
        await this.refresh();
      }
    })();
    return this.running;
  }

  /**
   * One pass over everything waiting. Each request is tried on its own, so one that keeps
   * failing (say the Journey Service is down, so "trip start" fails) never holds up the
   * others: no head-of-line blocking. Help always goes first.
   */
  private async drain(): Promise<void> {
    const all = await this.db.getAll("requests");
    all.sort((a, b) => priority(a) - priority(b) || a.id! - b.id!);
    let failed = false;
    for (const req of all) {
      const r = await this.transport.post(req.path, req.body);
      if (r.kind === "ok" || r.kind === "invalid") {
        // invalid (e.g. 404 trip not found) won't get better by retrying.
        await this.db.delete("requests", req.id!);
        this.update({
          lastError: r.kind === "invalid" ? `${req.label}: ${r.message}` : this.state.lastError,
          lastDone: r.kind === "ok" ? { label: req.label, at: (this.opts.now ?? Date.now)() } : this.state.lastDone,
        });
        continue;
      }
      failed = true;
      req.attempts += 1;
      await this.db.put("requests", req);
      this.update({ lastError: `${req.label}: ${r.kind === "retry" ? r.reason : "log in again"}` });
      await this.refresh(); // the help screen watches attempts to decide when to say "call now"
    }
    if (!failed) {
      this.failures = 0;
      return;
    }
    this.failures += 1;
    const delay = backoffDelay(this.failures, this.opts.random ?? Math.random, this.opts.baseMs ?? 1000, this.opts.capMs ?? 30_000);
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, delay);
  }

  private async refresh() {
    this.update({ pending: await this.db.getAll("requests") });
  }

  private update(patch: Partial<RequestQueueState>) {
    this.state = { ...this.state, ...patch };
    for (const fn of this.listeners) fn(this.state);
  }

  dispose() {
    if (this.timer) clearTimeout(this.timer);
    this.listeners.clear();
  }
}
