/* ਮਾਲਵਾ ਗਜ਼ਟ service worker
   - app shell (HTML, icons, manifest) cached for instant + offline open
   - news.json and feeds are ALWAYS network-first so news is never stale
*/
const SHELL = "mg-shell-v1";
const SHELL_FILES = [
  "./",
  "./index.html",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable.png",
  "./apple-touch-icon.png"
];

self.addEventListener("install", e=>{
  e.waitUntil(caches.open(SHELL).then(c=>c.addAll(SHELL_FILES)).then(()=>self.skipWaiting()));
});

self.addEventListener("activate", e=>{
  e.waitUntil(
    caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==SHELL).map(k=>caches.delete(k))))
      .then(()=>self.clients.claim())
  );
});

self.addEventListener("fetch", e=>{
  const url = new URL(e.request.url);
  if(e.request.method !== "GET") return;

  // never cache news data or external feeds/proxies — always live
  const isLiveData = url.pathname.endsWith("news.json")
    || /allorigins|corsproxy|news\.google|bing\.com|api\.anthropic/.test(url.href);
  if(isLiveData){
    e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
    return;
  }

  // app shell: cache-first, refresh in background
  if(url.origin === location.origin){
    e.respondWith(
      caches.match(e.request).then(hit=>{
        const net = fetch(e.request).then(res=>{
          if(res && res.status===200){
            const copy=res.clone(); caches.open(SHELL).then(c=>c.put(e.request,copy));
          }
          return res;
        }).catch(()=>hit);
        return hit || net;
      })
    );
  }
});
