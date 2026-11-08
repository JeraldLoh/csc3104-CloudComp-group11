// Where the API lives.
//  - Empty (default): same address as the app. Works with the dev/preview proxy, and on
//    Amplify when a rewrite forwards /api/* to the load balancer (needs HTTPS there).
//  - Set VITE_API_BASE (e.g. "https://api.careroute.example") in Amplify's environment
//    variables to call the load balancer directly. Then set CORS_ORIGINS on the
//    Check-in Service to the app's Amplify address.
// `?.`: import.meta.env only exists when Vite builds the app (not in Node scripts).
export const API_BASE: string = (import.meta.env?.VITE_API_BASE ?? "").replace(/\/$/, "");
