import { useEffect, useState } from "react";
import type { CareRoute } from "../app/useCareRoute";

const ago = (t: number | null) => (t ? `${Math.max(0, Math.round((Date.now() - t) / 1000))} s ago` : "never");
const inS = (t: number | null) => (t ? `in ${Math.max(0, Math.round((t - Date.now()) / 1000))} s` : "-");

/** For the team and the demo video (?debug=1). Not shown to travellers. */
export function DebugPanel({ app }: { app: CareRoute }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000); // keep "x s ago" current
    return () => clearInterval(t);
  }, []);
  const s = app.sender;
  const rows: [string, string | number][] = [
    ["Waiting in outbox", s?.pending ?? "-"],
    ["Sent (this session)", s?.sent ?? 0],
    ["Server already had", s?.duplicates ?? 0],
    ["Rejected", s?.rejected ?? 0],
    ["Dropped (outbox full)", app.outboxDropped],
    ["Failures in a row", s?.consecutiveFailures ?? 0],
    ["Next retry", inS(s?.nextRetryAt ?? null)],
    ["Last success", ago(s?.lastSuccessAt ?? null)],
    ["Last error", s?.lastError ?? "-"],
    ["Other requests waiting", app.requests?.pending.map((r) => `${r.label} (${r.attempts})`).join(", ") || "none"],
    ["Position", app.fix ? `${app.fix.lat}, ${app.fix.lng} ±${app.fix.accuracyM ?? "?"} m` : "none yet"],
    ["Step index", app.stepIndex],
    ["App stored offline", app.offline.appShell ? "yes" : "no (dev server or first visit)"],
    ["Trip media stored", app.offline.media ? `${app.offline.media.done}/${app.offline.media.total}${app.offline.media.failed.length ? `, ${app.offline.media.failed.length} failed` : ""}` : "-"],
    ["Persistent storage", app.offline.persisted === null ? "-" : app.offline.persisted ? "granted" : "not granted"],
  ];
  return (
    <details className="debug" open>
      <summary>Debug</summary>
      <dl>
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      <div className="debug__actions">
        <label>
          <input type="checkbox" checked={app.offlineSim} onChange={(e) => app.setOfflineSim(e.target.checked)} /> Pretend no signal
        </label>
        <button onClick={() => void app.checkInNow()}>Check in now</button>
      </div>
    </details>
  );
}
