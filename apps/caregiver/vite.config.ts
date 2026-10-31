/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

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
  "/api": { target: journey, changeOrigin: true },
};

export default defineConfig({
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
