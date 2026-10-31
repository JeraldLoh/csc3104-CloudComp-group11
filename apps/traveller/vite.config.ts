/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

// In development the app calls /api/... on its own origin and Vite forwards each path
// to the service that owns it (contracts/api.md). Same origin = no CORS needed locally.
// Override the targets with env vars if your ports differ.
const checkin = process.env.CHECKIN_URL ?? "http://localhost:8001";
const alerts = process.env.ALERT_URL ?? "http://localhost:8005";
const journey = process.env.JOURNEY_URL ?? "http://localhost:8006";

export default defineConfig({
  plugins: [
    react(),
    // Service worker (issue 2.4): stores the app itself on the phone so it opens with no signal.
    VitePWA({
      // Don't swap in a new version while someone is mid-journey: an update waits until
      // every tab of the app is closed and is used the next time it opens.
      registerType: "prompt",
      injectRegister: false, // registered in src/lib/sw-register.ts, production only
      manifest: false, // we ship our own public/manifest.webmanifest
      workbox: {
        // Every built file: HTML, JS, CSS, fonts, icons. Hashed names = safe to cache forever.
        globPatterns: ["**/*.{html,js,css,woff,woff2,svg,webmanifest}"],
        // Demo trip photos are downloaded like real ones (offline.ts), not bundled with the app.
        globIgnores: ["**/demo-media/**"],
        // Opening any page with no signal shows the app instead of the browser's offline page.
        navigateFallback: "/index.html",
        // API calls must never get the HTML page by mistake.
        navigateFallbackDenylist: [/^\/api\//],
        cleanupOutdatedCaches: true,
        // Trip photos and audio are not handled here: the app downloads them itself
        // (src/lib/offline.ts), so it knows exactly when a trip is ready to use offline.
      },
      devOptions: { enabled: false },
    }),
  ],
  server: {
    port: 5173,
    // host: true lets a phone on the same Wi-Fi open the app (see README about HTTPS).
    host: true,
    proxy: {
      "/api/checkins": { target: checkin, changeOrigin: true },
      "/api/help": { target: alerts, changeOrigin: true },
      "/api/arrived": { target: alerts, changeOrigin: true },
      "/api/trips": { target: journey, changeOrigin: true },
    },
  },
  // `npm run preview` serves the production build (with the service worker) using the same proxy.
  preview: {
    port: 4173,
    proxy: {
      "/api/checkins": { target: checkin, changeOrigin: true },
      "/api/help": { target: alerts, changeOrigin: true },
      "/api/arrived": { target: alerts, changeOrigin: true },
      "/api/trips": { target: journey, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
  },
});
