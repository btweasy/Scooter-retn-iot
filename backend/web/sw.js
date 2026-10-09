'use strict';
/*
 * Service worker aplikacji klienta (PWA).
 *
 * Zadanie: aplikacja ma się uruchomić także bez sieci (np. w metrze) i pokazać komunikat
 * "brak połączenia" zamiast białej strony przeglądarki.
 *  - pliki aplikacji ("powłoka"): najpierw sieć (zawsze świeża wersja), przy braku sieci – kopia z pamięci,
 *  - /api/...: zawsze sieć – dane o hulajnogach i wypożyczeniach muszą być aktualne,
 *  - kafelki mapy z innego serwera: obsługuje je przeglądarka bez zmian.
 */
const CACHE = 'hulaj-v2';
const APP_SHELL = [
  '/',
  '/manifest.webmanifest',
  '/static/client.css',
  '/static/client.js',
  '/static/vendor/leaflet/leaflet.css',
  '/static/vendor/leaflet/leaflet.js',
  '/static/icons/icon.svg',
  '/static/icons/icon-192.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // usuwamy pamięć starszych wersji aplikacji
  event.waitUntil(caches.keys().then((names) =>
    Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name)))));
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const isOurFile = url.origin === self.location.origin && !url.pathname.startsWith('/api/');
  if (event.request.method !== 'GET' || !isOurFile) return;   // przeglądarka obsłuży normalnie

  event.respondWith(networkFirst(event.request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    // cache: 'no-cache' – zawsze pytamy serwer (pamięć HTTP przeglądarki mogłaby oddać starą wersję)
    const response = await fetch(request, { cache: 'no-cache' });
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request, { ignoreSearch: true });
    if (cached) return cached;
    throw error;
  }
}
