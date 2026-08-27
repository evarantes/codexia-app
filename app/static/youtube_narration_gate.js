(() => {
  'use strict';

  const STORAGE_KEY = 'codexia.youtubeAuto.approvedNarration.v1';
  const originalFetch = window.fetch.bind(window);
  let mountedCard = null;
  let preview = null;
  let approved = null;
  let audioObjectUrl = '';

  function tokenHeaders(extra = {}) {
    const headers = new Headers(extra || {});
    const token = localStorage.getItem('access_token');
    if (token) headers.set('Authorization', `Bearer ${token}`);
    return headers;
  }

  async function authFetch(url, options = {}) {
    const response = await originalFetch(url, { ...options, headers: tokenHeaders(options.headers) });
    if (response.status === 401) {
      localStorage.removeItem('access_token');
      window.location.href = '/static/login.html';
    }
    return response;
  }

  function normalizeText(value) {
    return String(value || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
  }

  async function sha256(value) {
    const bytes = new TextEncoder().encode(normalizeText(value));
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
  }

  function storyCard() {
    const heading = [...document.querySelectorAll('h2')].find(el => normalizeText(el.textContent).includes('História/Devocional'));
    if (!heading) return null;
    let node = heading;
    while (node && node !== document.body) {
      if (node.classList && node.classList.contains('bg-white') && node.querySelector('textarea')) return node;
      node = node.parentElement;
    }
    return heading.parentElement && heading.parentElement.parentElement;
  }

  function storyTextarea(card) {
    if (!card) return null;
    const label = [...card.querySelectorAll('label')].find(el => normalizeText(el.textContent) === 'Texto para narração');
    if (label && label.parentElement) {
      const textarea = label.parentElement.querySelector('textarea');
      if (textarea) return textarea;
    }
    return card.querySelector('textarea[placeholder*="história"], textarea[placeholder*="devocional"]');
  }

  function existingGenerateButton(card) {
    return [...card.querySelectorAll('button')].find(btn => normalizeText(btn.textContent).toLowerCase().includes('gerar vídeo narrado')) || null;
  }

  function loadApproved() {
    try {
      const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (value && value.reuse_audio_from && value.text_sha256) approved = value;
    } catch (_) {}
  }

  function saveApproved(value) {
    approved = value;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
  }

  function clearApproved() {
    approved = null;
    localStorage.removeItem(STORAGE_KEY);
  }

  function detailMessage(data, fallback) {
    const detail = data && data.detail;
    if (detail && typeof detail === 'object') return detail.message || detail.code || JSON.stringify(detail);
    return detail || (data && data.message) || fallback;
  }

  function setStatus(panel, text, kind = 'info') {
    const el = panel.querySelector('[data-ng-status]');
    if (!el) return;
    el.textContent = text || '';
    el.style.color = kind === 'error' ? '#991b1b' : kind === 'success' ? '#047857' : '#475569';
  }

  async function loadProtectedAudio(url, panel) {
    if (audioObjectUrl) URL.revokeObjectURL(audioObjectUrl);
    audioObjectUrl = '';
    const response = await authFetch(url, { cache: 'no-store' });
    if (!response.ok) throw new Error('Não foi possível carregar o áudio protegido.');
    const blob = await response.blob();
    audioObjectUrl = URL.createObjectURL(blob);
    const audio = panel.querySelector('[data-ng-audio]');
    audio.src = audioObjectUrl;
    audio.hidden = false;
  }

  async function generatePreview(panel, card) {
    const textarea = storyTextarea(card);
    const text = normalizeText(textarea && textarea.value);
    if (!text) return setStatus(panel, 'Gere ou cole o texto da narração antes de criar o áudio.', 'error');
    const btn = panel.querySelector('[data-ng-preview]');
    btn.disabled = true;
    clearApproved();
    preview = null;
    setStatus(panel, 'Gerando somente a narração. Nenhuma imagem ou vídeo será criado…');
    try {
      const response = await authFetch('/youtube/narration-lab/production-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, voice: 'auto', voice_gender: 'female' })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailMessage(data, 'Falha ao gerar a narração.'));
      preview = data;
      panel.querySelector('[data-ng-spoken]').textContent = data.spoken_text_sent_to_tts || text;
      panel.querySelector('[data-ng-result]').hidden = false;
      panel.querySelector('[data-ng-approve]').disabled = false;
      await loadProtectedAudio(data.audio_url, panel);
      const duration = Number(data.audio_duration_sec || 0);
      setStatus(panel, `Narração pronta${duration ? ` • ${Math.floor(duration / 60)}m ${Math.round(duration % 60)}s` : ''}${data.cache_hit ? ' • áudio reaproveitado' : ''}. Ouça antes de aprovar.`, 'success');
    } catch (err) {
      setStatus(panel, err && err.message ? err.message : String(err), 'error');
    } finally {
      btn.disabled = false;
    }
  }

  async function approvePreview(panel, card) {
    if (!preview || !preview.preview_id) return setStatus(panel, 'Gere a narração antes de aprovar.', 'error');
    const text = normalizeText(storyTextarea(card)?.value);
    const btn = panel.querySelector('[data-ng-approve]');
    btn.disabled = true;
    try {
      const response = await authFetch('/youtube/narration-lab/production-preview/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preview_id: preview.preview_id, expected_text: text })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(detailMessage(data, 'Não foi possível aprovar a narração.'));
      saveApproved({
        preview_id: data.preview_id,
        text_sha256: data.text_sha256,
        reuse_audio_from: data.reuse_audio_from,
        approved_at: new Date().toISOString()
      });
      panel.querySelector('[data-ng-continue]').disabled = false;
      setStatus(panel, 'Narração aprovada. O vídeo usará exatamente este MP3 e não deverá gerar TTS novamente.', 'success');
    } catch (err) {
      clearApproved();
      setStatus(panel, err && err.message ? err.message : String(err), 'error');
    } finally {
      btn.disabled = false;
    }
  }

  async function continueWithApproved(panel, card) {
    const textarea = storyTextarea(card);
    const text = normalizeText(textarea && textarea.value);
    if (!approved) return setStatus(panel, 'Aprove a narração antes de avançar.', 'error');
    const currentHash = await sha256(text);
    if (currentHash !== approved.text_sha256) {
      clearApproved();
      panel.querySelector('[data-ng-continue]').disabled = true;
      return setStatus(panel, 'O texto foi alterado após a aprovação. Gere e aprove uma nova narração antes de continuar.', 'error');
    }
    const button = existingGenerateButton(card);
    if (!button) return setStatus(panel, 'Não encontrei o botão “Gerar vídeo narrado”. Recarregue a página e tente novamente.', 'error');
    setStatus(panel, 'Iniciando o vídeo com a narração aprovada e preservada…', 'success');
    button.click();
  }

  function mount() {
    const card = storyCard();
    if (!card || card === mountedCard || card.querySelector('[data-youtube-narration-gate]')) return;
    mountedCard = card;
    loadApproved();
    const textarea = storyTextarea(card);
    if (!textarea) return;

    const panel = document.createElement('div');
    panel.dataset.youtubeNarrationGate = '1';
    panel.className = 'mt-4 border-2 border-indigo-200 bg-indigo-50 rounded-xl p-4';
    panel.innerHTML = `
      <div class="font-bold text-indigo-900 mb-1">🎙️ Supervisão da narração antes do vídeo</div>
      <div class="text-sm text-slate-600 mb-3">Escolha entre produzir direto ou ouvir primeiro o áudio completo. A prévia não gera imagens, não renderiza MP4 e não entra na fila pesada.</div>
      <div class="flex flex-wrap gap-2 mb-3">
        <button type="button" data-ng-direct class="bg-green-600 text-white px-4 py-2 rounded-lg font-semibold">🎬 Gerar vídeo narrado</button>
        <button type="button" data-ng-preview class="bg-indigo-600 text-white px-4 py-2 rounded-lg font-semibold">🎙️ Gerar primeiro o áudio da narração</button>
      </div>
      <div data-ng-status class="text-sm mb-3"></div>
      <div data-ng-result hidden class="bg-white border border-indigo-200 rounded-lg p-3">
        <div class="font-semibold text-sm mb-2">Ouça e confira antes de avançar</div>
        <audio data-ng-audio hidden controls preload="metadata" style="width:100%"></audio>
        <div class="text-xs font-bold mt-3 mb-1">Texto exato enviado ao TTS</div>
        <div data-ng-spoken class="text-xs bg-slate-50 border rounded p-2 max-h-32 overflow-auto whitespace-pre-wrap"></div>
        <div class="flex flex-wrap gap-2 mt-3">
          <button type="button" data-ng-approve disabled class="bg-emerald-600 text-white px-4 py-2 rounded-lg font-semibold disabled:opacity-50">✅ Aprovar esta narração</button>
          <button type="button" data-ng-continue disabled class="bg-green-700 text-white px-4 py-2 rounded-lg font-semibold disabled:opacity-50">🎬 Avançar para geração do vídeo com este áudio</button>
          <button type="button" data-ng-redo class="border border-slate-300 bg-white px-4 py-2 rounded-lg font-semibold">🔄 Refazer somente a narração</button>
        </div>
      </div>`;

    const target = textarea.parentElement;
    target.insertAdjacentElement('afterend', panel);
    panel.querySelector('[data-ng-preview]').addEventListener('click', () => generatePreview(panel, card));
    panel.querySelector('[data-ng-redo]').addEventListener('click', () => generatePreview(panel, card));
    panel.querySelector('[data-ng-approve]').addEventListener('click', () => approvePreview(panel, card));
    panel.querySelector('[data-ng-continue]').addEventListener('click', () => continueWithApproved(panel, card));
    panel.querySelector('[data-ng-direct]').addEventListener('click', () => {
      clearApproved();
      const button = existingGenerateButton(card);
      if (button) button.click(); else setStatus(panel, 'Não encontrei o botão de geração do vídeo.', 'error');
    });
    textarea.addEventListener('input', async () => {
      if (!approved) return;
      if (await sha256(textarea.value) !== approved.text_sha256) {
        clearApproved();
        panel.querySelector('[data-ng-continue]').disabled = true;
        setStatus(panel, 'Texto alterado: a aprovação do áudio foi invalidada. Gere uma nova narração.', 'error');
      }
    });
  }

  window.fetch = async function narrationGateFetch(input, init = {}) {
    try {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = String(init.method || (input && input.method) || 'GET').toUpperCase();
      if (method === 'POST' && /\/youtube\/generate_video(?:\?|$)/.test(url) && approved && typeof init.body === 'string') {
        const payload = JSON.parse(init.body);
        const text = normalizeText(payload.story_content || payload.script_text || '');
        if (text && await sha256(text) === approved.text_sha256) {
          payload.reuse_audio_from = approved.reuse_audio_from;
          payload.approved_narration_preview_id = approved.preview_id;
          payload.approved_narration_text_sha256 = approved.text_sha256;
          init = { ...init, body: JSON.stringify(payload) };
        } else if (text) {
          clearApproved();
        }
      }
    } catch (err) {
      console.warn('Codexia narration gate interceptor warning:', err);
    }
    return originalFetch(input, init);
  };

  new MutationObserver(mount).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener('DOMContentLoaded', mount);
  window.setInterval(mount, 1500);
})();
