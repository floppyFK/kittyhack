// Service Worker for online-only PWA with offline notification

// Version your cache to force updates
const CACHE_VERSION = '3';
const CACHE_NAME = 'offline-only-v' + CACHE_VERSION;

// The offline fallback page
const OFFLINE_PAGE = './offline.html';

// Install event - cache the offline page
self.addEventListener('install', event => {
  console.log('[Service Worker] Installing');
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[Service Worker] Caching offline page');
      // Use no-cache to force revalidation
      return cache.add(new Request(OFFLINE_PAGE, { cache: 'no-cache' }));
    })
  );
  // Activate immediately
  self.skipWaiting();
});

// Activate event - clean up any old caches
self.addEventListener('activate', event => {
  console.log('[Service Worker] Activating');
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.filter(cacheName => {
          return cacheName.startsWith('offline-only-') && cacheName !== CACHE_NAME;
        }).map(cacheName => {
          console.log('[Service Worker] Removing old cache', cacheName);
          return caches.delete(cacheName);
        })
      );
    }).then(() => {
      // Update the offline page whenever the service worker activates
      return caches.open(CACHE_NAME).then(cache => {
        console.log('[Service Worker] Re-caching offline page on activation');
        return cache.add(new Request(OFFLINE_PAGE, { cache: 'reload' }));
      });
    })
  );
  // Enable navigation preload to improve first-load when online
  if (self.registration && 'navigationPreload' in self.registration) {
    try {
      self.registration.navigationPreload.enable();
    } catch (e) {
      // ignore
    }
  }
  // Take control of all clients
  return self.clients.claim();
});

// Fetch event: network-first for navigation with offline fallback; network-only for others
self.addEventListener('fetch', event => {
  // Only handle GET requests
  if (event.request.method !== 'GET') return;

  const isNavigationRequest = event.request.mode === 'navigate';

  event.respondWith((async () => {
    if (isNavigationRequest) {
      try {
        // Use navigation preload if available for faster responses
        const preload = event.preloadResponse ? await event.preloadResponse : null;
        const response = preload || await fetch(event.request);

        // If server error 5xx, fall back to offline page
        if (!response.ok && response.status >= 500 && response.status < 600) {
          console.log('[Service Worker] Server error', response.status);
          const cached = await caches.match(OFFLINE_PAGE);
          return cached || response;
        }
        return response;
      } catch (err) {
        console.log('[Service Worker] Navigation fetch failed, serving offline page');
        const cached = await caches.match(OFFLINE_PAGE);
        if (cached) return cached;
        return new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
      }
    }

    // Non-navigation requests: let network fail if offline
    try {
      return await fetch(event.request);
    } catch {
      return new Response('Network error', { status: 503, headers: { 'Content-Type': 'text/plain' } });
    }
  })());
});

// Optional: allow immediate activation via postMessage
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});