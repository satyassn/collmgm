/* CollMgm service worker — offline shell cache */
const CACHE = "collmgm-v3";
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
 * refresh the cache from the network in the background. Pages link assets as
 * style.css?v=<mtime> (see base.html), so a changed file is a NEW URL and
 * is a cache miss — it can never be served stale; the old-URL copies of the
 * same file are pruned below so the cache doesn't grow with every deploy. */
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.open(CACHE).then(cache =>
        cache.match(e.request).then(cached => {
          const fresh = fetch(e.request)
            .then(resp => {
              if (resp.ok) {
                cache.put(e.request, resp.clone());
                cache.keys().then(keys => keys.forEach(k => {
                  const u = new URL(k.url);
                  if (u.pathname === url.pathname && u.search !== url.search) cache.delete(k);
                }));
              }
              return resp;
            })
            .catch(() => cached);
          return cached || fresh;
        })
      )
    );
  }
});
