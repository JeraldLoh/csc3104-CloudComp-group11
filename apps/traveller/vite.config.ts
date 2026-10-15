/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the app calls /api/... on its own origin and Vite forwards each path
// to the service that owns it (contracts/api.md). Same origin = no CORS needed locally.
// Override the targets with env vars if your ports differ.
const checkin = process.env.CHECKIN_URL ?? "http://localhost:8001";
const alerts = process.env.ALERT_URL ?? "http://localhost:8005";
const journey = process.env.JOURNEY_URL ?? "http://localhost:8006";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // host: true lets a phone on the same Wi-Fi open the app (see README about HTTPS).
    host: true,
    proxy: {
      "/api/checkins": { target: checkin, changeOrigin: true },
      "/api/help": { target: alerts, changeOrigin: true },
      "/api/trips": { target: journey, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
  },
});
