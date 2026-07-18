/* CollMgm service worker — offline shell cache */
const CACHE = "collmgm-v2";
const SHELL = ["/static/style.css", "/static/manifest.json"];

self.addEventListener("install", e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
});

self.addEventListener("activate", e =>
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  )
);

/* Static assets: stale-while-revalidate — serve from cache for speed, but
 * refresh the cache from the network in the background so an updated
 * style.css/JS reaches installed clients on their next page load. */
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.open(CACHE).then(cache =>
        cache.match(e.request).then(cached => {
          const fresh = fetch(e.request)
            .then(resp => {
              if (resp.ok) cache.put(e.request, resp.clone());
              return resp;
            })
            .catch(() => cached);
          return cached || fresh;
        })
      )
    );
  }
});
