/** The Firebase web config from firebase-web-config.json in the repo root (vite.config.ts), or null: push is off. */
declare const __FIREBASE__: {
  apiKey: string;
  authDomain: string;
  projectId: string;
  storageBucket: string;
  messagingSenderId: string;
  appId: string;
  vapidKey: string;
} | null;
