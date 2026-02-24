const CACHE_NAME = 'codexia-v2';
const urlsToCache = [
  '/',
  '/static/index.html',
  '/static/login.html',
  'https://cdn.tailwindcss.com',
  'https://unpkg.com/vue@3/dist/vue.global.js',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css'
];

self.addEventListener('install', event => {
  // Force the waiting service worker to become the active service worker.
  self.skipWaiting();
  
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
          // Tenta cachear arquivos, mas não falha a instalação se um falhar (opcional)
          // Mas addAll falha se *qualquer* um falhar. 
          // Vamos manter addAll para garantir os assets críticos.
          return cache.addAll(urlsToCache);
      })
      .catch(err => console.error('Falha ao cachear assets:', err))
  );
});

self.addEventListener('activate', event => {
  // Delete old caches
  const cacheWhitelist = [CACHE_NAME];
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheWhitelist.indexOf(cacheName) === -1) {
            return caches.delete(cacheName);
          }
        })
      );
    })
    // Claim clients immediately so the new SW controls the page
    .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  // Ignora requisições de API e POST para não cachear dados dinâmicos
  if (event.request.method !== 'GET' || event.request.url.includes('/api/') || event.request.url.includes('/auth/')) {
      return;
  }

  event.respondWith(
    caches.match(event.request)
      .then(response => {
        // Cache hit - return response
        if (response) {
          return response;
        }
        return fetch(event.request).then(
          function(response) {
            // Check if we received a valid response
            if(!response || response.status !== 200 || (response.type !== 'basic' && response.type !== 'cors' && response.type !== 'default')) {
              return response;
            }

            // Clone the response. A response is a stream
            // and because we want the browser to consume the response
            // as well as the cache consuming the response, we need
            // to clone it so we have two streams.
            var responseToCache = response.clone();

            caches.open(CACHE_NAME)
              .then(function(cache) {
                // Não cachear vídeos grandes ou assets pesados desnecessários
                if (!event.request.url.includes('/videos/')) {
                    cache.put(event.request, responseToCache);
                }
              });

            return response;
          }
        );
      })
  );
});
