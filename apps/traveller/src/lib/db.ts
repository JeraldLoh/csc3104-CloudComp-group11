// IndexedDB: the phone's durable storage. Survives reloads, closing the tab and the phone
// restarting, which localStorage also does, but IndexedDB adds transactions: several
// reads and writes either all happen or none do, even with two tabs open.
import { openDB, type DBSchema, type IDBPDatabase } from "idb";
import type { Checkin, RoutePack, Session } from "../types";

export interface OutboxItem {
  tripId: string;
  seq: number;
  checkin: Checkin;
  createdAt: number; // ms since epoch, phone clock
  attempts: number;
  /** True once the check-in couldn't go out straight away (offline, or a send failed). */
  buffered: boolean;
}

export interface QueuedRequest {
  id?: number;
  method: "POST";
  path: string;
  body: unknown;
  createdAt: number;
  attempts: number;
  label: string; // shown in the debug panel, e.g. "help", "trip start"
}

interface CareRouteDB extends DBSchema {
  // Key [tripId, seq]: one entry per check-in, kept in the order they were made.
  outbox: { key: [string, number]; value: OutboxItem };
  // Small values: seq counters, the current session, the cached route pack.
  meta: { key: string; value: unknown };
  // Other requests that must eventually reach the server (help, trip start/end).
  requests: { key: number; value: QueuedRequest };
}

export type DB = IDBPDatabase<CareRouteDB>;

export function openCareRouteDB(name = "careroute"): Promise<DB> {
  return openDB<CareRouteDB>(name, 1, {
    upgrade(db) {
      db.createObjectStore("outbox", { keyPath: ["tripId", "seq"] });
      db.createObjectStore("meta");
      db.createObjectStore("requests", { keyPath: "id", autoIncrement: true });
    },
  });
}

export async function getMeta<T>(db: DB, key: string): Promise<T | undefined> {
  return (await db.get("meta", key)) as T | undefined;
}

export async function setMeta(db: DB, key: string, value: unknown): Promise<void> {
  await db.put("meta", value, key);
}

export const metaKeys = {
  session: "session",
  seq: (tripId: string) => `seq:${tripId}`,
  pack: (tripId: string) => `pack:${tripId}`,
  stepIndex: (tripId: string) => `step:${tripId}`,
  tripStarted: (tripId: string) => `started:${tripId}`,
};

export type { Session, RoutePack };
