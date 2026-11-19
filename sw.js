const CACHE_NAME = 'flightline-shell-v3';
const APP_SHELL = [
  '/index.html', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png',
  '/flightline-logo.png', '/squadrons.json', '/jppt-discuss-v1.json',
  '/pdf-viewer.html'
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(APP_SHELL.map(path => new Request(path,{cache:'reload'})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key =>
      key.startsWith('flightline-shell-') && key !== CACHE_NAME
    ).map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const {request} = event;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  const path = url.pathname === '/' ? '/index.html' : url.pathname;
  if (!APP_SHELL.includes(path) && !path.startsWith('/patches/')) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    try {
      const response = await fetch(request,{signal:AbortSignal.timeout(5500)});
      if (response.ok) {
        await cache.put(path,response.clone()).catch(() => {});
        return response;
      }
      return (await cache.match(path)) || response;
    } catch {
      return (await cache.match(path)) || Response.error();
    }
  })());
});
