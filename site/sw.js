// Offline-Fähigkeit: immer zuerst das Netz fragen (damit Updates sofort greifen),
// bei schlechtem Empfang die zuletzt geladene Version aus dem Cache zeigen.
const CACHE = "kino-hh-v1";
const SHELL = ["./", "index.html", "data.json", "manifest.webmanifest", "icon.svg", "icon-180.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET") return;

  // Poster der Kinos: einmal geladen, dann aus dem Cache
  if (url.origin !== location.origin) {
    if (e.request.destination !== "image") return;
    e.respondWith(caches.open(CACHE + "-img").then(async c => {
      const hit = await c.match(e.request);
      if (hit) return hit;
      const res = await fetch(e.request);
      if (res.ok || res.type === "opaque") c.put(e.request, res.clone());
      return res;
    }));
    return;
  }

  // eigene Dateien: Netz zuerst (max. 4 s), sonst Cache
  const key = url.pathname.endsWith("/data.json") ? new Request("data.json") : e.request;
  e.respondWith((async () => {
    const c = await caches.open(CACHE);
    try {
      const res = await Promise.race([
        fetch(e.request, { cache: "no-store" }),
        new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 4000)),
      ]);
      if (res.ok) c.put(key, res.clone());
      return res;
    } catch (err) {
      return (await c.match(key, { ignoreSearch: true })) || (await c.match("index.html")) || Response.error();
    }
  })());
});
