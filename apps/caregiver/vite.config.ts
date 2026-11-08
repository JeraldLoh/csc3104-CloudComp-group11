/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { existsSync, readFileSync } from "node:fs";

// The Firebase web config for push notifications (issue 5.4), shared by both apps: firebase-web-config.json in the
// repo root. Its values are public by design (the secret key is the Alert Service's). Without the file, push is off.
const firebaseFile = new URL("../../firebase-web-config.json", import.meta.url);
const firebaseRead = existsSync(firebaseFile) ? JSON.parse(readFileSync(firebaseFile, "utf8")) : null;
// No VAPID key yet (Cloud Messaging, Web Push certificates) means browsers can't subscribe, so push stays off.
// The tests always run without it, so they never depend on someone's real Firebase project.
const firebase = firebaseRead?.vapidKey && !process.env.VITEST ? firebaseRead : null;

// In development the app calls /api/... on its own origin and Vite forwards each path to the
// service that owns it (contracts/api.md). Same origin = no CORS needed locally.
// Override the targets with JOURNEY_URL / ALERT_URL if your ports differ.
const journey = process.env.JOURNEY_URL ?? "http://localhost:8006";
// 127.0.0.1, not localhost: Node may try IPv6 first, and the Alert Service's dev.ps1 listens on IPv4 only.
const alerts = process.env.ALERT_URL ?? "http://127.0.0.1:8005";

// More specific paths first: the first matching rule wins.
const proxy = {
  "/api/incidents": { target: alerts, changeOrigin: true },
  "/api/stream": { target: alerts, changeOrigin: true },
  "/api/travellers": { target: alerts, changeOrigin: true }, // profiles (issue 5.1) live in the Alert Service
  "/api/links": { target: alerts, changeOrigin: true },
  "/api/devices": { target: alerts, changeOrigin: true }, // push notifications (issue 5.4)
  "/api": { target: journey, changeOrigin: true },
};

export default defineConfig({
  define: { __FIREBASE__: JSON.stringify(firebase) },
  plugins: [react()],
  server: {
    port: 5174, // the traveller app uses 5173
    proxy,
  },
  preview: {
    port: 4174,
    proxy,
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
