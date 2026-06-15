/* ਮਾਲਵਾ ਗਜ਼ਟ — service worker
   Network-FIRST by design, so a new deploy is NEVER blocked by an old cache.
   - index.html + *.json  → network-first (fresh when online, cached copy only when offline)
   - icons / manifest      → cache-first (rarely change, instant load)
   - fonts / images/proxy  → network, fall back to cache
   Bump CACHE only when you change the SHELL list below. */
const CACHE = "malwa-v1";

/* relative paths → work under any GitHub Pages subpath (…/Malwa/) */
const SHELL = [
  "./", "./index.html", "./manifest.webmanifest",
  "./icon-192.png", "./icon-512.png", "./icon-maskable.png", "./apple-touch-icon.png",
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(SHELL))
      .then(() => self.skipWaiting())          // new SW takes over right away
  );
});

self.addEventListener("activate", e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)));
    await self.clients.claim();                 // control open tabs immediately
  })());
});

async function networkFirst(req, fallbackKey) {
  try {
    const res = await fetch(req, { cache: "no-store" });   // always hit the network first
    const cache = await caches.open(CACHE);
    cache.put(req, res.clone()).catch(() => {});           // keep a copy for offline
    return res;
  } catch (err) {
    const cached = await caches.match(req)
      || (fallbackKey && await caches.match(fallbackKey));
    return cached || Response.error();
  }
}

async function cacheFirst(req) {
  const cached = await caches.match(req);
  if (cached) return cached;
  const res = await fetch(req);
  const cache = await caches.open(CACHE);
  cache.put(req, res.clone()).catch(() => {});
  return res;
}

self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;             // never touch POSTs etc.
  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;

  // 1) page loads + index.html → newest deploy wins
  if (req.mode === "navigate" || (sameOrigin && url.pathname.endsWith("/index.html"))) {
    e.respondWith(networkFirst(req, "./index.html"));
    return;
  }
  // 2) live data (news.json, extras.json, claude_cache.json) → always fresh online
  if (sameOrigin && url.pathname.endsWith(".json")) {
    e.respondWith(networkFirst(req));
    return;
  }
  // 3) app shell assets → instant from cache
  if (sameOrigin && /\.(png|webmanifest|ico|svg|css|js)$/.test(url.pathname)) {
    e.respondWith(cacheFirst(req));
    return;
  }
  // 4) everything else (Google fonts, news images, CORS proxies) → network, then cache
  e.respondWith(fetch(req).catch(() => caches.match(req)));
});
