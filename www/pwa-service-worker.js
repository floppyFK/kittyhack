// Service Worker for online-only PWA with offline notification

// Version your cache to force updates
const CACHE_VERSION = '1';
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
  // Take control of all clients
  return self.clients.claim();
});

// Fetch event - try network first, show offline page if network fails or server returns 5xx
self.addEventListener('fetch', event => {
  // Only handle GET requests
  if (event.request.method !== 'GET') return;
  
  // Check if this is a navigation request (HTML page)
  const isNavigationRequest = event.request.mode === 'navigate';
  
  event.respondWith(
    fetch(event.request)
      .then(response => {
        // Check if response is ok (status 200-399)
        // If server returns 5xx, show offline page for navigation requests
        if (!response.ok && response.status >= 500 && response.status < 600) {
          console.log('[Service Worker] Server error', response.status);
          
          if (isNavigationRequest) {
            return caches.match(OFFLINE_PAGE);
          }
        }
        
        // For successful responses or non-5xx errors, return the response as-is
        return response;
      })
      .catch(() => {
        console.log('[Service Worker] Network request failed, serving offline page');
        
        // If it's a navigation request and network fails, serve offline page
        if (isNavigationRequest) {
          return caches.match(OFFLINE_PAGE);
        }
        
        // For other resources (images, scripts, etc.), return a network error
        return new Response('Network error', { 
          status: 503, 
          headers: { 'Content-Type': 'text/plain' } 
        });
      })
  );
});