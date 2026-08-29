(() => {
  'use strict';

  const originalFetch = window.fetch.bind(window);

  function normalizeText(value) {
    return String(value || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
  }

  function tokenHeaders(extra = {}) {
    const headers = new Headers(extra || {});
    const token = localStorage.getItem('access_token');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return headers;
  }

  async function authFetch(url, options = {}) {
    return originalFetch(url, { ...options, headers: tokenHeaders(options.headers) });
  }

  function storyCard(panel) {
    let node = panel;
    while (node && node !== document.body) {
      if (node.querySelector && node.querySelector('textarea')) return node;
      node = node.parentElement;
    }
    return null;
  }

  function storyText(panel) {
    const card = storyCard(panel);
    if (!card) return '';
    const labels = [...card.querySelectorAll('label')];
    const label = labels.find(el => normalizeText(el.textContent) === 'Texto para narração');
    const textarea = label?.parentElement?.querySelector('textarea') || card.querySelector('textarea');
    return normalizeText(textarea?.value);
  }

  function detailMessage(data, fallback) {
    const detail = data && data.detail;
    if (detail && typeof detail === 'object') return detail.message || detail.code || JSON.stringify(detail);
    return detail || data?.message || fallback;
  }

  async function generateLogoTest(panel) {
    const text = storyText(panel);
    const status = panel.querySelector('[data-logo-test-status]');
    const button = panel.querySelector('[data-logo-test-button]');
    const video = panel.querySelector('[data-logo-test-video]');
    if (!text) {
      status.textContent = 'Gere ou cole o texto da narração antes do teste.';
      return;
    }
    button.disabled = true;
    status.textContent = 'Gerando áudio canônico pelo mesmo caminho da Supervisão…';
    try {
      const previewResponse = await authFetch('/youtube/narration-lab/production-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice: 'auto', voice_gender: 'female' })
      });
      const preview = await previewResponse.json().catch(() => ({}));
      if (!previewResponse.ok) throw new Error(detailMessage(preview, 'Falha ao gerar o áudio canônico.'));

      status.textContent = 'Áudio pronto. Renderizando vídeo rápido somente com o logo; imagens de IA = 0…';
      const renderResponse = await authFetch('/youtube/narration-lab/production-preview/logo-test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preview_id: preview.preview_id })
      });
      const result = await renderResponse.json().catch(() => ({}));
      if (!renderResponse.ok) throw new Error(detailMessage(result, 'Falha ao renderizar o teste com logo.'));

      const token = localStorage.getItem('access_token') || '';
      const response = await originalFetch(result.video_url, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        cache: 'no-store'
      });
      if (!response.ok) throw new Error('Não foi possível carregar o MP4 de teste.');
      const blob = await response.blob();
      if (video.dataset.objectUrl) URL.revokeObjectURL(video.dataset.objectUrl);
      const objectUrl = URL.createObjectURL(blob);
      video.dataset.objectUrl = objectUrl;
      video.src = objectUrl;
      video.hidden = false;
      status.textContent = `Teste pronto • imagens IA: 0 • áudio reutilizado: SIM${result.cache_hit ? ' • cache' : ''}. Ouça a narração antes de qualquer produção paga.`;
    } catch (err) {
      status.textContent = err?.message || String(err);
    } finally {
      button.disabled = false;
    }
  }

  function mount() {
    const panel = document.querySelector('[data-youtube-narration-gate]');
    if (!panel || panel.querySelector('[data-logo-test-mode]')) return;
    const box = document.createElement('div');
    box.dataset.logoTestMode = '1';
    box.className = 'mt-3 rounded-lg border border-amber-300 bg-amber-50 p-3';
    box.innerHTML = `
      <div class="font-semibold text-amber-900">🧪 Teste econômico de narração</div>
      <div class="text-xs text-slate-600 mt-1">Gera o áudio pelo mesmo caminho da Supervisão e renderiza um MP4 usando somente o logo oficial do canal. Não gera imagens de IA nem thumbnail.</div>
      <button type="button" data-logo-test-button class="mt-2 bg-amber-600 text-white px-4 py-2 rounded-lg font-semibold">▶ Testar vídeo somente com o logo</button>
      <div data-logo-test-status class="text-xs mt-2 text-slate-700"></div>
      <video data-logo-test-video hidden controls preload="metadata" class="mt-2 w-full rounded-lg bg-black"></video>`;
    panel.appendChild(box);
    box.querySelector('[data-logo-test-button]').addEventListener('click', () => generateLogoTest(panel));
  }

  new MutationObserver(mount).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', mount);
  window.setInterval(mount, 1500);
})();
