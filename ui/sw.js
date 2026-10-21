// Minimal service worker: makes Clip Studio installable as an app.
// Network-first, no offline cache (everything lives on the server PC).
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
