const CACHE_NAME = 'codexia-v6';
const urlsToCache = [
  '/',
  '/static/index.html',
  '/static/login.html',
  '/static/vendor/vue.global.prod.js'
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
  if (event.request.method !== 'GET') return;

  try {
    const url = new URL(event.request.url);
    const auth = event.request.headers.get('authorization') || event.request.headers.get('Authorization');
    if (auth) return;
    const range = event.request.headers.get('range') || event.request.headers.get('Range');
    if (range) return;

    if (url.origin === self.location.origin) {
      const p = url.pathname || '';
      const accepts = event.request.headers.get('accept') || '';
      const isHtmlRequest = event.request.mode === 'navigate' || accepts.includes('text/html');
      if (
        p.startsWith('/api/') ||
        p.startsWith('/auth/') ||
        p.startsWith('/music/') ||
        p.startsWith('/youtube/') ||
        p.startsWith('/factory') ||
        p.startsWith('/books') ||
        p.startsWith('/token') ||
        p.startsWith('/task/') ||
        p.startsWith('/settings') ||
        p.startsWith('/static/music/')
      ) {
        return;
      }
      // Nao manter HTML de telas administrativas em cache para evitar servir UIs antigas apos deploy.
      if (isHtmlRequest || p === '/' || p.endsWith('.html') || p.startsWith('/static/pages/')) {
        return;
      }
      if (/\.(mp3|wav|m4a|aac|ogg|mp4|webm)$/i.test(p)) return;
    }
  } catch (e) {
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
