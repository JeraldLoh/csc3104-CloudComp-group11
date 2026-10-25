/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the app calls /api/... on its own origin and Vite forwards it to the
// Journey Service (contracts/api.md). Same origin = no CORS needed locally.
// Override the target with JOURNEY_URL if your port differs.
const journey = process.env.JOURNEY_URL ?? "http://localhost:8006";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174, // the traveller app uses 5173
    proxy: { "/api": { target: journey, changeOrigin: true } },
  },
  preview: {
    port: 4174,
    proxy: { "/api": { target: journey, changeOrigin: true } },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
